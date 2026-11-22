"""Atomic routing updates; never fall back to a process restart."""
from copy import deepcopy
import hashlib
import json
import re
import time

from logger import logger
from routing_assets import prepare_routing

PREFIX = "marzban-squad-"


class BaseRoutingMismatch(ValueError):
    def __init__(self, current_base_hash, observed_hash):
        super().__init__("Base routing differs from panel; apply the base core config first")
        self.current_base_hash = current_base_hash
        self.observed_hash = observed_hash


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


def sync(core, desired_hash, base_hash, routing=None, *, observed_hash=None):
    import xray_hot_reload as hot
    with hot.core_lock:
        if not supported(core.version):
            raise ValueError("Atomic routing requires Xray >= 26.9.9")
        if not core.started or not core.config:
            raise ValueError("Xray is not running")
        current = core.config.get("routing", {})
        current_hash = digest(normalize(current))
        if observed_hash is not None and current_hash != observed_hash:
            raise ValueError("Routing changed since the sync check; retry synchronization")
        current_base_hash = digest(normalize(current, True))
        if current_base_hash != base_hash and observed_hash is None:
            raise BaseRoutingMismatch(current_base_hash, current_hash)
        if routing is None and current_hash == desired_hash:
            return {"applied_hash": desired_hash, "needs_config": False}
        if routing is None:
            return {"needs_config": True}
        new = normalize(routing)
        if digest(new) != desired_hash or digest(normalize(new, True)) != base_hash:
            raise ValueError("Routing payload hash mismatch")
        if current_hash == desired_hash:
            return {"applied_hash": desired_hash, "needs_config": False}
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
        assets_path = getattr(core, 'assets_path', None)
        payload = json.dumps({'routing': prepare_routing(new, assets_path)})
        timeout = 15
        started = time.monotonic()
        try:
            hot._run_xray_api(['adrules', 'stdin:'], payload=payload, timeout=timeout,
                              assets_path=assets_path)
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
