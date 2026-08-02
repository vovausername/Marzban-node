import time
from socket import socket
from threading import Thread

import rpyc

import ip_block
import system_stats
import version_check
import xray_hot_reload
import xray_updater
from config import IP_BLOCK_ENABLED, XRAY_ASSETS_PATH, XRAY_EXECUTABLE_PATH, XRAY_REMOTE_UPDATE_ENABLED
from logger import logger
from xray import XRayConfig, XRayCore, get_xray_version


class XrayCoreLogsHandler(object):
    def __init__(self, core: XRayCore, callback: callable, interval: float = 0.6):
        self.core = core
        self.callback = callback
        self.interval = interval
        self.active = True
        self.thread = Thread(target=self.cast)
        self.thread.start()

    def stop(self):
        self.active = False
        self.thread.join()

    def cast(self):
        with self.core.get_logs() as logs:
            cache = ''
            last_sent_ts = 0
            while self.active:
                if time.time() - last_sent_ts >= self.interval and cache:
                    self.callback(cache)
                    cache = ''
                    last_sent_ts = time.time()

                if not logs:
                    time.sleep(0.2)
                    continue

                log = logs.popleft()
                cache += f'{log}\n'


@rpyc.service
class XrayService(rpyc.Service):
    def __init__(self):
        self.core = None
        self.connection = None

    def on_connect(self, conn):
        if self.connection:
            try:
                self.connection.ping()
                if self.connection.peer is not None:
                    logger.warning(
                        f'New connection rejected, already connected to {self.connection.peer}')
                return conn.close()
            except (EOFError, TimeoutError, AttributeError):
                if hasattr(self.connection, "peer"):
                    logger.warning(
                        f'Previous connection from {self.connection.peer} has lost')

        peer, _ = socket.getpeername(conn._channel.stream.sock)
        self.connection = conn
        self.connection.peer = peer
        logger.warning(f'Connected to {self.connection.peer}')

    def on_disconnect(self, conn):
        if conn is self.connection:
            logger.warning(f'Disconnected from {self.connection.peer}')

            if self.core is not None:
                self.core.stop()

            self.core = None
            self.connection = None

    @rpyc.exposed
    def start(self, config: str):
        if self.core is not None:
            self.stop()

        try:
            config = XRayConfig(config, self.connection.peer)
            self.core = XRayCore(executable_path=XRAY_EXECUTABLE_PATH,
                                 assets_path=XRAY_ASSETS_PATH)

            if self.connection and hasattr(self.connection.root, 'on_start'):
                @self.core.on_start
                def on_start():
                    try:
                        if self.connection:
                            self.connection.root.on_start()
                    except Exception as exc:
                        logger.debug('Peer on_start exception:', exc)
            else:
                logger.debug(
                    "Peer doesn't have on_start function on it's service, skipped")

            if self.connection and hasattr(self.connection.root, 'on_stop'):
                @self.core.on_stop
                def on_stop():
                    try:
                        if self.connection:
                            self.connection.root.on_stop()
                    except Exception as exc:
                        logger.debug('Peer on_stop exception:', exc)
            else:
                logger.debug(
                    "Peer doesn't have on_stop function on it's service, skipped")

            self.core.start(config)
            self.core.config = config
        except Exception as exc:
            logger.error(exc)
            raise exc

    @rpyc.exposed
    def stop(self):
        if self.core:
            try:
                self.core.stop()
            except RuntimeError:
                pass
        self.core = None

    @rpyc.exposed
    def restart(self, config: str):
        config = XRayConfig(config, self.connection.peer)

        # restart_lock spans the hot-reload attempt, the fallback full
        # restart and the core.config update: overlapping restart requests
        # must not diff against a core.config another thread is still
        # bringing in sync with the live process.
        with xray_hot_reload.restart_lock:
            # Hot path: identical config -> no-op; only client lists
            # changed -> applied to the live core without dropping user
            # connections (core.config updated inside). Any other
            # difference (or hot-path error) falls through to the full
            # restart below.
            if self.core is not None and xray_hot_reload.try_hot_reload(self.core, config):
                return

            self.core.restart(config)
            self.core.config = config

    @rpyc.exposed
    def fetch_xray_version(self):
        if self.core is None:
            raise ProcessLookupError("Xray has not been started")

        return self.core.version

    @rpyc.exposed
    def block_ip(self, ip: str, minutes: float) -> dict:
        if not IP_BLOCK_ENABLED:
            raise PermissionError(
                "IP blocking is disabled. Set IP_BLOCK_ENABLED=true to allow it."
            )
        try:
            return ip_block.block_ip(ip, minutes)
        except ip_block.IpBlockError as exc:
            raise ValueError(str(exc))

    @rpyc.exposed
    def update_xray(self, version: str) -> dict:
        if not XRAY_REMOTE_UPDATE_ENABLED:
            raise PermissionError(
                "Remote Xray updates are disabled. Set XRAY_REMOTE_UPDATE_ENABLED=true to allow them."
            )
        if self.core is None:
            raise ProcessLookupError("Xray has not been started")
        try:
            return xray_updater.update(version, self.core)
        except xray_updater.XrayUpdateError as exc:
            raise ValueError(str(exc))

    @rpyc.exposed
    def check_for_update(self) -> dict:
        return version_check.check_for_update()

    @rpyc.exposed
    def healthcheck(self) -> dict:
        if self.core is not None:
            xray_version = self.core.version
        else:
            try:
                xray_version = get_xray_version(XRAY_EXECUTABLE_PATH)
            except Exception:
                xray_version = None
        return {
            "isAlive": True,
            "isXrayOnline": self.core is not None and self.core.started,
            "xrayVersion": xray_version,
            "nodeVersion": version_check.CURRENT_VERSION,
        }

    @rpyc.exposed
    def get_system_stats(self) -> dict:
        return system_stats.get_system_stats()

    @rpyc.exposed
    def fetch_logs(self, callback: callable) -> XrayCoreLogsHandler:
        if self.core:
            logs = XrayCoreLogsHandler(self.core, callback)
            logs.exposed_stop = logs.stop
            logs.exposed_cast = logs.cast
            return logs
