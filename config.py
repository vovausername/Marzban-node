import os
import shutil
import stat

from decouple import config
from dotenv import load_dotenv

load_dotenv()

SERVICE_HOST = config("SERVICE_HOST", default="0.0.0.0")
SERVICE_PORT = config('SERVICE_PORT', cast=int, default=62050)

XRAY_API_HOST = config("XRAY_API_HOST", default="0.0.0.0")
XRAY_API_PORT = config('XRAY_API_PORT', cast=int, default=62051)

# Defaults to a path under the bind-mounted /var/lib/marzban-node volume
# (see docker-compose.yml), not the image's own /usr/local/bin/xray, so a
# version installed through the panel's remote Xray update (xray_updater.py,
# which replaces whatever binary sits at this path) survives container
# recreation. Without this, a panel-triggered node self-update
# (node_updater.py's `docker compose pull && up -d --force-recreate`)
# starts a brand-new container whose /usr/local/bin/xray is whatever was
# baked into the image at build time, silently reverting any Xray version
# picked through the panel. _seed_persistent_xray_binary() below populates
# this path from the image-baked binary the first time it's empty (fresh
# volume), so this works out of the box with no manual step required.
XRAY_EXECUTABLE_PATH = config("XRAY_EXECUTABLE_PATH", default="/var/lib/marzban-node/xray-core/xray")
XRAY_ASSETS_PATH = config("XRAY_ASSETS_PATH", default="/usr/local/share/xray")

_IMAGE_BAKED_XRAY_EXECUTABLE_PATH = "/usr/local/bin/xray"

# Marks the file at XRAY_EXECUTABLE_PATH as one _seed_persistent_xray_binary()
# put there itself, as opposed to one explicitly installed by the panel's
# remote Xray update (xray_updater.apply() deletes this the moment it installs
# a panel-picked version). While the marker is present, every startup re-seeds
# from the image-baked binary, so a plain image upgrade still brings whatever
# newer Xray release it bundles — matching the old (pre-persistence) behavior
# — right up until an operator actually picks a version through the panel,
# after which that explicit choice is left alone.
XRAY_EXECUTABLE_SEEDED_MARKER = XRAY_EXECUTABLE_PATH + ".seeded-from-image"


def _install_baked_xray_binary() -> None:
    # Copy to a temp file in the same directory, then rename into place, so a
    # copy interrupted partway (e.g. a full volume) can never leave a
    # truncated binary sitting at XRAY_EXECUTABLE_PATH — os.replace() is
    # atomic within the same filesystem and only ever swaps in a complete file.
    os.makedirs(os.path.dirname(XRAY_EXECUTABLE_PATH), exist_ok=True)
    tmp_path = XRAY_EXECUTABLE_PATH + ".seed-tmp"
    shutil.copy2(_IMAGE_BAKED_XRAY_EXECUTABLE_PATH, tmp_path)
    st = os.stat(tmp_path)
    os.chmod(tmp_path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(tmp_path, XRAY_EXECUTABLE_PATH)
    with open(XRAY_EXECUTABLE_SEEDED_MARKER, "w"):
        pass


def _seed_persistent_xray_binary() -> None:
    if os.path.abspath(XRAY_EXECUTABLE_PATH) == os.path.abspath(_IMAGE_BAKED_XRAY_EXECUTABLE_PATH):
        return
    if not os.path.isfile(_IMAGE_BAKED_XRAY_EXECUTABLE_PATH):
        return
    if os.path.isfile(XRAY_EXECUTABLE_PATH) and not os.path.isfile(XRAY_EXECUTABLE_SEEDED_MARKER):
        # An explicitly installed binary (panel update or manual install)
        # lives here; never overwrite an operator's choice.
        return
    try:
        _install_baked_xray_binary()
    except OSError as exc:
        print(f"WARNING: could not seed {XRAY_EXECUTABLE_PATH} from {_IMAGE_BAKED_XRAY_EXECUTABLE_PATH}: {exc}")


_seed_persistent_xray_binary()

SSL_CERT_FILE = config("SSL_CERT_FILE", default="/var/lib/marzban-node/ssl_cert.pem")
SSL_KEY_FILE = config("SSL_KEY_FILE", default="/var/lib/marzban-node/ssl_key.pem")
SSL_CLIENT_CERT_FILE = config("SSL_CLIENT_CERT_FILE", default="")

DEBUG = config("DEBUG", cast=bool, default=False)

SERVICE_PROTOCOL = config('SERVICE_PROTOCOL', cast=str, default='rest')

INBOUNDS = config("INBOUNDS", cast=lambda v: [x.strip() for x in v.split(',')] if v else [], default="")

# Remote Xray binary updates replace the running proxy engine's executable.
# On by default in this fork; set to false to opt out. A bad version/network
# blip during the swap is the highest-impact failure mode of anything this
# node exposes, and xray_updater.py backs up + verifies + rolls back around
# it, but the safety net only matters if you're aware this can happen.
XRAY_REMOTE_UPDATE_ENABLED = config("XRAY_REMOTE_UPDATE_ENABLED", cast=bool, default=True)

# ipset/iptables IP blocking manipulates the *host's* firewall state and (per
# docker-compose.yml) requires NET_ADMIN/NET_RAW — without those capabilities
# granted to the container, calls to it simply fail cleanly. On by default in
# this fork; set to false to opt out. This service can be run without
# SSL_CLIENT_CERT_FILE (explicitly supported, though discouraged — see
# main.py, which now warns loudly if that's the case while this is on), in
# which case any peer that can reach it could add arbitrary source addresses
# — including the panel's own — to the host's blocklist.
IP_BLOCK_ENABLED = config("IP_BLOCK_ENABLED", cast=bool, default=True)

# Apply user (client) additions/removals from the panel's restart calls to
# the live Xray process via its HandlerService API instead of restarting the
# whole core — a full restart drops every active user connection. Only a
# config whose SOLE difference from the running one is the inbound client
# lists is applied hot; an identical config becomes a no-op, and anything
# else (ports, stream settings, routing, ...) or any error in the hot path
# falls back to the plain full restart, so the node can never keep running
# a config the panel didn't send. Hot-applying needs an Xray binary >=
# v25.7.26 (first release shipping `xray api adu`/`rmu`); older binaries
# are detected and silently keep the full-restart behavior. On by default
# in this fork; set to false to opt out.
XRAY_HOT_RELOAD_ENABLED = config("XRAY_HOT_RELOAD_ENABLED", cast=bool, default=True)

# Port of the loopback-only plaintext Xray API inbound injected alongside
# the panel's TLS one when hot reload is enabled — the `xray api` CLI only
# speaks plaintext gRPC, so it can't use the panel's inbound. Always bound
# to 127.0.0.1, but since the container runs with network_mode: host that
# is the host's loopback: change this if 62052 collides with something.
XRAY_LOCAL_API_PORT = config("XRAY_LOCAL_API_PORT", cast=int, default=62052)

# Timeout (seconds) for `xray api adi`/`api ado` — hot-adding a single
# inbound/outbound to the running Xray process without a restart, exposed
# via rest_service's /add-inbound and /add-outbound (and rpyc_service's
# equivalents). Higher than adu/rmu's fixed 3s since adi/ado build a full
# protocol handler (e.g. TLS certs) before the RPC returns. Reuses
# XRAY_HOT_RELOAD_ENABLED as its on/off switch since both features depend
# on the same loopback plaintext API inbound.
XRAY_HOT_ADD_TIMEOUT_SECONDS = config("XRAY_HOT_ADD_TIMEOUT_SECONDS", cast=int, default=10)

# How long to watch a freshly (re)started Xray process before trusting that
# it actually stayed up, and how often to poll it while watching. Used by
# xray.wait_until_ready() — the single place both service handlers and
# xray_updater.py confirm a start/restart succeeded, replacing the old
# per-file hardcoded 3-second windows and fragile stdout log-scanning.
XRAY_START_CONFIRM_SECONDS = config("XRAY_START_CONFIRM_SECONDS", cast=float, default=3.0)
XRAY_START_POLL_INTERVAL = config("XRAY_START_POLL_INTERVAL", cast=float, default=0.1)

# Repo whose GitHub Releases are checked for a newer marzban-node version.
# Used both to answer "is an update available" and as the guard for
# panel-triggered node updates (node_updater.py refuses to schedule one,
# unless forced, when this repo shows nothing newer).
UPDATE_CHECK_REPO = config("UPDATE_CHECK_REPO", default="vovausername/Marzban-node")

# Let the panel trigger an update of marzban-node itself. The container
# can't recreate itself, so this only writes a request file into
# NODE_UPDATE_SHARED_DIR; the actual `docker compose pull && up` is done by
# a host-side systemd watcher installed with `marzban-node.sh
# install-updater` — without that watcher the request is refused with a
# clear error, so this flag is inert on hosts that never opted in. On by
# default in this fork; set to false to opt out.
NODE_REMOTE_UPDATE_ENABLED = config("NODE_REMOTE_UPDATE_ENABLED", cast=bool, default=True)

# Directory shared between this container and the host (the bind mount in
# docker-compose.yml) through which node update requests/results are
# exchanged with the host watcher. Must match the directory the watcher
# monitors — for script-managed installs that's /var/lib/<app name> on the
# host, mounted here as /var/lib/marzban-node.
NODE_UPDATE_SHARED_DIR = config("NODE_UPDATE_SHARED_DIR", default="/var/lib/marzban-node")
