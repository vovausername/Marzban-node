"""End-to-end check of xray_hot_reload against a real Xray process.

Everything else in this suite fakes the `xray api` CLI, so the output
formats those fakes imitate (per-user "already exists"/"not found" lines,
`inbounduser` JSON, exit codes) are only as right as this file says they
are. Skipped unless XRAY_REAL_BINARY points at an Xray >= 25.7.26
executable — CI has none; run it locally after touching xray_hot_reload.py
or bumping the pinned Xray version:

    XRAY_REAL_BINARY=/path/to/xray pytest tests/test_real_xray_integration.py
"""
import json
import os
import socket
import subprocess
import time

import pytest

import xray_hot_reload as hr
from certificate import generate_certificate

REAL_BINARY = os.environ.get("XRAY_REAL_BINARY")

pytestmark = pytest.mark.skipif(
    not (REAL_BINARY and os.path.isfile(REAL_BINARY)),
    reason="XRAY_REAL_BINARY is not set to an Xray executable",
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _vless(email, n):
    return {"id": f"00000000-0000-0000-0000-{n:012d}", "email": email}


def _trojan(email, password):
    return {"password": password, "email": email}


def _ss(email, password):
    return {"password": password, "method": "chacha20-ietf-poly1305", "email": email}


class RealCore:
    started = True

    def __init__(self, config, version):
        self.config = config
        self.version = version


@pytest.fixture
def xray(monkeypatch, tmp_path):
    api_port = _free_port()
    config = {
        "log": {"loglevel": "warning"},
        "api": {"services": ["HandlerService", "StatsService"], "tag": "API"},
        "inbounds": [
            {"listen": "127.0.0.1", "port": api_port, "protocol": "dokodemo-door",
             "settings": {"address": "127.0.0.1"}, "tag": "API_INBOUND_LOCAL"},
            {"tag": "vless", "listen": "127.0.0.1", "port": _free_port(), "protocol": "vless",
             "settings": {"clients": [_vless("1.Alice", 1)], "decryption": "none"}},
            {"tag": "trojan", "listen": "127.0.0.1", "port": _free_port(), "protocol": "trojan",
             "settings": {"clients": [_trojan("1.Alice", "p1")]}},
            {"tag": "ss", "listen": "127.0.0.1", "port": _free_port(), "protocol": "shadowsocks",
             "settings": {"clients": [_ss("1.Alice", "p1")], "network": "tcp,udp"}},
            {"tag": "plain", "listen": "127.0.0.1", "port": _free_port(), "protocol": "dokodemo-door",
             "settings": {"address": "127.0.0.1", "clients": []}},
            {"tag": "ss22", "listen": "127.0.0.1", "port": _free_port(), "protocol": "shadowsocks",
             "settings": {"method": "2022-blake3-aes-128-gcm", "password": "AAAAAAAAAAAAAAAAAAAAAA==",
                          "clients": [{"password": "BBBBBBBBBBBBBBBBBBBBBA==", "email": "1.Alice"}],
                          "network": "tcp,udp"}},
            {"tag": "hy", "listen": "127.0.0.1", "port": _free_port(), "protocol": "hysteria",
             "settings": {"version": 2, "clients": [{"auth": "h1", "email": "1.Alice"}]},
             "streamSettings": {
                 "network": "hysteria", "security": "tls",
                 "tlsSettings": {"alpn": ["h3"], "certificates": [
                     {"certificateFile": str(tmp_path / "cert.pem"), "keyFile": str(tmp_path / "key.pem")}]},
                 "hysteriaSettings": {"version": 2}}},
        ],
        "outbounds": [{"protocol": "freedom", "tag": "direct"}],
        "routing": {"rules": [{"inboundTag": ["API_INBOUND_LOCAL"], "outboundTag": "API", "type": "field"}]},
    }
    pems = generate_certificate()
    (tmp_path / "cert.pem").write_text(pems["cert"])
    (tmp_path / "key.pem").write_text(pems["key"])
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    process = subprocess.Popen([REAL_BINARY, "run", "-config", str(config_path)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    monkeypatch.setattr(hr, "XRAY_EXECUTABLE_PATH", REAL_BINARY)
    monkeypatch.setattr(hr, "XRAY_LOCAL_API_PORT", api_port)
    monkeypatch.setattr(hr, "XRAY_HOT_RELOAD_ENABLED", True)
    hr._cli_probe_cache.clear()
    try:
        deadline = time.time() + 10
        while True:
            try:
                socket.create_connection(("127.0.0.1", api_port), timeout=0.2).close()
                break
            except OSError:
                assert process.poll() is None, "xray exited during startup"
                assert time.time() < deadline, "xray API did not come up"
                time.sleep(0.1)
        version = subprocess.check_output([REAL_BINARY, "version"]).decode().split()[1]
        yield RealCore(config, version)
    finally:
        process.terminate()
        process.wait(timeout=10)
        hr._cli_probe_cache.clear()


def _live(tag):
    return sorted(email for emails in hr._list_live_emails(tag).values() for email in emails)


def _push_directly(tag, definition, clients):
    """What the panel does over gRPC, behind this node's back."""
    inbound = dict(definition)
    inbound["settings"] = dict(definition["settings"], clients=clients)
    hr._run_xray_api(["adu", "stdin:"], payload=json.dumps({"inbounds": [inbound]}))


def _definition(core, tag):
    return next(i for i in core.config["inbounds"] if i["tag"] == tag)


def test_cli_probe_finds_the_user_commands(xray):
    assert hr._cli_supports_user_ops(xray.version) is True
    assert hr.can_keep_core(xray) is True


def test_listing_reports_live_users_per_inbound(xray):
    assert _live("vless") == ["1.Alice"]
    with pytest.raises(hr.HotReloadError):
        hr._list_live_emails("plain")  # not a UserManager
    with pytest.raises(hr.HotReloadError):
        hr._list_live_emails("no-such-tag")


def test_sync_heals_every_kind_of_drift(xray):
    # Behind the node's back: a user it never tracked appears on both
    # inbounds, and a tracked one disappears from trojan.
    _push_directly("vless", _definition(xray, "vless"), [_vless("2.ghost", 2)])
    _push_directly("trojan", _definition(xray, "trojan"), [_trojan("2.ghost", "p2")])
    hr._run_xray_api(["rmu", "-tag=trojan", "1.Alice"])
    assert _live("vless") == ["1.Alice", "2.ghost"]
    assert _live("trojan") == ["2.ghost"]

    stats = hr.sync_users(xray, {
        "vless": [_vless("1.Alice", 1), _vless("3.new", 3)],
        "trojan": [_trojan("1.Alice", "p1")],
        "unknown-tag": [],
    })

    assert _live("vless") == ["1.Alice", "3.new"]
    assert _live("trojan") == ["1.Alice"]
    assert stats["skipped_tags"] == ["unknown-tag"]
    assert stats["added"] == 2 and stats["removed"] == 2 and stats["unchanged"] == 1

    # Converged: a second pass only reads.
    again = hr.sync_users(xray, {
        "vless": [_vless("1.Alice", 1), _vless("3.new", 3)],
        "trojan": [_trojan("1.Alice", "p1")],
    })
    assert (again["added"], again["removed"], again["unchanged"]) == (0, 0, 3)


def test_sync_collapses_shadowsocks_duplicates(xray):
    # Classic Shadowsocks accepts the same email again instead of
    # reporting "already exists".
    _push_directly("ss", _definition(xray, "ss"), [_ss("1.Alice", "p1"), _ss("1.Alice", "p1")])
    assert _live("ss") == ["1.Alice"] * 3

    hr.sync_users(xray, {"ss": [_ss("1.Alice", "p1")]})

    assert _live("ss") == ["1.Alice"]


def test_concurrent_direct_add_is_tolerated(xray, monkeypatch):
    # The panel adds the same user between this node's listing and its adu.
    real_run = hr._run_xray_api

    def racing(args, payload=None, timeout=3):
        if args[0] == "adu":
            _push = dict(_definition(xray, "vless"))
            _push["settings"] = dict(_push["settings"], clients=[_vless("5.race", 5)])
            real_run(["adu", "stdin:"], payload=json.dumps({"inbounds": [_push]}))
        return real_run(args, payload=payload, timeout=timeout)

    monkeypatch.setattr(hr, "_run_xray_api", racing)

    hr.sync_users(xray, {"vless": [_vless("1.Alice", 1), _vless("5.race", 5)]})

    monkeypatch.setattr(hr, "_run_xray_api", real_run)
    assert _live("vless") == ["1.Alice", "5.race"]


def test_hot_reload_applies_user_changes_and_skips_non_user_inbounds(xray):
    new_config = json.loads(json.dumps(xray.config))
    _definition_new = next(i for i in new_config["inbounds"] if i["tag"] == "vless")
    _definition_new["settings"]["clients"] = [_vless("1.Alice", 99), _vless("4.d", 4)]

    assert hr.try_hot_reload(xray, new_config) is True
    assert _live("vless") == ["1.Alice", "4.d"]
    assert xray.config is new_config


def test_hot_reload_declines_when_a_non_user_inbound_gains_clients(xray):
    new_config = json.loads(json.dumps(xray.config))
    next(i for i in new_config["inbounds"] if i["tag"] == "plain")["settings"]["clients"] = [_vless("1.Alice", 1)]

    assert hr.try_hot_reload(xray, new_config) is False


def test_bulk_sync_of_many_users(xray):
    clients = [_vless(f"{i}.user{i}", i) for i in range(1000, 6000)]

    stats = hr.sync_users(xray, {"vless": clients})
    assert stats["added"] == 5000 and stats["removed"] == 1
    assert len(_live("vless")) == 5000

    stats = hr.sync_users(xray, {"vless": clients[:10]})
    assert stats["removed"] == 4990
    assert len(_live("vless")) == 10


def _calls(monkeypatch):
    """Record which `xray api` subcommands run from here on."""
    seen = []
    real_run = hr._run_xray_api

    def recording(args, payload=None, timeout=3):
        seen.append(args[0])
        return real_run(args, payload=payload, timeout=timeout)

    monkeypatch.setattr(hr, "_run_xray_api", recording)
    return seen


def test_untracked_users_with_the_wanted_credentials_are_not_touched(xray, monkeypatch):
    # Users the panel pushed itself: unknown to core.config, but Xray's own
    # listing proves they already hold the wanted credentials.
    _push_directly("vless", _definition(xray, "vless"), [_vless("2.b", 2)])
    _push_directly("trojan", _definition(xray, "trojan"), [_trojan("2.b", "p2")])
    _push_directly("ss", _definition(xray, "ss"), [_ss("2.b", "p2")])
    _push_directly("ss22", _definition(xray, "ss22"),
                   [{"password": "CCCCCCCCCCCCCCCCCCCCCA==", "email": "2.b"}])
    seen = _calls(monkeypatch)

    stats = hr.sync_users(xray, {
        "vless": [_vless("1.Alice", 1), _vless("2.b", 2)],
        "trojan": [_trojan("1.Alice", "p1"), _trojan("2.b", "p2")],
        "ss": [_ss("1.Alice", "p1"), _ss("2.b", "p2")],
        "ss22": [{"password": "BBBBBBBBBBBBBBBBBBBBBA==", "email": "1.Alice"},
                 {"password": "CCCCCCCCCCCCCCCCCCCCCA==", "email": "2.b"}],
    })

    assert set(seen) == {"inbounduser"}
    assert (stats["added"], stats["removed"], stats["unchanged"]) == (0, 0, 8)


def test_live_user_with_other_credentials_is_replaced_even_if_tracked(xray):
    # The panel rotated the user over gRPC and the DB now says something
    # else again: core.config matches the wanted entry, the process doesn't.
    hr._run_xray_api(["rmu", "-tag=vless", "1.Alice"])
    _push_directly("vless", _definition(xray, "vless"), [_vless("1.Alice", 777)])

    stats = hr.sync_users(xray, {"vless": [_vless("1.Alice", 1)]})

    assert (stats["added"], stats["removed"]) == (1, 1)
    account = hr._list_live_users("vless")["1.alice"][0][1]
    assert account["id"] == _vless("1.Alice", 1)["id"]


def test_hysteria_users_are_verified_and_removed_but_never_cut_off(xray):
    hy = [{"auth": "h1", "email": "1.Alice"}]
    assert hr._live_account_matches(hr._list_live_users("hy")["1.alice"][0][1], hy[0]) is True

    # Unchanged: fine. A user it should not have: removed.
    assert hr.sync_users(xray, {"hy": hy})["unchanged"] == 1

    # `adu` can't add Hysteria users: a sync that would need to must fail
    # before removing anything, on this inbound or any other.
    _push_directly("vless", _definition(xray, "vless"), [_vless("9.ghost", 9)])
    for wanted in ([{"auth": "rotated", "email": "1.Alice"}],
                   hy + [{"auth": "h2", "email": "2.b"}]):
        with pytest.raises(hr.HotReloadError):
            hr.sync_users(xray, {"vless": [_vless("1.Alice", 1)], "hy": wanted})
        assert _live("hy") == ["1.Alice"]
        assert _live("vless") == ["1.Alice", "9.ghost"]

    # ...and the same change on /restart falls back to a full restart.
    new_config = json.loads(json.dumps(xray.config))
    next(i for i in new_config["inbounds"] if i["tag"] == "hy")["settings"]["clients"] = hy + [
        {"auth": "h2", "email": "2.b"}]
    assert hr.try_hot_reload(xray, new_config) is False
    assert _live("hy") == ["1.Alice"]


def test_allow_pending_keeps_hysteria_from_blocking_the_rest(xray):
    hy = [{"auth": "h1", "email": "1.Alice"}]
    _push_directly("vless", _definition(xray, "vless"), [_vless("9.ghost", 9)])

    stats = hr.sync_users(xray, {
        "vless": [_vless("1.Alice", 1), _vless("2.b", 2)],
        "hy": [{"auth": "rotated", "email": "1.Alice"}, {"auth": "h2", "email": "2.b"}],
    }, allow_pending=True)

    assert _live("vless") == ["1.Alice", "2.b"]
    assert _live("hy") == ["1.Alice"]  # still on its old account, not cut off
    assert stats["pending_users"] == {"hy": ["1.Alice", "2.b"]}

    # In sync again once nothing is pending.
    assert hr.sync_users(xray, {"hy": hy}, allow_pending=True)["pending_users"] == {}
