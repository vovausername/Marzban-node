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
prompts) whether to enable this fork's two opt-in remote-control triggers:
remote Xray updates and temporary IP blocking. Both default to **no** — see
`.env.example` for the underlying `XRAY_REMOTE_UPDATE_ENABLED` /
`IP_BLOCK_ENABLED` variables if you want to turn them on later by hand.

Use `help` to view all commands:
```marzban-node help```


## Manual install
Read the setup guide here: https://gozargah.github.io/marzban/docs/marzban-node