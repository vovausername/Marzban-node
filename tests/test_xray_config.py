"""XRayConfig._apply_api() is pure JSON manipulation — no subprocess, no
network — so it's a cheap, high-value place to pin down behavior a
pydantic/starlette bump has no business changing, but a careless
dependency-driven refactor could still break.
"""
import json

import xray


def _config(**overrides) -> str:
    base = {
        "inbounds": [{"tag": "vless-in", "protocol": "vless", "port": 443}],
        "outbounds": [{"tag": "direct", "protocol": "freedom"}],
    }
    base.update(overrides)
    return json.dumps(base)


def test_injects_api_inbound_first_and_sets_api_stats_blocks():
    config = xray.XRayConfig(_config(), peer_ip="203.0.113.5")

    assert config["inbounds"][0]["tag"] == "API_INBOUND"
    assert config["inbounds"][0]["protocol"] == "dokodemo-door"
    assert config["api"]["tag"] == "API"
    assert "stats" in config


def test_strips_any_existing_api_inbound_from_caller_config_first():
    raw = _config(inbounds=[
        {"tag": "API_INBOUND", "protocol": "dokodemo-door"},
        {"tag": "vless-in", "protocol": "vless", "port": 443},
    ])
    config = xray.XRayConfig(raw, peer_ip="203.0.113.5")

    # Exactly one API_INBOUND survives (the freshly injected one), not the
    # caller-supplied duplicate plus a second one appended on top.
    api_inbounds = [i for i in config["inbounds"] if i["tag"] == "API_INBOUND"]
    assert len(api_inbounds) == 1


def test_routing_rule_prepended_with_control_sources(monkeypatch):
    monkeypatch.setattr(xray, "XRAY_HOT_RELOAD_ENABLED", False)
    config = xray.XRayConfig(_config(), peer_ip="203.0.113.5")

    rule = config["routing"]["rules"][0]
    assert rule["outboundTag"] == "API"
    assert rule["source"] == ["127.0.0.1", "203.0.113.5"]
    assert rule["inboundTag"] == ["API_INBOUND"]


def test_inbounds_allowlist_filters_non_matching_tags(monkeypatch):
    monkeypatch.setattr(xray, "INBOUNDS", ["vless-in"])
    raw = _config(inbounds=[
        {"tag": "vless-in", "protocol": "vless", "port": 443},
        {"tag": "trojan-in", "protocol": "trojan", "port": 8443},
    ])
    config = xray.XRayConfig(raw, peer_ip="203.0.113.5")

    tags = {i["tag"] for i in config["inbounds"]}
    assert "vless-in" in tags
    assert "trojan-in" not in tags
    # The allowlist must never accidentally filter out the node's own
    # control inbound.
    assert "API_INBOUND" in tags


def test_hot_reload_enabled_adds_local_plaintext_api_inbound(monkeypatch):
    monkeypatch.setattr(xray, "XRAY_HOT_RELOAD_ENABLED", True)
    config = xray.XRayConfig(_config(), peer_ip="203.0.113.5")

    tags = [i["tag"] for i in config["inbounds"]]
    assert "API_INBOUND_LOCAL" in tags
    local = next(i for i in config["inbounds"] if i["tag"] == "API_INBOUND_LOCAL")
    assert local["listen"] == "127.0.0.1"
    assert config["routing"]["rules"][0]["inboundTag"] == ["API_INBOUND", "API_INBOUND_LOCAL"]


def test_hot_reload_disabled_omits_local_api_inbound(monkeypatch):
    monkeypatch.setattr(xray, "XRAY_HOT_RELOAD_ENABLED", False)
    config = xray.XRayConfig(_config(), peer_ip="203.0.113.5")

    tags = [i["tag"] for i in config["inbounds"]]
    assert "API_INBOUND_LOCAL" not in tags
    assert config["routing"]["rules"][0]["inboundTag"] == ["API_INBOUND"]


def test_to_json_round_trips():
    config = xray.XRayConfig(_config(), peer_ip="203.0.113.5")
    round_tripped = json.loads(config.to_json())
    assert round_tripped["api"]["tag"] == "API"
