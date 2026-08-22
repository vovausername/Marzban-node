"""Regression tests for rest_service.py's HTTP surface.

REST is the default SERVICE_PROTOCOL, so this file is the main thing
standing between a fastapi/starlette/pydantic/uvicorn/websockets bump and
a silent behavior change in the node's actual control API: session-id
gating, config-JSON validation, the hot-reload-vs-full-restart branch, and
websocket log streaming.

Talks to the real module-level `rest_service.app` / `rest_service.service`
singleton (that's what main.py serves) rather than building a fresh app
per test, so the RequestValidationError handler registered on `app` at
import time is exercised too. `service.core` is swapped for a FakeCore
stand-in per test so nothing here ever shells out to a real Xray binary.
"""
import json
from collections import deque
from contextlib import contextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import rest_service
import xray_hot_reload


class FakeCore:
    """Minimal stand-in for XRayCore — only the surface rest_service.py
    actually touches (.started, .config, .start/.stop/.restart, .get_logs())."""

    def __init__(self):
        self.started = False
        self.config = None
        self.logs = []

    def start(self, config):
        self.started = True
        self.config = config

    def stop(self):
        self.started = False

    def restart(self, config):
        self.started = True
        self.config = config

    @contextmanager
    def get_logs(self):
        yield deque(self.logs)


@pytest.fixture
def client():
    return TestClient(rest_service.app)


@pytest.fixture
def svc(monkeypatch):
    service = rest_service.service
    monkeypatch.setattr(service, "core", FakeCore())
    monkeypatch.setattr(service, "core_version", "test-version")
    service.connected = False
    service.session_id = None
    service.client_ip = None
    yield service
    service.connected = False
    service.session_id = None
    service.client_ip = None


def _connect(client) -> str:
    res = client.post("/connect")
    assert res.status_code == 200
    return res.json()["session_id"]


def test_base_reports_connected_started_and_version(client, svc):
    res = client.post("/")
    assert res.status_code == 200
    data = res.json()
    assert data == {"connected": False, "started": False, "core_version": "test-version"}


def test_first_connect_leaves_a_not_yet_running_core_untouched(client, svc):
    res = client.post("/connect")
    assert res.status_code == 200
    data = res.json()
    assert "session_id" in data
    assert data["connected"] is True


def test_second_connect_stops_an_already_running_core(client, svc):
    _connect(client)
    svc.core.started = True

    res = client.post("/connect")

    assert res.status_code == 200
    assert svc.core.started is False


def test_ping_with_wrong_session_id_is_rejected(client, svc):
    _connect(client)
    res = client.post("/ping", json={"session_id": str(uuid4())})
    assert res.status_code == 403


def test_ping_with_correct_session_id_succeeds(client, svc):
    sid = _connect(client)
    res = client.post("/ping", json={"session_id": sid})
    assert res.status_code == 200


def test_disconnect_clears_session_and_stops_core(client, svc):
    _connect(client)
    svc.core.started = True

    res = client.post("/disconnect")

    assert res.status_code == 200
    assert svc.core.started is False
    assert res.json()["connected"] is False


def test_start_success_starts_core_and_waits_until_ready(client, svc, monkeypatch):
    sid = _connect(client)
    monkeypatch.setattr(rest_service, "wait_until_ready", lambda core: True)
    config = json.dumps({"inbounds": [], "outbounds": []})

    res = client.post("/start", json={"session_id": sid, "config": config})

    assert res.status_code == 200
    assert svc.core.started is True


def test_start_with_invalid_config_json_returns_422(client, svc):
    sid = _connect(client)

    res = client.post("/start", json={"session_id": sid, "config": "not json"})

    assert res.status_code == 422
    assert "config" in res.json()["detail"]


def test_start_that_never_stays_up_returns_503(client, svc, monkeypatch):
    sid = _connect(client)
    monkeypatch.setattr(rest_service, "wait_until_ready", lambda core: False)
    config = json.dumps({"inbounds": [], "outbounds": []})

    res = client.post("/start", json={"session_id": sid, "config": config})

    assert res.status_code == 503


def test_restart_uses_hot_reload_and_skips_full_restart(client, svc, monkeypatch):
    sid = _connect(client)
    monkeypatch.setattr(xray_hot_reload, "try_hot_reload", lambda core, cfg: True)
    restart_calls = []
    monkeypatch.setattr(svc.core, "restart", lambda cfg: restart_calls.append(cfg))
    config = json.dumps({"inbounds": [], "outbounds": []})

    res = client.post("/restart", json={"session_id": sid, "config": config})

    assert res.status_code == 200
    assert restart_calls == []


def test_restart_falls_back_to_full_restart_when_hot_reload_declines(client, svc, monkeypatch):
    sid = _connect(client)
    monkeypatch.setattr(xray_hot_reload, "try_hot_reload", lambda core, cfg: False)
    monkeypatch.setattr(rest_service, "wait_until_ready", lambda core: True)
    config = json.dumps({"inbounds": [], "outbounds": []})

    res = client.post("/restart", json={"session_id": sid, "config": config})

    assert res.status_code == 200
    assert svc.core.started is True


def test_healthcheck_needs_no_session(client, svc):
    res = client.get("/healthcheck")
    assert res.status_code == 200
    assert res.json()["isAlive"] is True


def test_system_stats_requires_a_valid_session(client, svc):
    res = client.post("/system-stats", json={"session_id": str(uuid4())})
    assert res.status_code == 403


def test_system_stats_with_valid_session_returns_host_metrics(client, svc):
    sid = _connect(client)
    res = client.post("/system-stats", json={"session_id": sid})
    assert res.status_code == 200
    assert "cpu_count" in res.json()


def test_block_ip_returns_403_when_feature_disabled(client, svc, monkeypatch):
    monkeypatch.setattr(rest_service, "IP_BLOCK_ENABLED", False)
    sid = _connect(client)

    res = client.post("/block-ip", json={"session_id": sid, "ip": "203.0.113.9", "minutes": 5})

    assert res.status_code == 403


def test_add_inbound_requires_a_started_core(client, svc, monkeypatch):
    monkeypatch.setattr(rest_service, "XRAY_HOT_RELOAD_ENABLED", True)
    sid = _connect(client)
    svc.core.started = False

    res = client.post("/add-inbound", json={"session_id": sid, "inbound": {"tag": "x"}})

    assert res.status_code == 503


def test_logs_websocket_rejects_session_mismatch(client, svc):
    _connect(client)

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/logs?session_id={uuid4()}"):
            pass


def test_logs_websocket_streams_buffered_lines(client, svc):
    svc.core.logs = ["hello xray"]
    sid = _connect(client)

    with client.websocket_connect(f"/logs?session_id={sid}") as ws:
        assert ws.receive_text() == "hello xray"
