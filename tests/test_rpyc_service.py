"""Regression tests for rpyc_service.py — exercised both as plain Python
(permission-gating logic) and over a real rpyc connection (the wire
protocol / connection-lifecycle behavior an `rpyc` version bump could
actually break, which a same-process method call can't catch).
"""
import threading
import time

import pytest
import rpyc
from rpyc.utils.server import ThreadedServer

import rpyc_service


class FakeCore:
    def __init__(self, started=False, version="1.2.3"):
        self.started = started
        self.version = version
        self.config = None

    def stop(self):
        self.started = False


# --- Plain Python calls: permission gating and simple state checks,
# no network involved. `@rpyc.exposed` only marks a method for remote
# dispatch — nothing stops calling it directly like this. ---

def test_healthcheck_reports_not_started_when_no_core():
    service = rpyc_service.XrayService()
    data = service.healthcheck()
    assert data["isAlive"] is True
    assert data["isXrayOnline"] is False


def test_healthcheck_reports_started_from_the_live_core():
    service = rpyc_service.XrayService()
    service.core = FakeCore(started=True, version="1.8.4")
    data = service.healthcheck()
    assert data["isXrayOnline"] is True
    assert data["xrayVersion"] == "1.8.4"


def test_fetch_xray_version_without_a_core_raises():
    service = rpyc_service.XrayService()
    with pytest.raises(ProcessLookupError):
        service.fetch_xray_version()


def test_block_ip_disabled_raises_permission_error(monkeypatch):
    monkeypatch.setattr(rpyc_service, "IP_BLOCK_ENABLED", False)
    service = rpyc_service.XrayService()
    with pytest.raises(PermissionError):
        service.block_ip("203.0.113.9", 5)


def test_add_inbound_disabled_raises_permission_error(monkeypatch):
    monkeypatch.setattr(rpyc_service, "XRAY_HOT_RELOAD_ENABLED", False)
    service = rpyc_service.XrayService()
    service.core = FakeCore(started=True)
    with pytest.raises(PermissionError):
        service.add_inbound({"tag": "x"})


def test_add_inbound_without_started_core_raises():
    service = rpyc_service.XrayService()
    with pytest.raises(ProcessLookupError):
        service.add_inbound({"tag": "x"})


def test_update_node_disabled_raises_permission_error(monkeypatch):
    monkeypatch.setattr(rpyc_service, "NODE_REMOTE_UPDATE_ENABLED", False)
    service = rpyc_service.XrayService()
    with pytest.raises(PermissionError):
        service.update_node()


# --- Live rpyc connection: proves the actual wire protocol / connection
# lifecycle (on_connect/on_disconnect, single-session enforcement) still
# works after an `rpyc` version bump, not just the Python-level logic. ---

@pytest.fixture
def running_server():
    server = ThreadedServer(
        rpyc_service.XrayService(),
        hostname="127.0.0.1",
        port=0,
        protocol_config={"allow_public_attrs": True},
    )
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    # ThreadedServer.start() binds the listener before entering its accept
    # loop, but does so on the background thread; give it a moment.
    for _ in range(50):
        if server.active:
            break
        time.sleep(0.02)
    yield server
    server.close()
    thread.join(timeout=5)


def test_live_connection_healthcheck_round_trip(running_server):
    conn = rpyc.connect("127.0.0.1", running_server.port)
    try:
        data = conn.root.healthcheck()
        assert data["isAlive"] is True
        assert data["isXrayOnline"] is False
    finally:
        conn.close()


def test_live_connection_fetch_version_without_core_raises_remotely(running_server):
    conn = rpyc.connect("127.0.0.1", running_server.port)
    try:
        with pytest.raises(ProcessLookupError):
            conn.root.fetch_xray_version()
    finally:
        conn.close()


def test_second_concurrent_connection_is_rejected(running_server):
    conn1 = rpyc.connect("127.0.0.1", running_server.port)
    # on_connect()'s "is the previous connection still alive" check pings
    # conn1 from the server side (conn1.ping(), see rpyc's Connection.ping),
    # which needs conn1's own dispatch loop actively running to answer —
    # exactly what a bare, idle client (no calls in flight, no background
    # serving thread) does NOT do. A real long-lived client keeps a
    # BgServingThread running for the same reason (see get_logs() on the
    # panel's node client) — without one here, the server would wrongly
    # conclude conn1 is lost and admit conn2, which isn't what this test
    # means to exercise.
    bg = rpyc.BgServingThread(conn1)
    try:
        conn1.root.healthcheck()  # make sure conn1 is fully established first

        conn2 = rpyc.connect("127.0.0.1", running_server.port)
        # The server accepts the TCP connection but closes it immediately
        # from on_connect() once it sees conn1 is still alive — any call
        # on conn2 must fail rather than silently taking over the session.
        with pytest.raises((EOFError, ConnectionError, OSError)):
            conn2.root.healthcheck()
        conn2.close()
    finally:
        bg.stop()
        conn1.close()


def test_new_connection_is_accepted_after_previous_one_closes(running_server):
    conn1 = rpyc.connect("127.0.0.1", running_server.port)
    conn1.root.healthcheck()
    conn1.close()

    conn2 = rpyc.connect("127.0.0.1", running_server.port)
    try:
        data = conn2.root.healthcheck()
        assert data["isAlive"] is True
    finally:
        conn2.close()
