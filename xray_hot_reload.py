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
                    XRAY_HOT_ROUTING_TIMEOUT_SECONDS, XRAY_LOCAL_API_PORT)
from logger import logger

# `adu`/`rmu` print per-user errors but still exit 0; the trailing
# "Added/Removed N user(s) in total." line is the only reliable success
# signal, so it is parsed and compared against the expected count.
_TOTAL_RE = re.compile(r"(?:Added|Removed) (\d+) user\(s\) in total\.")

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


def _build_api_routing_rule(core) -> dict:
    """Rebuild the routing rule XRayConfig._apply_api() injects for
    API_INBOUND/API_INBOUND_LOCAL on every /start and /restart (see
    xray.py) — the caller's `routing` payload never has it, since the
    panel has no knowledge these node-local control inbounds exist at
    all. update_routing() must prepend this before replacing the table,
    or the node permanently loses the ability to reach its own control
    API — including every future hot-reload call (add/remove
    inbound/outbound, user adu/rmu, and update_routing itself) — the
    moment the first whole-table replace goes out without it."""
    api_inbound_tags = ["API_INBOUND"]
    if XRAY_HOT_RELOAD_ENABLED:
        api_inbound_tags.append("API_INBOUND_LOCAL")
    peer_ip = getattr(core.config, "peer_ip", None) if getattr(core, "config", None) is not None else None
    source = ["127.0.0.1", peer_ip] if peer_ip else ["127.0.0.1"]
    return {
        "inboundTag": api_inbound_tags,
        "source": source,
        "outboundTag": "API",
        "type": "field",
    }


def update_routing(core, routing: dict) -> str:
    """Hot-replace the entire routing table (rules + balancers) on the
    running Xray process via `xray api adrules` (without `-append`) — no
    restart, existing connections on every inbound/outbound are undisturbed.

    Unlike add_inbound/remove_inbound (which target one tagged entry),
    Xray's RoutingService has no per-rule "alter" or targeted "add one rule
    among many" primitive that preserves rule order: AddRule with
    shouldAppend=false atomically clears and rebuilds the whole rule (and
    balancer) list from what's sent, which is also the only way to
    guarantee the new rule order matches exactly what the caller intends
    (routing rules are evaluated in order — appending would put changed
    rules last, silently altering which rule wins for ambiguous traffic).
    `-append` is intentionally never used here for that reason.

    Always prepends the node's own control-API routing rule (see
    _build_api_routing_rule) regardless of what the caller sent — the
    panel has no idea API_INBOUND/API_INBOUND_LOCAL exist, so its payload
    never includes it, and a whole-table replace without it would strand
    the node with no route from its control inbounds to the "API"
    outbound.

    Does NOT touch domainStrategy/domainMatcher or anything else in
    `routing` besides rules/balancers — callers must fall back to a full
    restart if those changed (see routing_hot_changed in the panel).

    Known Xray-core caveat, not fixable from this side: Router.ReloadRules
    (the AddRule handler) clears r.rules/r.balancers up front when
    shouldAppend=false, then validates and appends the new ones one at a
    time; if any rule in the middle fails to build (bad domain/regex/geo
    file, duplicate ruleTag among the NEW rules, ...), it returns an error
    with r.rules already trimmed back to empty — not rolled back to the
    table that was live before this call. The Xray process keeps running
    with NO routing rules for the (typically sub-second) window between
    that failure and the caller's fallback restart completing. There is no
    server-side "validate first, apply atomically" alternative to fall
    back to; the best mitigation is what callers already do — treat any
    error here as fatal and restart immediately, keeping that window as
    short as possible rather than retrying or queuing.

    Raises HotReloadError (bad rule, duplicate ruleTag, Xray unreachable,
    ...) carrying the CLI's own stderr text as the message. On success,
    records the new (effective, with the control rule included) routing
    table onto core.config (see _record_updated_routing)."""
    routing = json.loads(json.dumps(routing or {}))
    rules = routing.setdefault("rules", [])
    # Drop any stale/caller-sent rule already targeting the API outbound
    # before prepending the freshly rebuilt one, so there is never more
    # than one control rule in the effective table.
    rules[:] = [r for r in rules if r.get("outboundTag") != "API"]
    rules.insert(0, _build_api_routing_rule(core))

    output = _run_xray_api(
        ["adrules", "stdin:"],
        payload=json.dumps({"routing": routing}),
        timeout=XRAY_HOT_ROUTING_TIMEOUT_SECONDS,
    )
    _record_updated_routing(core, routing)
    return output


def _record_updated_routing(core, routing: dict) -> None:
    """Replace core.config["routing"] with `routing` — the routing
    equivalent of _record_added_entry/_record_removed_entry. Without this,
    core.config would keep claiming the old routing table is running after
    it's actually been replaced: update_xray() would rebuild Xray from the
    stale table on the next binary swap, and a later restart carrying the
    panel's config (which already has the new table) would see a spurious
    structural diff against try_hot_reload()'s stripped-clients comparison
    instead of matching."""
    config = getattr(core, "config", None)
    if config is None:
        return
    config["routing"] = json.loads(json.dumps(routing))


def _expect_total(stdout: str, expected: int, operation: str) -> None:
    m = _TOTAL_RE.search(stdout)
    if not m or int(m.group(1)) != expected:
        raise HotReloadError(
            f"xray api {operation} applied {m.group(1) if m else 'unknown'} "
            f"of {expected} user(s): {stdout.strip()}"
        )


def apply_delta(new_config: dict, delta: dict) -> None:
    # Removals first: a changed user's old credentials must be gone before
    # `adu` re-adds them — it errors on a duplicate email.
    for tag, emails in delta["removed"].items():
        output = _run_xray_api(["rmu", f"-tag={tag}", *emails])
        _expect_total(output, len(emails), "rmu")

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
            apply_delta(new_config, delta)
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
