"""Atomic routing updates; never fall back to a process restart."""
from copy import deepcopy
import hashlib
import json
import re
import time

from logger import logger

PREFIX = "marzban-squad-"


def supported(version):
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", str(version))
    return bool(match and tuple(map(int, match.groups())) >= (26, 9, 9))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def normalize(routing, strip_managed=False):
    value = deepcopy(routing)
    value["rules"] = [r for r in value.get("rules", []) if r.get("outboundTag") != "API"
                      and not (strip_managed and r.get("ruleTag", "").startswith(PREFIX))]
    return value


def sync(core, desired_hash, base_hash, routing=None):
    import xray_hot_reload as hot
    with hot.core_lock:
        if not supported(core.version):
            raise ValueError("Atomic routing requires Xray >= 26.9.9")
        if not core.started or not core.config:
            raise ValueError("Xray is not running")
        current = core.config.get("routing", {})
        if digest(normalize(current, True)) != base_hash:
            raise ValueError("Base routing differs from panel; apply the base core config first")
        if digest(normalize(current)) == desired_hash:
            return {"applied_hash": desired_hash, "needs_config": False}
        if routing is None:
            return {"needs_config": True}
        new = normalize(routing)
        if digest(new) != desired_hash or digest(normalize(new, True)) != base_hash:
            raise ValueError("Routing payload hash mismatch")
        if {k: v for k, v in new.items() if k not in ('rules', 'balancers')} != {
            k: v for k, v in current.items() if k not in ('rules', 'balancers')
        }:
            raise ValueError("Only rules and balancers can be updated live")
        outbounds = {o.get('tag') for o in core.config.get('outbounds', [])}
        for rule in new.get('rules', []):
            if rule.get('outboundTag') and rule['outboundTag'] not in outbounds:
                raise ValueError(f"Unknown outbound: {rule['outboundTag']}")
        service = [deepcopy(r) for r in current.get('rules', []) if r.get('outboundTag') == 'API']
        if not service:
            raise ValueError("API routing rule is missing")
        new['rules'] = service + new['rules']
        payload = json.dumps({'routing': new})
        timeout = 15
        started = time.monotonic()
        try:
            hot._run_xray_api(['adrules', 'stdin:'], payload=payload, timeout=timeout,
                              assets_path=getattr(core, 'assets_path', None))
        except hot.HotReloadError as exc:
            # CLI errors can include configuration contents. Log only the gRPC status.
            grpc_status = re.search(r"rpc error: code = ([A-Za-z]+) desc =", str(exc))
            logger.warning(
                "Xray adrules outcome=failed duration_ms=%.1f timeout_s=%d grpc_status=%s rules=%d",
                (time.monotonic() - started) * 1000, timeout,
                grpc_status.group(1) if grpc_status else "unavailable", len(new['rules']),
            )
            raise
        duration_ms = (time.monotonic() - started) * 1000
        # Commit tracked state ONLY after Xray accepts it, under the same lock
        # used by start/restart/update-xray and user reconciliation.
        core.config['routing'] = new
        logger.info("Xray adrules outcome=applied duration_ms=%.1f timeout_s=%d rules=%d",
                    duration_ms, timeout, len(new['rules']))
        return {"applied_hash": desired_hash, "needs_config": False}
