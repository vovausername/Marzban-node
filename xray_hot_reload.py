"""Apply user (client) changes to the running Xray core without a restart.

A full core restart drops every active user connection, but the panel's
restart calls are almost always just "a user was added/removed/changed".
This module compares the incoming config with the one currently running:
if they differ ONLY in the "clients" lists inside inbound settings (or not
at all), the wanted client lists are reconciled against the users the live
process actually has, through Xray's HandlerService. Anything else — or
any error on this hot path — makes the caller fall back to the plain full
restart, so the node can never keep running a config the panel didn't send.
sync_users() exposes the same reconciliation on its own, for a panel that
only wants a node's users brought up to date.

No grpcio dependency: HandlerService is driven through the xray binary's
own `api adu` / `api rmu` / `api inbounduser` subcommands (all present
since Xray v25.7.26)
against the loopback plaintext API inbound that xray.py injects — the CLI
only speaks plaintext gRPC, so it can't use the panel's TLS API inbound.
"""
import json
import re
import subprocess
import threading

from config import (INBOUNDS, XRAY_EXECUTABLE_PATH,
                    XRAY_HOT_ADD_TIMEOUT_SECONDS, XRAY_HOT_RELOAD_ENABLED,
                    XRAY_INBOUNDUSER_TIMEOUT_SECONDS,
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

# `adu`'s per-user error for an email the live process already has — see
# _expect_added.
_ALREADY_EXISTS_RE = re.compile(r"User (\S+) already exists", re.IGNORECASE)

# Emails per `rmu` call: they travel as argv, so an unbounded list can hit
# the kernel's argument-size limit on a big removal.
_RMU_CHUNK = 2000

# Clients per `adu` call (summed over the inbounds in its payload).
_ADU_CHUNK = 20000

# Users per remove-then-re-add step, see reconcile_users().
_REPLACE_CHUNK = 500

# Inbound protocols whose users `xray api adu` can add. For anything else
# (e.g. hysteria) the CLI answers "unsupported inbound type" and adds
# nothing, although `rmu` and `inbounduser` work — so on such an inbound
# users can be listed and removed here, but never (re-)added.
_ADU_PROTOCOLS = frozenset({"vless", "vmess", "trojan", "shadowsocks"})

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


class NotUserManageable(HotReloadError):
    """Xray has the inbound, but its protocol has no users to list or
    change — as opposed to failing to ask Xray at all."""


class HotReloadUnavailable(HotReloadError):
    """User hot-ops can't be used on this node right now (feature off, old
    Xray binary, core not running) — as opposed to a failed apply."""


def _cli_supports_user_ops(version) -> bool:
    """Whether this xray binary's CLI has `api adu`/`rmu` (add/remove users)
    and `api inbounduser` (list an inbound's live users) — everything
    reconcile_users() needs."""
    cached = _cli_probe_cache.get(version)
    if cached is not None:
        return cached

    try:
        output = subprocess.check_output(
            [XRAY_EXECUTABLE_PATH, "help", "api"],
            stderr=subprocess.STDOUT, timeout=10
        ).decode()
        supported = bool(re.search(r"\badu\b", output) and re.search(r"\brmu\b", output)
                         and re.search(r"\binbounduser\b", output))
    except Exception:
        supported = False

    _cli_probe_cache.clear()  # only the current binary's answer matters
    _cli_probe_cache[version] = supported
    return supported


def _strip_clients(config: dict) -> tuple:
    """Return (config_without_clients, {inbound_tag: clients_list}).

    Nothing is deep-copied: the result shares every nested object with
    `config` except the inbound and settings dicts that had to lose their
    "clients" key, so it is only good for reading/serializing. A config
    carries every user of every inbound — copying and re-serializing all of
    that just to compare the (small) rest of the config was most of the
    cost of a hot reload."""
    stripped = dict(config)
    inbounds = []
    clients_by_tag = {}
    for inbound in config.get("inbounds", []):
        settings = inbound.get("settings") if isinstance(inbound, dict) else None
        if isinstance(settings, dict):
            clients_by_tag[inbound.get("tag")] = settings.get("clients", [])
            if "clients" in settings:
                inbound = dict(inbound)
                inbound["settings"] = {k: v for k, v in settings.items() if k != "clients"}
        elif isinstance(inbound, dict):
            clients_by_tag[inbound.get("tag")] = []
        inbounds.append(inbound)
    if "inbounds" in config:
        stripped["inbounds"] = inbounds
    return stripped, clients_by_tag


def _same_structure(old_stripped: dict, new_stripped: dict) -> bool:
    return json.dumps(old_stripped, sort_keys=True) == json.dumps(new_stripped, sort_keys=True)


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

    if not _same_structure(old_stripped, new_stripped):
        return None

    added = {}
    removed = {}
    # Tag sets are equal here: tags live in the stripped copies just compared.
    for tag, new_list in new_clients.items():
        old_list = old_clients.get(tag, [])
        if old_list == new_list:
            continue

        old_map = _clients_by_email(old_list)
        new_map = _clients_by_email(new_list)
        if old_map is None or new_map is None:
            return None

        tag_removed = [email for email in old_map if email not in new_map]
        tag_added = [client for email, client in new_map.items() if email not in old_map]
        for email, client in new_map.items():
            old_client = old_map.get(email)
            if old_client is not None and old_client != client:
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
            f"of {expected} user(s): {stdout.strip()[-2000:]}"
        )


def _reported_emails(pattern, stdout: str, emails: list) -> int:
    """How many of `pattern`'s per-user lines in `stdout` are about one of
    `emails` (compared case-insensitively: Xray lowercases emails)."""
    wanted = {str(email).lower() for email in emails}
    return sum(1 for email in pattern.findall(stdout) if email.rstrip(".").lower() in wanted)


def _expect_removed(stdout: str, emails: list, tag: str) -> None:
    """rmu prints per-user errors but exits 0; a user reported "not found"
    is treated as success rather than a hard failure — the desired end
    state (user gone from this inbound) is already true. This happens
    routinely: the panel also removes users straight through Xray's gRPC
    API, so the live process can be ahead of anything this node tracked.
    Any other failure (Xray unreachable, malformed email, ...) still
    raises."""
    m = _TOTAL_RE.search(stdout)
    removed_count = int(m.group(1)) if m else 0
    not_found = _reported_emails(_NOT_FOUND_RE, stdout, emails)
    if removed_count + not_found < len(emails):
        raise HotReloadError(
            f"xray api rmu applied {removed_count} of {len(emails)} user(s) "
            f"on inbound {tag!r}: {stdout.strip()[-2000:]}"
        )


def _expect_added(stdout: str, emails: list) -> None:
    """adu's counterpart of _expect_removed: a user reported "already
    exists" is treated as success. reconcile_users() only adds emails the
    live process didn't have a moment earlier, so this can only be the
    panel adding the very same user through Xray's gRPC API in between —
    and what the panel pushes directly is at least as fresh as the list
    this sync was built from. Before this was tolerated, one such user
    turned every hot reload into a full restart that dropped all
    connections."""
    m = _TOTAL_RE.search(stdout)
    added_count = int(m.group(1)) if m else 0
    exists = _reported_emails(_ALREADY_EXISTS_RE, stdout, emails)
    if added_count + exists < len(emails):
        raise HotReloadError(
            f"xray api adu applied {added_count} of {len(emails)} user(s): "
            f"{stdout.strip()[-2000:]}"
        )


def _bulk_timeout(operations: int) -> int:
    """`-timeout` for a CLI call making `operations` loopback RPCs. Xray
    handles several thousand of them per second; this leaves a wide margin
    and still bounds a hung process."""
    return max(10, 5 + operations // 500)


def _inbounduser_timeout(users: int) -> int:
    """`-timeout` for `api inbounduser` on an inbound expected to hold about
    `users` users: 30 s floor, +1 s per 100 users, capped by
    XRAY_INBOUNDUSER_TIMEOUT_SECONDS so a hung Xray can't hold a worker
    for minutes."""
    return min(max(30, 10 + users // 100), max(30, XRAY_INBOUNDUSER_TIMEOUT_SECONDS))


def _lowercase_map(clients: list):
    """{lowercased email: client}; None if a client has no email or two
    clients collide — such a list can't be reconciled reliably."""
    by_email = {}
    for client in clients:
        email = client.get("email") if isinstance(client, dict) else None
        if not email or not isinstance(email, str) or email.lower() in by_email:
            return None
        by_email[email.lower()] = client
    return by_email


def _list_live_users(tag: str, expected_users: int = 0) -> dict:
    """{lowercased email: [(email, account, level), ...]} for every user
    Xray currently has on inbound `tag`: the email as the live process
    reports it, the account it holds (credentials as Xray prints them) and
    its policy level. One list entry per occurrence — a classic Shadowsocks
    inbound accepts the same email more than once.

    Raises NotUserManageable when the inbound's protocol has no users, and
    plain HotReloadError for everything else (Xray unreachable, timeout,
    unknown tag, unparsable answer): only the former says anything about
    the inbound, the rest means the live state is simply unknown."""
    try:
        output = _run_xray_api(["inbounduser", f"-tag={tag}"],
                               timeout=_inbounduser_timeout(expected_users))
    except HotReloadError as exc:
        if "not a usermanager" in str(exc).lower():
            raise NotUserManageable(str(exc))
        raise
    try:
        users = (json.loads(output) if output.strip() else {}).get("users") or []
    except (ValueError, AttributeError):
        raise HotReloadError(f"xray api inbounduser returned unparsable output for inbound {tag!r}")
    live = {}
    for user in users:
        email = user.get("email") if isinstance(user, dict) else None
        if email:
            account = user.get("account")
            live.setdefault(email.lower(), []).append(
                (email, account if isinstance(account, dict) else {}, user.get("level") or 0))
    return live


def _list_live_emails(tag: str) -> dict:
    """{lowercased email: [email, ...]} — _list_live_users() without accounts."""
    return {key: [entry[0] for entry in entries] for key, entries in _list_live_users(tag).items()}


def _same_level(live_level, client: dict) -> bool:
    """Whether the live user's policy level is the one the client entry
    asks for (Xray omits level 0 from its listing, configs usually too)."""
    try:
        return int(live_level or 0) == int(client.get("level") or 0)
    except (TypeError, ValueError):
        return False


def _cipher_name(value) -> str:
    """Shadowsocks cipher spelled the same whether it comes from a config
    ("chacha20-ietf-poly1305") or from Xray's listing ("CHACHA20_POLY1305")."""
    return re.sub(r"[^A-Z0-9]", "", str(value).upper()).replace("IETF", "")


_CREDENTIAL_NAMES = {
    "id": ("id",),
    "password": ("password", "pass"),
    "auth": ("auth",),
    "key": ("password",),
}


def _live_account_matches(account: dict, client: dict):
    """Whether the live `account` of a user (as `api inbounduser` prints it)
    carries the credentials of the wanted config `client`.

    True: every field Xray reports equals the client's. False: one differs.
    None: can't tell — Xray reported no credential at all, or a field this
    doesn't know how to compare.
    """
    verified = False
    for key, value in account.items():
        if key == "_TypedMessage_":
            continue
        if key in ("id", "password", "auth", "key"):
            # The same credential under the names configs use for it:
            # Shadowsocks 2022 reports the user's key (config: password),
            # MASQUE reports password (config: pass).
            wanted = next((client[name] for name in _CREDENTIAL_NAMES[key] if client.get(name) is not None), None)
            if wanted is None:
                return False
            wanted, value = str(wanted), str(value)
            if key == "id":  # UUIDs compare case-insensitively
                wanted, value = wanted.lower(), value.lower()
            if wanted != value:
                return False
            verified = True
        elif key == "flow":
            pass  # compared below, also when Xray omits it
        elif key == "cipherType":
            if "method" in client and _cipher_name(client["method"]) != _cipher_name(value):
                return False
        elif key == "encryption":
            # VLESS accounts the panel pushes over gRPC carry
            # encryption "none"; inbound client entries never set it.
            if str(value or "none") != str(client.get("encryption") or "none"):
                return None
        elif key == "securitySettings":
            if "security" in client:
                return None
        else:
            return None
    if (account.get("flow") or "") != (client.get("flow") or ""):
        return False
    return True if verified else None


def _set_tracked_clients(core, tag: str, clients: list) -> None:
    """Point core.config's inbound `tag` at `clients`, keeping the tracked
    config in step with what was just applied to the live process."""
    config = getattr(core, "config", None)
    if config is None:
        return
    for inbound in config.get("inbounds", []):
        if inbound.get("tag") == tag and isinstance(inbound.get("settings"), dict):
            inbound["settings"]["clients"] = clients
            return


def reconcile_users(core, desired_by_tag: dict, inbound_defs: dict = None,
                    allow_pending: bool = False) -> dict:
    """Make the live process hold exactly `desired_by_tag[tag]` on each
    listed inbound, without restarting it.

    The live user list is read back from Xray itself (`api inbounduser`)
    instead of being assumed equal to core.config: the panel also adds and
    removes users directly through Xray's gRPC API, which this node never
    sees, so core.config drifts from the running process within minutes.
    Diffing against that stale copy both missed real differences and
    reported phantom ones.

    Per inbound: users the process has but the list doesn't are removed;
    users the list has but the process doesn't are added; a user present
    on both sides is left alone when the credentials Xray reports for it
    are the wanted ones (or, where Xray reports none, when core.config
    already tracks exactly the wanted entry), and is removed + re-added
    otherwise. Duplicate live entries for one email are collapsed the same
    way. On an inbound whose protocol `xray api adu` can't add users to
    (see _ADU_PROTOCOLS) users are only ever removed. The users such an
    inbound would need added or replaced are left exactly as they are and
    returned as "pending_users" when `allow_pending` is set — the caller
    promises to push those itself (the panel can, over Xray's gRPC API);
    without it the whole call fails before touching anything.

    `inbound_defs` supplies the inbound definitions to build `adu` payloads
    from (the CLI needs a complete inbound); core.config's own are used
    when omitted. Tags the running config doesn't have are skipped and
    reported. An inbound that can't be reconciled (not user-manageable, or
    a client list with a missing/repeated email) is skipped when its client
    list is the one already tracked, and fails the call otherwise.

    Returns {"added", "removed", "unchanged", "skipped_tags",
    "pending_users": {tag: [email, ...]}}. Raises
    HotReloadError on any failure; core.config then reflects the removals
    already applied, and the next call re-reads the live state anyway.
    """
    config = getattr(core, "config", None) or {}
    running = {
        inbound.get("tag"): inbound
        for inbound in config.get("inbounds", []) if isinstance(inbound, dict)
    }
    inbound_defs = inbound_defs or {}

    # (tag, wanted clients, emails to drop, [(live emails, client)] to
    # replace, clients to add)
    plans = []
    skipped_tags = []
    pending_users = {}
    unchanged = 0
    for tag, clients in desired_by_tag.items():
        inbound = running.get(tag)
        if inbound is None or tag in _PROTECTED_INBOUND_TAGS:
            skipped_tags.append(tag)
            continue
        if not isinstance(clients, list):
            raise HotReloadError(f"clients of inbound {tag!r} must be a list")

        settings = inbound.get("settings")
        tracked_list = settings.get("clients") if isinstance(settings, dict) else None
        tracked = _lowercase_map(tracked_list) if isinstance(tracked_list, list) else None
        untouched = (tracked_list or []) == clients

        # An inbound this can't reconcile — a client without an email, a
        # repeated email, or a protocol that has no users in Xray — is
        # left alone as long as its client list is the one already tracked
        # (nothing was asked to change on it), and fails the call otherwise.
        # A listing that failed for any other reason (Xray unreachable,
        # timeout, garbage) always fails the call: the live state is
        # unknown then, and reporting success would hide exactly the drift
        # this exists to repair.
        desired = _lowercase_map(clients)
        if desired is None:
            if untouched:
                continue
            raise HotReloadError(f"inbound {tag!r} has a client without an email or a repeated email")
        try:
            live = _list_live_users(tag, max(len(clients), len(tracked_list or [])))
        except NotUserManageable:
            if untouched:
                continue
            raise

        to_drop = []
        to_replace = []
        for email, entries in live.items():
            occurrences = [entry[0] for entry in entries]
            wanted = desired.get(email)
            if wanted is None:
                to_drop.extend(occurrences)
                continue
            # Trust what the process itself reports about the user's
            # credentials; only when that says nothing fall back to "the
            # tracked config already has exactly this entry".
            matches = _live_account_matches(entries[0][1], wanted) if len(entries) == 1 else False
            if matches is None:
                matches = tracked is not None and tracked.get(email) == wanted
            # The level lives on the user, not in the account: same
            # credentials under another policy level is still a change.
            if matches and not _same_level(entries[0][2], wanted):
                matches = False
            if matches:
                unchanged += 1
            else:
                to_replace.append((occurrences, wanted))
        to_add = [client for email, client in desired.items() if email not in live]

        if (to_add or to_replace) and inbound.get("protocol") not in _ADU_PROTOCOLS:
            if not allow_pending:
                # Decided here, before anything is removed anywhere: removing
                # a user this can't add back would cut them off until the
                # caller's fallback (full restart / per-user sync) gets to them.
                raise HotReloadError(
                    f"inbound {tag!r} ({inbound.get('protocol')}) needs users added, "
                    "which `xray api adu` can't do for this protocol"
                )
            # Hand them to the caller untouched: a user to replace keeps its
            # current account until the caller swaps it.
            pending_users[tag] = (
                [client["email"] for _occurrences, client in to_replace]
                + [client["email"] for client in to_add]
            )
            to_replace, to_add = [], []
        plans.append((tag, clients, to_drop, to_replace, to_add))

    def _rmu(tag, emails):
        for start in range(0, len(emails), _RMU_CHUNK):
            chunk = emails[start:start + _RMU_CHUNK]
            output = _run_xray_api(["rmu", f"-tag={tag}", *chunk], timeout=_bulk_timeout(len(chunk)))
            _expect_removed(output, chunk, tag)
        gone = {email.lower() for email in emails}
        tracked_list = (running[tag].get("settings") or {}).get("clients")
        if gone and isinstance(tracked_list, list):
            _set_tracked_clients(core, tag, [
                client for client in tracked_list
                if not (isinstance(client, dict) and str(client.get("email", "")).lower() in gone)
            ])

    # `adu` over stdin. Each payload inbound must be a complete valid
    # inbound (the CLI Build()s it before extracting users), so ship the
    # inbound definition with only the clients to add inside.
    def _adu(batch):
        emails = [client["email"] for inbound in batch for client in inbound["settings"]["clients"]]
        output = _run_xray_api(["adu", "stdin:"], payload=json.dumps({"inbounds": batch}),
                               timeout=_bulk_timeout(len(emails)))
        _expect_added(output, emails)

    def _payload_inbound(tag, clients):
        definition = inbound_defs.get(tag) or running[tag]
        inbound = dict(definition)
        inbound["settings"] = dict(definition.get("settings") or {})
        inbound["settings"]["clients"] = clients
        return inbound

    removed = added = 0
    for tag, _clients, to_drop, to_replace, _to_add in plans:
        if to_drop:
            _rmu(tag, to_drop)
            removed += len(to_drop)
        # Replacements go in small remove-then-add steps: between the two
        # calls those users can't open new connections (established ones
        # are unaffected), so the fewer users per step the shorter that is.
        for start in range(0, len(to_replace), _REPLACE_CHUNK):
            chunk = to_replace[start:start + _REPLACE_CHUNK]
            emails = [email for occurrences, _client in chunk for email in occurrences]
            _rmu(tag, emails)
            removed += len(emails)
            _adu([_payload_inbound(tag, [client for _occurrences, client in chunk])])
            added += len(chunk)

    batch, batch_size = [], 0
    for tag, _clients, _to_drop, _to_replace, to_add in plans:
        for start in range(0, len(to_add), _ADU_CHUNK):
            chunk = to_add[start:start + _ADU_CHUNK]
            if batch and batch_size + len(chunk) > _ADU_CHUNK:
                _adu(batch)
                added += batch_size
                batch, batch_size = [], 0
            batch.append(_payload_inbound(tag, chunk))
            batch_size += len(chunk)
    if batch:
        _adu(batch)
        added += batch_size

    for tag, clients, _to_drop, _to_replace, _to_add in plans:
        _set_tracked_clients(core, tag, json.loads(json.dumps(clients)))

    return {"added": added, "removed": removed, "unchanged": unchanged,
            "skipped_tags": skipped_tags, "pending_users": pending_users}


def _user_ops_available(core) -> bool:
    return bool(
        XRAY_HOT_RELOAD_ENABLED
        and getattr(core, "config", None) is not None
        and _cli_supports_user_ops(getattr(core, "version", None))
    )


def can_keep_core(core) -> bool:
    """Whether a new control session may take over the already-running
    process instead of stopping it: only when the caller can then bring it
    to its own config without a restart (see try_hot_reload)."""
    with core_lock:
        return bool(core.started and _user_ops_available(core))


def _pending_note(stats: dict) -> str:
    pending = sum(len(emails) for emails in stats.get("pending_users", {}).values())
    return f", {pending} left to the panel on {', '.join(map(str, stats['pending_users']))}" if pending else ""


def sync_users(core, clients_by_tag: dict, allow_pending: bool = False) -> dict:
    """Bring the running process's users to `clients_by_tag` ({inbound tag:
    [client, ...]}, the same client entries a start config carries) — the
    panel's "resync this node" in one call instead of several gRPC calls
    per user. Never restarts or stops the core: raises HotReloadUnavailable
    when user hot-ops can't be used here (the caller falls back to its own
    per-user path) and HotReloadError on a failed apply. See
    reconcile_users() for the semantics and the returned counters."""
    with core_lock:
        if not core.started:
            raise HotReloadUnavailable("Xray has not been started")
        if not _user_ops_available(core):
            raise HotReloadUnavailable(
                "user hot-ops are unavailable (XRAY_HOT_RELOAD_ENABLED is off, "
                "or this Xray lacks `api adu`/`rmu`/`inbounduser`)"
            )
        stats = reconcile_users(core, clients_by_tag, allow_pending=allow_pending)
        logger.info(
            f"Synced users without restart: +{stats['added']} -{stats['removed']} "
            f"={stats['unchanged']}" + _pending_note(stats)
            + (f", unknown inbound(s) skipped: {', '.join(map(str, stats['skipped_tags']))}"
               if stats["skipped_tags"] else "")
        )
        return stats


def try_hot_reload(core, new_config: dict) -> bool:
    """hot_reload() as a yes/no: True when `new_config` is fully in effect
    without a restart, False when the caller must do the full restart."""
    return hot_reload(core, new_config) is not None


def hot_reload(core, new_config: dict, allow_pending: bool = False):
    """Try to bring the running core to `new_config` without restarting it.

    Returns the reconcile counters (see reconcile_users) when the config is
    now in effect and core.config has been updated — the caller reports
    success without touching the process (users were reconciled against the
    live process, or nothing needed changing). With `allow_pending`, users
    this could not add (see reconcile_users) are listed under
    "pending_users" for the caller to push. Returns None when the caller
    must do the normal full restart — including after any hot-path error,
    since the full restart re-applies the complete new config and therefore
    re-syncs the live state no matter how far the hot path got. Callers
    should hold core_lock across this call AND their fallback restart +
    core.config assignment, so overlapping restart requests can't diff
    against a core.config another thread is still updating.
    """
    with core_lock:
        if not XRAY_HOT_RELOAD_ENABLED:
            return None
        if not core.started:
            return None
        old_config = getattr(core, "config", None)
        if old_config is None:
            return None

        old_stripped, old_clients = _strip_clients(old_config)
        new_stripped, new_clients = _strip_clients(new_config)
        if not _same_structure(old_stripped, new_stripped):
            return None

        if not _cli_supports_user_ops(getattr(core, "version", None)):
            if old_clients == new_clients:
                logger.info("Config unchanged, skipping Xray restart")
                core.config = new_config
                return {"added": 0, "removed": 0, "unchanged": 0, "skipped_tags": [], "pending_users": {}}
            logger.warning(
                f"Xray {getattr(core, 'version', None)} lacks `api adu`/`rmu`/`inbounduser` "
                "(needs >= 25.7.26); falling back to full restart"
            )
            return None

        # Every inbound that has, or had, a client list. One whose "clients"
        # key is gone from the new config must lose its users too.
        desired = {}
        definitions = {}
        for inbound in new_config.get("inbounds", []):
            settings = inbound.get("settings") if isinstance(inbound, dict) else None
            if not isinstance(settings, dict):
                continue
            tag = inbound.get("tag")
            clients = settings.get("clients")
            if isinstance(clients, list):
                desired[tag] = clients
            elif "clients" not in settings and old_clients.get(tag):
                desired[tag] = []
            else:
                continue
            definitions[tag] = inbound

        try:
            stats = reconcile_users(core, desired, definitions, allow_pending=allow_pending)
        except Exception as exc:
            logger.warning(f"Hot reload failed ({exc}), falling back to full restart")
            return None

        if stats["added"] or stats["removed"]:
            logger.info(
                f"Hot-reloaded users without restart: +{stats['added']} -{stats['removed']}"
                + _pending_note(stats)
            )
        else:
            logger.info("Config and live users unchanged, skipping Xray restart" + _pending_note(stats))
        core.config = new_config
        return stats
