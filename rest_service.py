import asyncio
import json
import time
from uuid import UUID, uuid4

from fastapi import (APIRouter, Body, FastAPI, HTTPException, Request,
                     WebSocket, status)
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect

import ip_block
import node_updater
import system_stats
import version_check
import xray_hot_reload
import xray_updater
from config import (IP_BLOCK_ENABLED, NODE_REMOTE_UPDATE_ENABLED,
                    XRAY_ASSETS_PATH, XRAY_EXECUTABLE_PATH,
                    XRAY_HOT_RELOAD_ENABLED, XRAY_REMOTE_UPDATE_ENABLED)
from logger import logger
from xray import XRayConfig, XRayCore, wait_until_ready

app = FastAPI()


@app.exception_handler(RequestValidationError)
def validation_exception_handler(request: Request, exc: RequestValidationError):
    details = {}
    for error in exc.errors():
        details[error["loc"][-1]] = error.get("msg")
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=jsonable_encoder({"detail": details}),
    )


class Service(object):
    def __init__(self):
        self.router = APIRouter()

        self.connected = False
        self.client_ip = None
        self.session_id = None
        self.core = XRayCore(
            executable_path=XRAY_EXECUTABLE_PATH,
            assets_path=XRAY_ASSETS_PATH
        )
        self.core_version = self.core.get_version()
        self.config = None

        self.router.add_api_route("/", self.base, methods=["POST"])
        self.router.add_api_route("/ping", self.ping, methods=["POST"])
        self.router.add_api_route("/connect", self.connect, methods=["POST"])
        self.router.add_api_route("/disconnect", self.disconnect, methods=["POST"])
        self.router.add_api_route("/start", self.start, methods=["POST"])
        self.router.add_api_route("/stop", self.stop, methods=["POST"])
        self.router.add_api_route("/restart", self.restart, methods=["POST"])
        self.router.add_api_route("/block-ip", self.block_ip, methods=["POST"])
        self.router.add_api_route("/add-inbound", self.add_inbound, methods=["POST"])
        self.router.add_api_route("/add-outbound", self.add_outbound, methods=["POST"])
        self.router.add_api_route("/update-xray", self.update_xray, methods=["POST"])
        self.router.add_api_route("/check-for-update", self.check_for_update, methods=["POST"])
        self.router.add_api_route("/update-node", self.update_node, methods=["POST"])
        self.router.add_api_route("/update-node-status", self.update_node_status, methods=["POST"])
        self.router.add_api_route("/healthcheck", self.healthcheck, methods=["GET"])
        self.router.add_api_route("/system-stats", self.system_stats, methods=["POST"])

        self.router.add_websocket_route("/logs", self.logs)

    def match_session_id(self, session_id: UUID):
        if session_id != self.session_id:
            raise HTTPException(
                status_code=403,
                detail="Session ID mismatch."
            )
        return True

    def response(self, **kwargs):
        return {
            "connected": self.connected,
            "started": self.core.started,
            "core_version": self.core_version,
            **kwargs
        }

    def base(self):
        return self.response()

    def connect(self, request: Request):
        # core_lock: connect() can stop() a still-running core, which must
        # not interleave with another request's start()/stop()/restart()
        # touching the same core.process.
        with xray_hot_reload.core_lock:
            self.session_id = uuid4()
            self.client_ip = request.client.host

            if self.connected:
                logger.warning(
                    f'New connection from {self.client_ip}, Core control access was taken away from previous client.')
                if self.core.started:
                    try:
                        self.core.stop()
                    except RuntimeError:
                        pass

            self.connected = True
            logger.info(f'{self.client_ip} connected, Session ID = "{self.session_id}".')

            return self.response(
                session_id=self.session_id
            )

    def disconnect(self):
        with xray_hot_reload.core_lock:
            if self.connected:
                logger.info(f'{self.client_ip} disconnected, Session ID = "{self.session_id}".')

            self.session_id = None
            self.client_ip = None
            self.connected = False

            if self.core.started:
                try:
                    self.core.stop()
                except RuntimeError:
                    pass

            return self.response()

    def ping(self, session_id: UUID = Body(embed=True)):
        self.match_session_id(session_id)
        return {}

    def start(self, session_id: UUID = Body(embed=True), config: str = Body(embed=True)):
        self.match_session_id(session_id)

        try:
            config = XRayConfig(config, self.client_ip)
        except json.decoder.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "config": f'Failed to decode config: {exc}'
                }
            )

        with xray_hot_reload.core_lock:
            try:
                self.core.start(config)
                ready = wait_until_ready(self.core)
            except Exception as exc:
                logger.error(f"Failed to start core: {exc}")
                raise HTTPException(
                    status_code=503,
                    detail=str(exc)
                )

            if not ready:
                with self.core.get_logs() as logs:
                    last_log = logs[-1] if logs else ''
                raise HTTPException(
                    status_code=503,
                    detail=last_log
                )

            self.core.config = config
            return self.response()

    def stop(self, session_id: UUID = Body(embed=True)):
        self.match_session_id(session_id)

        with xray_hot_reload.core_lock:
            try:
                self.core.stop()
            except RuntimeError:
                pass

            return self.response()

    def restart(self, session_id: UUID = Body(embed=True), config: str = Body(embed=True)):
        self.match_session_id(session_id)

        try:
            config = XRayConfig(config, self.client_ip)
        except json.decoder.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "config": f'Failed to decode config: {exc}'
                }
            )

        # core_lock spans the hot-reload attempt, the fallback full
        # restart and the core.config update: overlapping requests
        # must not diff against a core.config another thread is still
        # bringing in sync with the live process.
        with xray_hot_reload.core_lock:
            # Hot path: identical config -> no-op; only client lists
            # changed -> applied to the live core without dropping user
            # connections (core.config updated inside). Any other
            # difference (or hot-path error) falls through to the full
            # restart below.
            if xray_hot_reload.try_hot_reload(self.core, config):
                return self.response()

            try:
                self.core.restart(config)
                ready = wait_until_ready(self.core)
            except Exception as exc:
                logger.error(f"Failed to restart core: {exc}")
                raise HTTPException(
                    status_code=503,
                    detail=str(exc)
                )

            if not ready:
                with self.core.get_logs() as logs:
                    last_log = logs[-1] if logs else ''
                raise HTTPException(
                    status_code=503,
                    detail=last_log
                )

            self.core.config = config
            return self.response()

    def block_ip(
        self,
        session_id: UUID = Body(embed=True),
        ip: str = Body(embed=True),
        minutes: float = Body(embed=True),
    ):
        self.match_session_id(session_id)
        if not IP_BLOCK_ENABLED:
            raise HTTPException(
                status_code=403,
                detail="IP blocking is disabled. Set IP_BLOCK_ENABLED=true to allow it.",
            )
        try:
            return ip_block.block_ip(ip, minutes)
        except ip_block.IpBlockError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    def add_inbound(self, session_id: UUID = Body(embed=True), inbound: dict = Body(embed=True)):
        self.match_session_id(session_id)
        if not XRAY_HOT_RELOAD_ENABLED:
            raise HTTPException(
                status_code=403,
                detail="Xray hot reload is disabled. Set XRAY_HOT_RELOAD_ENABLED=true to allow it.",
            )
        # core_lock: `xray api adi` talks to the live process over its
        # loopback API, which must not be dialed mid-stop/restart while
        # another request is bringing the process down or back up.
        with xray_hot_reload.core_lock:
            try:
                output = xray_hot_reload.add_inbound(inbound)
            except xray_hot_reload.HotReloadError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            return self.response(detail=output.strip())

    def add_outbound(self, session_id: UUID = Body(embed=True), outbound: dict = Body(embed=True)):
        self.match_session_id(session_id)
        if not XRAY_HOT_RELOAD_ENABLED:
            raise HTTPException(
                status_code=403,
                detail="Xray hot reload is disabled. Set XRAY_HOT_RELOAD_ENABLED=true to allow it.",
            )
        with xray_hot_reload.core_lock:
            try:
                output = xray_hot_reload.add_outbound(outbound)
            except xray_hot_reload.HotReloadError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            return self.response(detail=output.strip())

    def update_xray(self, session_id: UUID = Body(embed=True), version: str = Body(embed=True)):
        self.match_session_id(session_id)
        if not XRAY_REMOTE_UPDATE_ENABLED:
            raise HTTPException(
                status_code=403,
                detail="Remote Xray updates are disabled. Set XRAY_REMOTE_UPDATE_ENABLED=true to allow them.",
            )
        # Download + verify without core_lock: pure network I/O and local
        # checks, independent of the running core. Holding the lock for
        # as long as GitHub takes to respond would block every other
        # core-mutating request (connect/disconnect/start/stop/restart)
        # for the same duration, even though none of them touch anything
        # this step reads or writes.
        try:
            prepared = xray_updater.fetch_and_verify(version)
        except xray_updater.XrayUpdateError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # core_lock: applying the update stops and restarts self.core
        # internally, same as restart() — it must not interleave with a
        # concurrent connect/disconnect/start/stop/restart touching the
        # same core.process.
        with xray_hot_reload.core_lock:
            try:
                result = xray_updater.apply(prepared, self.core)
            except xray_updater.XrayUpdateError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            self.core_version = self.core.version
            return result

    def check_for_update(self, session_id: UUID = Body(embed=True)):
        self.match_session_id(session_id)
        return version_check.check_for_update()

    def update_node(self, session_id: UUID = Body(embed=True), force: bool = Body(False, embed=True)):
        self.match_session_id(session_id)
        if not NODE_REMOTE_UPDATE_ENABLED:
            raise HTTPException(
                status_code=403,
                detail="Remote node updates are disabled. Set NODE_REMOTE_UPDATE_ENABLED=true to allow them.",
            )
        try:
            return node_updater.request_update(force=force)
        except node_updater.NodeUpdateError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    def update_node_status(self, session_id: UUID = Body(embed=True)):
        self.match_session_id(session_id)
        return node_updater.get_status()

    def healthcheck(self):
        # No session_id on purpose: this is for monitoring, and mTLS alone
        # already gates it (uvicorn runs with ssl_cert_reqs=2).
        return {
            "isAlive": True,
            "isXrayOnline": self.core.started,
            "xrayVersion": self.core_version,
            "nodeVersion": version_check.CURRENT_VERSION,
        }

    def system_stats(self, session_id: UUID = Body(embed=True)):
        self.match_session_id(session_id)
        return system_stats.get_system_stats()

    async def logs(self, websocket: WebSocket):
        session_id = websocket.query_params.get('session_id')
        interval = websocket.query_params.get('interval')

        try:
            session_id = UUID(session_id)
            if session_id != self.session_id:
                return await websocket.close(reason="Session ID mismatch.", code=4403)

        except ValueError:
            return await websocket.close(reason="session_id should be a valid UUID.", code=4400)

        if interval:
            try:
                interval = float(interval)

            except ValueError:
                return await websocket.close(reason="Invalid interval value.", code=4400)

            if interval > 10:
                return await websocket.close(reason="Interval must be more than 0 and at most 10 seconds.", code=4400)

        await websocket.accept()

        cache = ''
        last_sent_ts = 0
        with self.core.get_logs() as logs:
            while session_id == self.session_id:
                if interval and time.time() - last_sent_ts >= interval and cache:
                    try:
                        await websocket.send_text(cache)
                    except (WebSocketDisconnect, RuntimeError):
                        break
                    cache = ''
                    last_sent_ts = time.time()

                if not logs:
                    try:
                        await asyncio.wait_for(websocket.receive(), timeout=0.2)
                        continue
                    except asyncio.TimeoutError:
                        continue
                    except (WebSocketDisconnect, RuntimeError):
                        break

                log = logs.popleft()

                if interval:
                    cache += f'{log}\n'
                    continue

                try:
                    await websocket.send_text(log)
                except (WebSocketDisconnect, RuntimeError):
                    break

        await websocket.close()


service = Service()
app.include_router(service.router)
