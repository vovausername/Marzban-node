from decouple import config
from dotenv import load_dotenv

load_dotenv()

SERVICE_HOST = config("SERVICE_HOST", default="0.0.0.0")
SERVICE_PORT = config('SERVICE_PORT', cast=int, default=62050)

XRAY_API_HOST = config("XRAY_API_HOST", default="0.0.0.0")
XRAY_API_PORT = config('XRAY_API_PORT', cast=int, default=62051)
XRAY_EXECUTABLE_PATH = config("XRAY_EXECUTABLE_PATH", default="/usr/local/bin/xray")
XRAY_ASSETS_PATH = config("XRAY_ASSETS_PATH", default="/usr/local/share/xray")

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
