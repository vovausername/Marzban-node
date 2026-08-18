"""Apply user (client) changes to the running Xray core without a restart.

A full core restart drops every active user connection, but the panel's
restart calls are almost always just "a user was added/removed/changed".
This module diffs the incoming config against the one currently running:
if they are identical it's a no-op, and if the ONLY difference is the
"clients" lists inside inbound settings, the delta is applied to the live
process through Xray's HandlerService. Anything else — or any error on
this hot path — makes the caller fall back to the plain full restart, so
the node can never keep running a config the panel didn't send.

No grpcio dependency: HandlerService is driven through the xray binary's
own `api adu` / `api rmu` subcommands (available since Xray v25.7.26)
against the loopback plaintext API inbound that xray.py injects — the CLI
only speaks plaintext gRPC, so it can't use the panel's TLS API inbound.
"""
import json
import re
import subprocess
import threading

from config import (INBOUNDS, XRAY_EXECUTABLE_PATH,
                    XRAY_HOT_ADD_TIMEOUT_SECONDS, XRAY_HOT_RELOAD_ENABLED,
                    XRAY_LOCAL_API_PORT)
from logger import logger

# `adu`/`rmu` print per-user errors but still exit 0; the trailing
# "Added/Removed N user(s) in total." line is the only reliable success
# signal, so it is parsed and compared against the expected count.
_TOTAL_RE = re.compile(r"(?:Added|Removed) (\d+) user\(s\) in total\.")

# `rmu`'s per-user error for a user the live process doesn't have — see
# _expect_removed for why this is treated as success rather than a hard
# failure.
_NOT_FOUND_RE = re.compile(r"User (\S+) not found", re.IGNORECASE)

# One synchronization boundary around every mutation of the live core:
# connect/disconnect/start/stop/restart, hot-reload's diff against
# core.config, and the core.config assignment that follows any of them.
# Both FastAPI's sync handlers (run in a threadpool) and the threaded rpyc
# server let these calls overlap, and two of them interleaving on the same
# core.process — or a restart diffing against a core.config another
# request hasn't finished updating — can leave the node's idea of its own
# state out of sync with the actual live process. Re-entrant so callers
# can hold it across a whole multi-step operation (e.g. try_hot_reload()
# plus its full-restart fallback) while try_hot_reload() also stays safe
# called standalone.
core_lock = threading.RLock()

# xray version string -> whether its CLI has `api adu`/`api rmu`.
_cli_probe_cache = {}


class HotReloadError(Exception):
    pass


def _cli_supports_user_ops(version) -> bool:
    cached = _cli_probe_cache.get(version)
    if cached is not None:
        return cached

    try:
        output = subprocess.check_output(
            [XRAY_EXECUTABLE_PATH, "help", "api"],
            stderr=subprocess.STDOUT, timeout=10
        ).decode()
        supported = bool(re.search(r"\badu\b", output) and re.search(r"\brmu\b", output))
    except Exception:
        supported = False

    _cli_probe_cache.clear()  # only the current binary's answer matters
    _cli_probe_cache[version] = supported
    return supported


def _strip_clients(config: dict) -> tuple:
    """Deep-copy `config` with every inbound's settings.clients removed;
    return (stripped_config, {inbound_tag: clients_list})."""
    stripped = json.loads(json.dumps(config))
    clients_by_tag = {}
    for inbound in stripped.get("inbounds", []):
        settings = inbound.get("settings")
        if isinstance(settings, dict):
            clients_by_tag[inbound.get("tag")] = settings.pop("clients", [])
        else:
            clients_by_tag[inbound.get("tag")] = []
    return stripped, clients_by_tag


def _clients_by_email(clients: list):
    """Key clients by email; None if any client has no email or an email
    repeats within the inbound — such lists can't be diffed reliably."""
    by_email = {}
    for client in clients:
        email = client.get("email")
        if not email or email in by_email:
            return None
        by_email[email] = client
    return by_email


def compute_client_delta(old_config: dict, new_config: dict):
    """Diff two post-normalization configs (XRayConfig dicts).

    Returns None when they differ beyond inbound client lists (caller must
    fully restart), or {"added": {tag: [client, ...]}, "removed":
    {tag: [email, ...]}} — with both empty meaning the configs are
    identical. A client whose body changed for the same email (uuid,
    password, flow, ...) appears in both removed and added.
    """
    old_stripped, old_clients = _strip_clients(old_config)
    new_stripped, new_clients = _strip_clients(new_config)

    if json.dumps(old_stripped, sort_keys=True) != json.dumps(new_stripped, sort_keys=True):
        return None

    added = {}
    removed = {}
    # Tag sets are equal here: tags live in the stripped copies just compared.
    for tag, new_list in new_clients.items():
        old_list = old_clients.get(tag, [])
        if json.dumps(old_list, sort_keys=True) == json.dumps(new_list, sort_keys=True):
            continue

        old_map = _clients_by_email(old_list)
        new_map = _clients_by_email(new_list)
        if old_map is None or new_map is None:
            return None

        tag_removed = [email for email in old_map if email not in new_map]
        tag_added = [client for email, client in new_map.items() if email not in old_map]
        for email, client in new_map.items():
            old_client = old_map.get(email)
            if old_client is not None and \
                    json.dumps(old_client, sort_keys=True) != json.dumps(client, sort_keys=True):
                tag_removed.append(email)
                tag_added.append(client)

        if tag_removed:
            removed[tag] = tag_removed
        if tag_added:
            added[tag] = tag_added

    return {"added": added, "removed": removed}


def _run_xray_api(args: list, payload: str = None, timeout: int = 3) -> str:
    # Flags must precede positional args (emails / stdin:) — Go's flag
    # parser stops at the first non-flag argument.
    cmd = [
        XRAY_EXECUTABLE_PATH, "api", args[0],
        f"--server=127.0.0.1:{XRAY_LOCAL_API_PORT}",
        "-timeout", str(timeout),
        *args[1:],
    ]
    try:
        result = subprocess.run(cmd, input=payload, capture_output=True,
                                text=True, timeout=timeout + 12)
    except subprocess.TimeoutExpired:
        raise HotReloadError(f"xray api {args[0]} timed out")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise HotReloadError(f"xray api {args[0]} failed: {detail}")
    return result.stdout


def _record_added_entry(core, key: str, entry: dict) -> None:
    """Append `entry` onto core.config[key] so the tracked config matches
    what's actually running. Without this, core.config stays a stale
    pre-add snapshot: update_xray() would rebuild Xray from it and
    silently drop every hot-added handler, and a later restart carrying
    that same stale config back would hit try_hot_reload()'s
    identical-config no-op path and leave the discrepancy in place."""
    config = getattr(core, "config", None)
    if config is None:
        return
    config.setdefault(key, []).append(json.loads(json.dumps(entry)))


def add_inbound(core, inbound: dict) -> str:
    """Hot-add a single inbound to the running Xray process via `xray api
    adi` against the loopback plaintext API inbound — no restart, so
    existing connections on every other inbound are undisturbed. Raises
    HotReloadError (duplicate tag, Xray unreachable, tag outside the
    configured INBOUNDS allowlist, ...) carrying the CLI's own stderr text
    (or an allowlist message) as the message. On success, records the
    addition onto core.config (see _record_added_entry).

    Enforces INBOUNDS itself: unlike /start and /restart, this doesn't go
    through XRayConfig._apply_api(), which is the only other place that
    filter is applied — without this check an operator's INBOUNDS
    allowlist would be silently bypassable through this endpoint."""
    if INBOUNDS and inbound.get("tag") not in INBOUNDS:
        raise HotReloadError(
            f"inbound tag {inbound.get('tag')!r} is not in the configured INBOUNDS allowlist"
        )
    output = _run_xray_api(
        ["adi", "stdin:"],
        payload=json.dumps({"inbounds": [inbound]}),
        timeout=XRAY_HOT_ADD_TIMEOUT_SECONDS,
    )
    _record_added_entry(core, "inbounds", inbound)
    return output


def add_outbound(core, outbound: dict) -> str:
    """Same as add_inbound() but for outbounds, via `xray api ado`."""
    output = _run_xray_api(
        ["ado", "stdin:"],
        payload=json.dumps({"outbounds": [outbound]}),
        timeout=XRAY_HOT_ADD_TIMEOUT_SECONDS,
    )
    _record_added_entry(core, "outbounds", outbound)
    return output


def _record_removed_entry(core, key: str, tag: str) -> None:
    """Drop the entry tagged `tag` from core.config[key] — the inverse of
    _record_added_entry(). Without this, core.config would keep claiming a
    handler is running after it's actually been removed: update_xray()
    would resurrect it on the next rebuild, and a later restart carrying
    the panel's config (which never had it) would see a spurious
    structural diff instead of matching."""
    config = getattr(core, "config", None)
    if config is None:
        return
    entries = config.get(key)
    if not isinstance(entries, list):
        return
    config[key] = [entry for entry in entries if entry.get("tag") != tag]


# Tags xray.py's XRayConfig._apply_api() injects for the node's own control
# channel. Removing either would cut off the `xray api` CLI (and, for the
# TLS one, the panel's own connection) — refused outright rather than left
# to whatever error Xray happens to raise.
_PROTECTED_INBOUND_TAGS = frozenset({"API_INBOUND", "API_INBOUND_LOCAL"})


def remove_inbound(core, tag: str) -> str:
    """Hot-remove a single inbound by tag via `xray api rmi` — no restart,
    existing connections on every other inbound are undisturbed. Raises
    HotReloadError (unknown tag, Xray unreachable, protected tag, ...)
    carrying the CLI's own stderr text (or a protection message) as the
    message. On success, drops the entry from core.config (see
    _record_removed_entry)."""
    if tag in _PROTECTED_INBOUND_TAGS:
        raise HotReloadError(f"refusing to remove protected inbound tag {tag!r}")
    output = _run_xray_api(["rmi", tag], timeout=XRAY_HOT_ADD_TIMEOUT_SECONDS)
    _record_removed_entry(core, "inbounds", tag)
    return output


def remove_outbound(core, tag: str) -> str:
    """Same as remove_inbound() but for outbounds, via `xray api rmo`."""
    output = _run_xray_api(["rmo", tag], timeout=XRAY_HOT_ADD_TIMEOUT_SECONDS)
    _record_removed_entry(core, "outbounds", tag)
    return output


def _expect_total(stdout: str, expected: int, operation: str) -> None:
    m = _TOTAL_RE.search(stdout)
    if not m or int(m.group(1)) != expected:
        raise HotReloadError(
            f"xray api {operation} applied {m.group(1) if m else 'unknown'} "
            f"of {expected} user(s): {stdout.strip()}"
        )


def _expect_removed(stdout: str, emails: list, tag: str) -> None:
    """rmu prints per-user errors but exits 0; a user reported "not found"
    is treated as success rather than a hard failure — the desired end
    state (user gone from this inbound) is already true. This happens
    routinely when an earlier hot-reload attempt removed the same users
    live but then failed on a *later* tag before core.config could be
    updated to match (see the progressive core.config sync in
    apply_delta): the next attempt recomputes the identical "remove these"
    delta against a live process that's already caught up. Any other
    failure (Xray unreachable, malformed email, ...) still raises."""
    m = _TOTAL_RE.search(stdout)
    removed_count = int(m.group(1)) if m else 0
    not_found = set(_NOT_FOUND_RE.findall(stdout)) & set(emails)
    if removed_count + len(not_found) < len(emails):
        raise HotReloadError(
            f"xray api rmu applied {removed_count} of {len(emails)} user(s) "
            f"on inbound {tag!r}: {stdout.strip()}"
        )


def _record_user_delta(core, tag: str, removed_emails=None, added_clients=None) -> None:
    """Update core.config's inbound `tag` client list to reflect a
    successful adu/rmu call — the per-user equivalent of
    _record_added_entry/_record_removed_entry, applied incrementally as
    apply_delta works through each tag rather than only once at the very
    end. Without this, a later tag's rmu/adu failing mid apply_delta would
    leave core.config believing this tag's already-successful change never
    happened: the next hot-reload attempt would recompute the exact same
    delta for this tag and hit spurious "already removed"/"already exists"
    responses against the live process instead of a no-op."""
    config = getattr(core, "config", None)
    if config is None:
        return
    for inbound in config.get("inbounds", []):
        if inbound.get("tag") != tag:
            continue
        settings = inbound.get("settings")
        if not isinstance(settings, dict):
            return
        clients = settings.get("clients")
        if not isinstance(clients, list):
            return
        if removed_emails:
            clients[:] = [c for c in clients if c.get("email") not in removed_emails]
        if added_clients:
            clients.extend(json.loads(json.dumps(added_clients)))
        return


def apply_delta(core, new_config: dict, delta: dict) -> None:
    # Removals first: a changed user's old credentials must be gone before
    # `adu` re-adds them — it errors on a duplicate email.
    for tag, emails in delta["removed"].items():
        output = _run_xray_api(["rmu", f"-tag={tag}", *emails])
        _expect_removed(output, emails, tag)
        # The full requested set is confirmed gone either way (freshly
        # removed or already absent) — see _expect_removed.
        _record_user_delta(core, tag, removed_emails=set(emails))

    if not delta["added"]:
        return

    # One `adu` call over stdin. Each payload inbound must be a complete
    # valid inbound (the CLI Build()s it before extracting users), so ship
    # full copies from the new config with only the added clients inside.
    payload_inbounds = []
    total_added = 0
    for inbound in new_config.get("inbounds", []):
        clients = delta["added"].get(inbound.get("tag"))
        if not clients:
            continue
        inbound_copy = json.loads(json.dumps(inbound))
        inbound_copy["settings"]["clients"] = clients
        payload_inbounds.append(inbound_copy)
        total_added += len(clients)

    output = _run_xray_api(["adu", "stdin:"],
                           payload=json.dumps({"inbounds": payload_inbounds}))
    _expect_total(output, total_added, "adu")
    for inbound in payload_inbounds:
        _record_user_delta(core, inbound["tag"], added_clients=inbound["settings"]["clients"])


def try_hot_reload(core, new_config: dict) -> bool:
    """Try to bring the running core to `new_config` without restarting it.

    True means the config is now in effect and core.config has been
    updated — the caller reports success without touching the process
    (users were hot-applied, or the config was already up to date). False
    means the caller must do the normal full restart — including after any
    hot-path error, since the full restart re-applies the complete new
    config and therefore re-syncs the live state no matter how far the hot
    path got. Callers should hold core_lock across this call AND their
    fallback restart + core.config assignment, so overlapping restart
    requests can't diff against a core.config another thread is still
    updating.
    """
    with core_lock:
        if not XRAY_HOT_RELOAD_ENABLED:
            return False
        if not core.started:
            return False
        old_config = getattr(core, "config", None)
        if old_config is None:
            return False

        delta = compute_client_delta(old_config, new_config)
        if delta is None:
            return False

        if not delta["added"] and not delta["removed"]:
            logger.info("Config unchanged, skipping Xray restart")
            core.config = new_config
            return True

        if not _cli_supports_user_ops(core.version):
            logger.warning(
                f"Xray {core.version} lacks `api adu`/`rmu` (needs >= 25.7.26); "
                "falling back to full restart"
            )
            return False

        try:
            apply_delta(core, new_config, delta)
        except Exception as exc:
            logger.warning(f"Hot reload failed ({exc}), falling back to full restart")
            return False

        n_added = sum(len(v) for v in delta["added"].values())
        n_removed = sum(len(v) for v in delta["removed"].values())
        tags = sorted(set(delta["added"]) | set(delta["removed"]))
        logger.info(
            f"Hot-reloaded users without restart: +{n_added} -{n_removed} "
            f"on inbound(s) {', '.join(tags)}"
        )
        core.config = new_config
        return True
