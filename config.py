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

# Remote Xray binary updates replace the running proxy engine's executable —
# opt-in only, since a bad version/network blip during the swap is the
# highest-impact failure mode of anything this node exposes.
XRAY_REMOTE_UPDATE_ENABLED = config("XRAY_REMOTE_UPDATE_ENABLED", cast=bool, default=False)

# ipset/iptables IP blocking manipulates the *host's* firewall state and (per
# docker-compose.yml) requires NET_ADMIN/NET_RAW. Opt-in for the same reason
# as XRAY_REMOTE_UPDATE_ENABLED: this service can be run without
# SSL_CLIENT_CERT_FILE (explicitly supported, though discouraged — see
# main.py), in which case any peer that can reach it can call any exposed
# method. Without this gate, that peer could add arbitrary source addresses
# — including the panel's own — to the host's blocklist.
IP_BLOCK_ENABLED = config("IP_BLOCK_ENABLED", cast=bool, default=False)

# Repo whose GitHub Releases are checked for a newer marzban-node version.
# Only used to answer "is an update available" — this process never
# updates itself, since Docker deployments update by pulling a new image.
UPDATE_CHECK_REPO = config("UPDATE_CHECK_REPO", default="vovausername/Marzban-node")
