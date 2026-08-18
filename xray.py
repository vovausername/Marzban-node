import atexit
import json
import re
import subprocess
import threading
import time
from collections import deque
from contextlib import contextmanager

from config import (DEBUG, SSL_CERT_FILE, SSL_KEY_FILE, XRAY_API_HOST,
                    XRAY_API_PORT, XRAY_HOT_RELOAD_ENABLED,
                    XRAY_LOCAL_API_PORT, XRAY_START_CONFIRM_SECONDS,
                    XRAY_START_POLL_INTERVAL, INBOUNDS)
from logger import logger


def get_xray_version(executable_path: str):
    cmd = [executable_path, "version"]
    output = subprocess.check_output(
        cmd, stderr=subprocess.STDOUT).decode('utf-8')
    m = re.match(r'^Xray (\d+\.\d+\.\d+)', output)
    if m:
        return m.groups()[0]


def wait_until_ready(core, timeout: float = None, poll_interval: float = None) -> bool:
    """Confirm a just-started/restarted core actually stayed up for the
    whole confirmation window — not merely that it came up at some point.

    core.start()/restart() only spawn the process and return immediately,
    so this is the only reliable way to notice a config/binary combination
    that starts, then dies moments later — replacing per-caller stdout
    log-scanning, which only ever gated on core.started anyway.

    Polls core.started for the full `timeout` seconds and returns False
    the instant the process dies — but never returns True before the
    window elapses. Returning early on any positive signal (e.g. the
    first successful response from Xray's own API) would let a
    binary/config that comes up, serves briefly, then crashes a moment
    later slip through as "ready": xray_updater.py's rollback safety net
    specifically depends on this window to catch exactly that case, and
    declaring success early would delete the backup binary before a
    delayed crash had a chance to show up.
    """
    if timeout is None:
        timeout = XRAY_START_CONFIRM_SECONDS
    if poll_interval is None:
        poll_interval = XRAY_START_POLL_INTERVAL

    time.sleep(poll_interval)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not core.started:
            return False
        time.sleep(poll_interval)

    return core.started


class XRayConfig(dict):
    """
    Loads Xray config json
    config must contain an inbound with the API_INBOUND tag name which handles API requests
    """

    def __init__(self, config: str, peer_ip: str):
        config = json.loads(config)

        self.api_host = XRAY_API_HOST
        self.api_port = XRAY_API_PORT
        self.ssl_cert = SSL_CERT_FILE
        self.ssl_key = SSL_KEY_FILE
        self.peer_ip = peer_ip

        super().__init__(config)
        self._apply_api()

    def to_json(self, **json_kwargs):
        return json.dumps(self, **json_kwargs)

    def _apply_api(self):
        for inbound in self.get('inbounds', []).copy():
            if inbound.get('protocol') == 'dokodemo-door' and inbound.get('tag') in ('API_INBOUND',
                                                                                     'API_INBOUND_LOCAL'):
                self['inbounds'].remove(inbound)

            elif INBOUNDS and inbound.get('tag') not in INBOUNDS:
                self['inbounds'].remove(inbound)

        for rule in self.get('routing', {}).get("rules", []):
            api_tag = self.get('api', {}).get('tag')
            if api_tag and rule.get('outboundTag') == api_tag:
                self['routing']['rules'].remove(rule)

        self["api"] = {
            "services": [
                "HandlerService",
                "StatsService",
                "LoggerService",
                "RoutingService"
            ],
            "tag": "API"
        }
        self["stats"] = {}
        inbound = {
            "listen": self.api_host,
            "port": self.api_port,
            "protocol": "dokodemo-door",
            "settings": {
                "address": "127.0.0.1"
            },
            "streamSettings": {
                "security": "tls",
                "tlsSettings": {
                    "certificates": [
                        {
                            "certificateFile": self.ssl_cert,
                            "keyFile": self.ssl_key
                        }
                    ]
                }
            },
            "tag": "API_INBOUND"
        }
        try:
            self["inbounds"].insert(0, inbound)
        except KeyError:
            self["inbounds"] = []
            self["inbounds"].insert(0, inbound)

        api_inbound_tags = ["API_INBOUND"]
        if XRAY_HOT_RELOAD_ENABLED:
            # Loopback-only plaintext twin of the API inbound above: the
            # `xray api` CLI used by xray_hot_reload.py can't speak TLS.
            self["inbounds"].insert(1, {
                "listen": "127.0.0.1",
                "port": XRAY_LOCAL_API_PORT,
                "protocol": "dokodemo-door",
                "settings": {
                    "address": "127.0.0.1"
                },
                "tag": "API_INBOUND_LOCAL"
            })
            api_inbound_tags.append("API_INBOUND_LOCAL")

        rule = {
            "inboundTag": api_inbound_tags,
            "source": [
                "127.0.0.1",
                self.peer_ip
            ],
            "outboundTag": "API",
            "type": "field"
        }
        try:
            self["routing"]["rules"].insert(0, rule)
        except KeyError:
            self["routing"] = {"rules": []}
            self["routing"]["rules"].insert(0, rule)


class XRayCore:
    def __init__(self,
                 executable_path: str = "/usr/bin/xray",
                 assets_path: str = "/usr/share/xray"):
        self.executable_path = executable_path
        self.assets_path = assets_path

        self.version = self.get_version()
        self.process = None
        self._log_thread = None
        self.restarting = False

        self._logs_buffer = deque(maxlen=100)
        self._temp_log_buffers = {}
        self._on_start_funcs = []
        self._on_stop_funcs = []
        self._env = {
            "XRAY_LOCATION_ASSET": assets_path
        }

        atexit.register(lambda: self.stop() if self.started else None)

    def get_version(self):
        return get_xray_version(self.executable_path)

    def __capture_process_logs(self):
        def capture_and_debug_log():
            while self.process:
                output = self.process.stdout.readline()
                if output:
                    output = output.strip()
                    self._logs_buffer.append(output)
                    for buf in list(self._temp_log_buffers.values()):
                        buf.append(output)
                    logger.debug(output)

                elif not self.process or self.process.poll() is not None:
                    break

        def capture_only():
            while self.process:
                output = self.process.stdout.readline()
                if output:
                    output = output.strip()
                    self._logs_buffer.append(output)
                    for buf in list(self._temp_log_buffers.values()):
                        buf.append(output)

                elif not self.process or self.process.poll() is not None:
                    break

        thread = threading.Thread(target=capture_and_debug_log if DEBUG else capture_only)
        thread.start()
        return thread

    @contextmanager
    def get_logs(self):
        buf = deque(self._logs_buffer, maxlen=100)
        buf_id = id(buf)
        try:
            self._temp_log_buffers[buf_id] = buf
            yield buf
        except (EOFError, TimeoutError):
            pass
        finally:
            del self._temp_log_buffers[buf_id]
            del buf

    @property
    def started(self):
        if not self.process:
            return False

        if self.process.poll() is None:
            return True

        return False

    def start(self, config: XRayConfig):
        if self.started is True:
            raise RuntimeError("Xray is started already")

        if config.get('log', {}).get('logLevel') in ('none', 'error'):
            config['log']['logLevel'] = 'warning'

        cmd = [
            self.executable_path,
            "run",
            '-config',
            'stdin:'
        ]
        self.process = subprocess.Popen(
            cmd,
            env=self._env,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
            universal_newlines=True
        )
        self.process.stdin.write(config.to_json())
        self.process.stdin.flush()
        self.process.stdin.close()

        self._log_thread = self.__capture_process_logs()

        # execute on start functions
        for func in self._on_start_funcs:
            threading.Thread(target=func).start()

    def stop(self):
        if not self.started:
            return

        process = self.process
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        self.process = None
        logger.warning("Xray core stopped")

        # execute on stop functions
        for func in self._on_stop_funcs:
            threading.Thread(target=func).start()

    def restart(self, config: XRayConfig):
        if self.restarting is True:
            return

        self.restarting = True
        try:
            logger.warning("Restarting Xray core...")
            self.stop()
            self._start_with_bind_retry(config)
        finally:
            self.restarting = False

    # Delays (seconds) between successive bind-conflict retries in
    # _start_with_bind_retry — 3 attempts total (2 retries).
    _BIND_RETRY_DELAYS = (0.5, 1.0)
    _BIND_RETRY_PROBE_SECONDS = 0.5
    _BIND_RETRY_PROBE_INTERVAL = 0.05

    def _start_with_bind_retry(self, config: XRayConfig) -> None:
        """start(), retrying a couple of times if the freshly-spawned
        process dies almost immediately with "address already in use".

        The OS can take a brief moment to fully release a listening socket
        right after the previous Xray process exits (observed in practice
        on XHTTP/H2 inbounds) — a restart landing in that narrow window
        would otherwise fail outright even though the very same port binds
        cleanly a moment later. Only retries a fast, bind-conflict-shaped
        failure caught within a short probe window; anything else (a
        different startup error, or a process that's still running past
        the probe window) is left exactly as before for the caller's own
        wait_until_ready()/get_logs() to report.
        """
        delays = (0.0, *self._BIND_RETRY_DELAYS)
        for attempt, delay in enumerate(delays, start=1):
            if delay:
                time.sleep(delay)

            self.start(config)

            if wait_until_ready(self, timeout=self._BIND_RETRY_PROBE_SECONDS,
                                 poll_interval=self._BIND_RETRY_PROBE_INTERVAL):
                return  # survived the probe window — hand off to the caller as usual

            # The process has already exited (wait_until_ready only returns
            # False once core.started is False), but the background
            # capture thread reads its stdout asynchronously and may not
            # have drained the final "Failed to start: ..." line yet.
            # Join it — its own loop naturally stops at EOF once the dead
            # process's stdout is fully drained, which is prompt since the
            # process has already exited — before reading logs, and only
            # clear self.process afterward. Reading logs first and/or
            # clearing self.process before the thread catches up races the
            # thread's `while self.process:` check and can make it exit
            # having missed the very line we need to classify the failure.
            log_thread = self._log_thread
            if log_thread is not None:
                log_thread.join(timeout=2.0)

            with self.get_logs() as logs:
                last_log = logs[-1] if logs else ''
            self.process = None

            if attempt == len(delays) or "address already in use" not in last_log.lower():
                return  # give up, or not our failure mode — caller reports it as before

            logger.warning(
                f"Xray failed to bind (attempt {attempt}/{len(delays)}), retrying: {last_log}"
            )

    def on_start(self, func: callable):
        self._on_start_funcs.append(func)
        return func

    def on_stop(self, func: callable):
        self._on_stop_funcs.append(func)
        return func
