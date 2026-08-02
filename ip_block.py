"""OS-level temporary IP blocking via ipset + iptables/ip6tables.

An entry is added to a persistent ipset with a TTL; the kernel evicts it
automatically once the TTL expires, so nothing in this process needs to
track active blocks or clean them up itself — a restart of this service
does not lose or extend a block, since the set and its rule live in the
kernel's netfilter state, not in this process' memory.

Requires the container/host to run with NET_ADMIN (and NET_RAW for the
iptables rule) and have ipset + iptables installed; see Dockerfile /
docker-compose.yml.
"""
import ipaddress
import shutil
import subprocess

from logger import logger

_IPSET_V4 = "marzban-blocklist4"
_IPSET_V6 = "marzban-blocklist6"

_initialized = {"v4": False, "v6": False}


class IpBlockError(Exception):
    pass


def _run(cmd: list) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _run_checked(cmd: list) -> None:
    result = _run(cmd)
    if result.returncode != 0:
        raise IpBlockError(f"{' '.join(cmd)} failed: {result.stderr.strip()}")


def _require_tools() -> None:
    for binary in ("ipset", "iptables", "ip6tables"):
        if not shutil.which(binary):
            raise IpBlockError(
                f"'{binary}' is not installed — IP blocking requires it "
                "(the node image/host must provide ipset, iptables and ip6tables)."
            )


def _ensure_ready(is_v6: bool) -> None:
    key = "v6" if is_v6 else "v4"
    if _initialized[key]:
        return

    _require_tools()

    set_name = _IPSET_V6 if is_v6 else _IPSET_V4
    family = "inet6" if is_v6 else "inet"
    iptables_bin = "ip6tables" if is_v6 else "iptables"

    create = _run(["ipset", "create", set_name, "hash:ip", "family", family, "timeout", "0"])
    if create.returncode != 0 and "already exists" not in create.stderr.lower():
        raise IpBlockError(f"ipset create failed: {create.stderr.strip()}")

    check = _run([iptables_bin, "-C", "INPUT", "-m", "set", "--match-set", set_name, "src", "-j", "DROP"])
    if check.returncode != 0:
        _run_checked([iptables_bin, "-I", "INPUT", "1", "-m", "set", "--match-set", set_name, "src", "-j", "DROP"])

    _initialized[key] = True


def block_ip(ip: str, minutes: float) -> dict:
    # Fractional minutes are allowed (e.g. 0.1 = 6s) for callers that want a
    # near-instant block; ipset's --timeout only accepts whole seconds, so
    # round rather than truncate (0.1 * 60 can land on 5.999999999999999
    # due to float representation).
    seconds = round(minutes * 60)
    if seconds <= 0:
        raise IpBlockError("minutes must resolve to a positive number of seconds")

    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        raise IpBlockError(f"{ip!r} is not a valid IP address")

    is_v6 = parsed.version == 6
    _ensure_ready(is_v6)

    set_name = _IPSET_V6 if is_v6 else _IPSET_V4
    # -exist: refresh the timeout instead of erroring if already blocked.
    _run_checked(["ipset", "add", set_name, str(parsed), "timeout", str(seconds), "-exist"])

    logger.warning(f"Blocked {parsed} for {seconds}s")
    return {"ip": str(parsed), "minutes": minutes}


def unblock_ip(ip: str) -> dict:
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        raise IpBlockError(f"{ip!r} is not a valid IP address")

    set_name = _IPSET_V6 if parsed.version == 6 else _IPSET_V4
    result = _run(["ipset", "del", set_name, str(parsed)])
    stderr = (result.stderr or "").lower()
    if result.returncode != 0 and "not in set" not in stderr and "doesn't exist" not in stderr:
        raise IpBlockError(f"ipset del failed: {result.stderr.strip()}")

    return {"ip": str(parsed)}


def list_blocked_ips() -> list:
    """Best-effort listing of currently-blocked IPs, for diagnostics."""
    entries = []
    for set_name in (_IPSET_V4, _IPSET_V6):
        result = _run(["ipset", "list", set_name])
        if result.returncode != 0:
            continue
        in_members = False
        for line in result.stdout.splitlines():
            if line.strip() == "Members:":
                in_members = True
                continue
            if in_members and line.strip():
                entries.append(line.strip())
    return entries
