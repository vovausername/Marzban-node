# Marzban-node

This is [vovausername/Marzban-node](https://github.com/vovausername/Marzban-node), a fork of Gozargah/Marzban-node kept in sync with upstream and extended with remote-control triggers the panel can send: temporary OS-level IP blocking, targeted Xray binary updates, and node-update availability checks.

## Quick install
Install Marzban-node on your server using this command
```bash
sudo bash -c "$(curl -sL https://github.com/vovausername/Marzban-node/raw/master/marzban-node.sh)" @ install
```
Install Marzban-node on your server using this command with custom name:
```bash
sudo bash -c "$(curl -sL https://github.com/vovausername/Marzban-node/raw/master/marzban-node.sh)" @ install --name marzban-node2
```
Or you can only install this script (marzban-node command) on your server by using this command
```bash
sudo bash -c "$(curl -sL https://github.com/vovausername/Marzban-node/raw/master/marzban-node.sh)" @ install-script
```

During install, you'll be asked (in addition to the usual protocol/cert/port
prompts) whether to enable this fork's two remote-control triggers: remote
Xray updates and temporary IP blocking. Both default to **yes** — see
`.env.example` for the underlying `XRAY_REMOTE_UPDATE_ENABLED` /
`IP_BLOCK_ENABLED` variables if you want to turn either off later by hand.
Both require the node to actually be running with a client certificate
(`SSL_CLIENT_CERT_FILE`) to be safe — the node logs a loud warning on
startup if either is enabled without one.

Use `help` to view all commands:
```marzban-node help```

## Migrating an existing node from upstream

If you already have a node installed from the original
`Gozargah/Marzban-scripts` installer (pointing at `gozargah/marzban-node`),
switch it to this fork in place with:
```bash
sudo bash -c "$(curl -sL https://github.com/vovausername/Marzban-node/raw/master/marzban-node.sh)" @ migrate
```
This rewrites the node's existing `docker-compose.yml` to use this fork's
image, asks whether to turn on the two remote-control triggers (adding
`NET_ADMIN`/`NET_RAW` if you say yes), pulls the new image, and restarts —
without touching your existing certificate, ports, or protocol choice.
Re-running `migrate` at any time is safe (it detects it's already on this
fork's image and just leaves it alone).

## Manual install
Read the setup guide here: https://gozargah.github.io/marzban/docs/marzban-node