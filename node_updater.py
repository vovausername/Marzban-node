"""Ask the host to update this marzban-node deployment to the latest image.

A Docker container cannot recreate itself: whatever process ran
`docker compose pull && up -d` from the inside would die with the old
container before the new one exists. So the actual update is performed by
a small host-side watcher (installed with `marzban-node.sh
install-updater`): this module only drops a request file into the data
directory that is bind-mounted from the host (`/var/lib/marzban-node`),
and the host's systemd path unit picks it up, pulls the new image and
recreates the container. The watcher reports back the same way, through a
result file it writes into the shared directory; after a successful update
the panel simply sees the new `nodeVersion` on /healthcheck once the
recreated container is up.
"""
import json
import os
import time
import uuid

import version_check
from config import NODE_UPDATE_SHARED_DIR
from logger import logger

REQUEST_FILE = os.path.join(NODE_UPDATE_SHARED_DIR, "node-update-request.json")
# The watcher renames the request to this the moment it starts working on
# it (an atomic mv), so a crash mid-update can never leave the request
# behind — a leftover request would re-trigger the systemd path unit in a
# loop.
WORKING_FILE = REQUEST_FILE + ".working"
RESULT_FILE = os.path.join(NODE_UPDATE_SHARED_DIR, "node-update-result.json")
# Dropped by `marzban-node.sh install-updater` so this process can tell
# whether anything on the host is actually listening for requests, instead
# of writing requests nobody will ever pick up.
UPDATER_MARKER_FILE = os.path.join(NODE_UPDATE_SHARED_DIR, ".node-updater-installed")


class NodeUpdateError(Exception):
    pass


def _read_json(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def updater_installed() -> bool:
    return os.path.isfile(UPDATER_MARKER_FILE)


def update_pending() -> bool:
    return os.path.isfile(REQUEST_FILE) or os.path.isfile(WORKING_FILE)


def request_update(force: bool = False) -> dict:
    """Schedule a node update by writing the request file for the host
    watcher. Raises NodeUpdateError if no watcher is installed, an update
    is already pending, or (unless `force`) there is nothing newer to
    update to.

    Returns the scheduled request. The caller should expect this whole
    service to go down shortly after: the watcher recreates the container,
    which restarts Xray and drops the panel's connection until the new
    container is up.
    """
    if not updater_installed():
        raise NodeUpdateError(
            "Host-side updater is not installed on this node. Run "
            "'sudo marzban-node install-updater' on the node's host first "
            "(see marzban-node.sh)."
        )
    if update_pending():
        raise NodeUpdateError("A node update is already pending or in progress.")

    check = version_check.check_for_update()
    if not force:
        if check["error"]:
            raise NodeUpdateError(
                f"Could not verify that an update exists: {check['error']}. "
                "Pass force=true to pull the latest published image anyway."
            )
        if not check["update_available"]:
            raise NodeUpdateError(
                f"Already on the latest version ({check['current_version']}). "
                "Pass force=true to re-pull and recreate anyway."
            )

    request = {
        "request_id": str(uuid.uuid4()),
        "requested_at": time.time(),
        "current_version": version_check.CURRENT_VERSION,
        # Informational only — the watcher always pulls the image tag the
        # compose file pins (":latest" for script-managed installs).
        "target_version": check["latest_version"],
        "force": force,
    }

    tmp_path = REQUEST_FILE + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(request, f)
        os.replace(tmp_path, REQUEST_FILE)
    except OSError as exc:
        raise NodeUpdateError(f"Failed to write update request: {exc}")

    logger.warning(
        f"Node update scheduled ({request['current_version']} -> "
        f"{request['target_version'] or 'latest'}); the container will be "
        "recreated by the host watcher shortly."
    )
    return {
        "status": "scheduled",
        "detail": "The host watcher will pull the new image and recreate "
                  "the container; this service and Xray will restart.",
        **request,
    }


def get_status() -> dict:
    """Report the updater's state: whether the host watcher is installed,
    whether a request is pending, and the outcome the watcher wrote for
    the last finished update (survives the container recreation since it
    lives in the bind-mounted data directory).
    """
    return {
        "updater_installed": updater_installed(),
        "update_pending": update_pending(),
        "pending_request": _read_json(REQUEST_FILE) or _read_json(WORKING_FILE),
        "last_result": _read_json(RESULT_FILE),
        "current_version": version_check.CURRENT_VERSION,
    }
