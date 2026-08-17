# Marzban-node

This is [vovausername/Marzban-node](https://github.com/vovausername/Marzban-node), a fork of Gozargah/Marzban-node kept in sync with upstream and extended with remote-control triggers the panel can send: temporary OS-level IP blocking, targeted Xray binary updates, node-update availability checks, and panel-triggered updates of the node itself.

## Quick install
Install Marzban-node on your server using this command
```bash
curl -sL https://github.com/vovausername/Marzban-node/raw/master/marzban-node.sh | sudo bash -s -- install
```
Install Marzban-node on your server using this command with custom name:
```bash
curl -sL https://github.com/vovausername/Marzban-node/raw/master/marzban-node.sh | sudo bash -s -- install --name marzban-node2
```
Or you can only install this script (marzban-node command) on your server by using this command
```bash
curl -sL https://github.com/vovausername/Marzban-node/raw/master/marzban-node.sh | sudo bash -s -- install-script
```

Piping straight into `bash -s --` (instead of substituting the whole
downloaded script into a `bash -c "$(...)"` argument) avoids `Argument list
too long` failures that `$(...)` substitution can hit on shells/hosts with a
small `ARG_MAX` or a large existing environment. `sudo` wraps `bash`, not
`curl`, so the download itself doesn't need root.

During install, you'll be asked (in addition to the usual protocol/cert/port
prompts) whether to enable this fork's remote-control triggers: remote
Xray updates, temporary IP blocking, and panel-triggered updates of the
node itself (which also installs a small host-side watcher — see below).
All default to **yes** — see `.env.example` for the underlying
`XRAY_REMOTE_UPDATE_ENABLED` / `IP_BLOCK_ENABLED` /
`NODE_REMOTE_UPDATE_ENABLED` variables if you want to turn any off later
by hand. All of them require the node to actually be running with a client
certificate (`SSL_CLIENT_CERT_FILE`) to be safe — the node logs a loud
warning on startup if any is enabled without one.

Use `help` to view all commands:
```marzban-node help```

## Migrating an existing node from upstream

If you already have a node installed from the original
`Gozargah/Marzban-scripts` installer (pointing at `gozargah/marzban-node`),
switch it to this fork in place with:
```bash
curl -sL https://github.com/vovausername/Marzban-node/raw/master/marzban-node.sh | sudo bash -s -- migrate
```
This rewrites the node's existing `docker-compose.yml` to use this fork's
image, asks whether to turn on the remote-control triggers (adding
`NET_ADMIN`/`NET_RAW` if you say yes) and whether to install the host-side
watcher for panel-triggered node updates, pulls the new image, and restarts —
without touching your existing certificate, ports, or protocol choice.
Re-running `migrate` at any time is safe (it detects it's already on this
fork's image and just leaves it alone).

## Updating the node from the panel

The node can't update itself from inside its container — recreating the
container would kill the very process doing it. Instead, the update is
split in two:

1. **In the container**: the panel calls `POST /update-node` (REST, with
   its `session_id`; RPyC: `update_node()`). The node verifies a newer
   release actually exists (pass `force: true` to skip that and re-pull
   anyway) and drops a request file into `/var/lib/marzban-node`, which is
   bind-mounted from the host.
2. **On the host**: a small systemd path unit notices the request, runs
   `docker compose pull` + `up -d --force-recreate`, and writes the outcome
   to a result file. You almost never install it by hand: `install` and
   `migrate` offer it, and every `marzban-node update` installs/refreshes
   it automatically (unless the compose file says
   `NODE_REMOTE_UPDATE_ENABLED: "false"`). After each successful
   panel-triggered update the watcher also regenerates itself from the
   freshly updated CLI, so watcher improvements roll out with normal node
   updates — no per-node commands ever again. `sudo marzban-node
   install-updater` / `uninstall-updater` exist for doing it explicitly.

### Rolling this out to nodes you already have

Existing nodes need one last manual update to pick this feature up (their
installed CLI predates it, so run the one-liner, which always executes the
latest script):
```bash
curl -sL https://github.com/vovausername/Marzban-node/raw/master/marzban-node.sh | sudo bash -s -- update
```
That single command updates the node **and** installs the watcher. From
then on, every update — of the node and of the watcher itself — can be
triggered from the panel.

The panel can poll `POST /update-node-status` (RPyC:
`update_node_status()`) to see whether a request is pending and how the
last update ended, and `GET /healthcheck` reports the new `nodeVersion`
once the recreated container is back up. Expect a short outage during the
recreation: Xray restarts and the panel has to reconnect.

If the watcher was never installed on the host, `/update-node` fails with
a clear error instead of writing a request nobody will pick up. To opt out
of the whole feature set `NODE_REMOTE_UPDATE_ENABLED: "false"` (and/or run
`marzban-node uninstall-updater`). As with the other remote-control
triggers, only expose this on nodes that authenticate the panel with
`SSL_CLIENT_CERT_FILE`.

## Manual install
Read the setup guide here: https://gozargah.github.io/marzban/docs/marzban-node