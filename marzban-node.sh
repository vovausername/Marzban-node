#!/usr/bin/env bash
# Install/management script for vovausername/Marzban-node — a fork of
# Gozargah/Marzban-node kept in sync with upstream and extended with
# remote-control triggers the panel can send: temporary OS-level IP
# blocking, targeted Xray binary updates, and node-update checks.
# Adapted from Gozargah/Marzban-scripts' marzban-node.sh.
set -e


while [[ $# -gt 0 ]]; do
    key="$1"
    
    case $key in
        install|update|migrate|uninstall|up|down|restart|status|logs|core-update|install-script|uninstall-script|install-updater|uninstall-updater|edit)
            COMMAND="$1"
            shift # past argument
        ;;
        --name)
            if [[ "$COMMAND" == "install" || "$COMMAND" == "install-script" ]]; then
                APP_NAME="$2"
                shift # past argument
            else
                echo "Error: --name parameter is only allowed with 'install' or 'install-script' commands."
                exit 1
            fi
            shift # past value
        ;;
        -y|--yes)
            ASSUME_YES="true"
            shift # past argument
        ;;
        *)
            shift # past unknown argument
        ;;
    esac
done

# Fetch IP address from ipinfo.io API
NODE_IP=$(curl -s -4 ifconfig.io)

# If the IPv4 retrieval is empty, attempt to retrieve the IPv6 address
if [ -z "$NODE_IP" ]; then
    NODE_IP=$(curl -s -6 ifconfig.io)
fi

if [[ "$COMMAND" == "install" || "$COMMAND" == "install-script" ]] && [ -z "$APP_NAME" ]; then
    APP_NAME="marzban-node"
fi
# Set script name if APP_NAME is not set. basename "$0" only reflects a real
# custom install name when this file is invoked by its installed path/name
# (e.g. running the installed `marzban-node2` command after `install
# --name marzban-node2`). Piped one-liners like
# `curl ... | bash -s -- migrate` leave $0 as the literal "bash", which
# isn't an install name at all — falling back to it here broke every piped
# management command except install/install-script (they get APP_NAME above
# regardless). Only trust the basename guess when a matching /opt install
# directory actually exists for it; otherwise use the default name.
if [ -z "$APP_NAME" ]; then
    SCRIPT_NAME=$(basename "$0")
    GUESSED_APP_NAME="${SCRIPT_NAME%.*}"
    if [ -d "/opt/$GUESSED_APP_NAME" ]; then
        APP_NAME="$GUESSED_APP_NAME"
    else
        APP_NAME="marzban-node"
    fi
fi

INSTALL_DIR="/opt"

if [ -d "$INSTALL_DIR/$APP_NAME" ]; then
    APP_DIR="$INSTALL_DIR/$APP_NAME"
elif [ -d "$INSTALL_DIR/Marzban-node" ]; then
    APP_DIR="$INSTALL_DIR/Marzban-node"
else
    APP_DIR="$INSTALL_DIR/$APP_NAME"
fi

DATA_DIR="/var/lib/$APP_NAME"
DATA_MAIN_DIR="/var/lib/$APP_NAME"
COMPOSE_FILE="$APP_DIR/docker-compose.yml"
LAST_XRAY_CORES=5
CERT_FILE="$DATA_DIR/cert.pem"
FETCH_REPO="vovausername/Marzban-node"
SCRIPT_URL="https://github.com/$FETCH_REPO/raw/master/marzban-node.sh"

# Panel-triggered node self-update bridge (see node_updater.py in the
# repo): the container drops a request file into $DATA_DIR (bind-mounted
# as /var/lib/marzban-node inside), and a host-side systemd path unit —
# installed by the `install-updater` command — picks it up and runs the
# updater script below, which pulls the new image and recreates the
# container. The marker file tells the container a watcher is listening.
UPDATER_SCRIPT="/usr/local/bin/$APP_NAME-updater"
UPDATER_SERVICE_UNIT="$APP_NAME-updater.service"
UPDATER_PATH_UNIT="$APP_NAME-updater.path"
UPDATE_REQUEST_FILE="$DATA_DIR/node-update-request.json"
UPDATE_RESULT_FILE="$DATA_DIR/node-update-result.json"
UPDATER_MARKER_FILE="$DATA_DIR/.node-updater-installed"

colorized_echo() {
    local color=$1
    local text=$2
    local style=${3:-0}  # Default style is normal

    case $color in
        "red")
            printf "\e[${style};91m${text}\e[0m\n"
        ;;
        "green")
            printf "\e[${style};92m${text}\e[0m\n"
        ;;
        "yellow")
            printf "\e[${style};93m${text}\e[0m\n"
        ;;
        "blue")
            printf "\e[${style};94m${text}\e[0m\n"
        ;;
        "magenta")
            printf "\e[${style};95m${text}\e[0m\n"
        ;;
        "cyan")
            printf "\e[${style};96m${text}\e[0m\n"
        ;;
        *)
            echo "${text}"
        ;;
    esac
}

check_running_as_root() {
    if [ "$(id -u)" != "0" ]; then
        colorized_echo red "This command must be run as root."
        exit 1
    fi
}

detect_os() {
    # Detect the operating system
    if [ -f /etc/lsb-release ]; then
        OS=$(lsb_release -si)
        elif [ -f /etc/os-release ]; then
        OS=$(awk -F= '/^NAME/{print $2}' /etc/os-release | tr -d '"')
        elif [ -f /etc/redhat-release ]; then
        OS=$(cat /etc/redhat-release | awk '{print $1}')
        elif [ -f /etc/arch-release ]; then
        OS="Arch"
    else
        colorized_echo red "Unsupported operating system"
        exit 1
    fi
}

detect_and_update_package_manager() {
    colorized_echo blue "Updating package manager"
    if [[ "$OS" == "Ubuntu"* ]] || [[ "$OS" == "Debian"* ]]; then
        PKG_MANAGER="apt-get"
        $PKG_MANAGER update -qq >/dev/null 2>&1
    elif [[ "$OS" == "CentOS"* ]] || [[ "$OS" == "AlmaLinux"* ]]; then
        PKG_MANAGER="yum"
        $PKG_MANAGER update -y -q >/dev/null 2>&1
        $PKG_MANAGER install -y -q epel-release >/dev/null 2>&1
    elif [[ "$OS" == "Fedora"* ]]; then
        PKG_MANAGER="dnf"
        $PKG_MANAGER update -q -y >/dev/null 2>&1
    elif [[ "$OS" == "Arch"* ]]; then
        PKG_MANAGER="pacman"
        $PKG_MANAGER -Sy --noconfirm --quiet >/dev/null 2>&1
    elif [[ "$OS" == "openSUSE"* ]]; then
        PKG_MANAGER="zypper"
        $PKG_MANAGER refresh --quiet >/dev/null 2>&1
    else
        colorized_echo red "Unsupported operating system"
        exit 1
    fi
}


detect_compose() {
    # Check if docker compose command exists
    if docker compose >/dev/null 2>&1; then
        COMPOSE='docker compose'
        elif docker-compose >/dev/null 2>&1; then
        COMPOSE='docker-compose'
    else
        colorized_echo red "docker compose not found"
        exit 1
    fi
}

install_package () {
    if [ -z "$PKG_MANAGER" ]; then
        detect_and_update_package_manager
    fi

    PACKAGE=$1
    colorized_echo blue "Installing $PACKAGE"
    if [[ "$OS" == "Ubuntu"* ]] || [[ "$OS" == "Debian"* ]]; then
        $PKG_MANAGER -y -qq install "$PACKAGE" >/dev/null 2>&1
    elif [[ "$OS" == "CentOS"* ]] || [[ "$OS" == "AlmaLinux"* ]]; then
        $PKG_MANAGER install -y -q "$PACKAGE" >/dev/null 2>&1
    elif [[ "$OS" == "Fedora"* ]]; then
        $PKG_MANAGER install -y -q "$PACKAGE" >/dev/null 2>&1
    elif [[ "$OS" == "Arch"* ]]; then
        $PKG_MANAGER -S --noconfirm --quiet "$PACKAGE" >/dev/null 2>&1
    elif [[ "$OS" == "openSUSE"* ]]; then
        PKG_MANAGER="zypper"
        $PKG_MANAGER --quiet install -y "$PACKAGE" >/dev/null 2>&1
    else
        colorized_echo red "Unsupported operating system"
        exit 1
    fi
}

install_docker() {
    # Install Docker and Docker Compose using the official installation script
    colorized_echo blue "Installing Docker"
    curl -fsSL https://get.docker.com | sh
    colorized_echo green "Docker installed successfully"
}

install_marzban_node_script() {
    colorized_echo blue "Installing marzban script"
    TARGET_PATH="/usr/local/bin/$APP_NAME"
    curl -sSL $SCRIPT_URL -o $TARGET_PATH
    
    sed -i "s/^APP_NAME=.*/APP_NAME=\"$APP_NAME\"/" $TARGET_PATH
    
    chmod 755 $TARGET_PATH
    colorized_echo green "Marzban-node script installed successfully at $TARGET_PATH"
}

# Get a list of occupied ports
get_occupied_ports() {
    if command -v ss &>/dev/null; then
        OCCUPIED_PORTS=$(ss -tuln | awk '{print $5}' | grep -Eo '[0-9]+$' | sort | uniq)
    elif command -v netstat &>/dev/null; then
        OCCUPIED_PORTS=$(netstat -tuln | awk '{print $4}' | grep -Eo '[0-9]+$' | sort | uniq)
    else
        colorized_echo yellow "Neither ss nor netstat found. Attempting to install net-tools."
        detect_os
        install_package net-tools
        if command -v netstat &>/dev/null; then
            OCCUPIED_PORTS=$(netstat -tuln | awk '{print $4}' | grep -Eo '[0-9]+$' | sort | uniq)
        else
            colorized_echo red "Failed to install net-tools. Please install it manually."
            exit 1
        fi
    fi
}

# Function to check if a port is occupied
is_port_occupied() {
    if echo "$OCCUPIED_PORTS" | grep -q -w "$1"; then
        return 0
    else
        return 1
    fi
}

install_marzban_node() {
    # Fetch releases
    mkdir -p "$DATA_DIR"
    mkdir -p "$APP_DIR"
    mkdir -p "$DATA_MAIN_DIR"
    
    # Проверка на существование файла перед его очисткой
    if [ -f "$CERT_FILE" ]; then
        >"$CERT_FILE"
    fi
    
    # Function to print information to the user
    print_info() {
        echo -e "\033[1;34m$1\033[0m"
    }
    
    # Prompt the user to input the certificate
    echo -e "Please paste the content of the Client Certificate, press ENTER on a new line when finished: "
    
    while IFS= read -r line; do
        if [[ -z $line ]]; then
            break
        fi
        echo "$line" >>"$CERT_FILE"
    done
    
    print_info "Certificate saved to $CERT_FILE"
    
    # Prompt the user to choose REST or another protocol
    read -p "Do you want to use REST protocol? (Y/n): " -r use_rest

    # Default to "Y" if the user just presses ENTER
    if [[ -z "$use_rest" || "$use_rest" =~ ^[Yy]$ ]]; then
        USE_REST=true
    else
        USE_REST=false
    fi

    # This fork's two remote-control triggers, on by default (matching
    # config.py): XRAY_REMOTE_UPDATE_ENABLED lets the panel replace this
    # node's Xray binary; IP_BLOCK_ENABLED lets it add temporary
    # ipset/iptables DROP rules on this host (requires NET_ADMIN/NET_RAW,
    # added to the compose file below unless you opt out of IP blocking).
    read -p "Allow the panel to remotely update this node's Xray version? (Y/n): " -r allow_xray_update
    if [[ -z "$allow_xray_update" || "$allow_xray_update" =~ ^[Yy]$ ]]; then
        XRAY_REMOTE_UPDATE_ENABLED=true
    else
        XRAY_REMOTE_UPDATE_ENABLED=false
    fi

    read -p "Allow the panel to temporarily block IPs on this node's firewall? (Y/n): " -r allow_ip_block
    if [[ -z "$allow_ip_block" || "$allow_ip_block" =~ ^[Yy]$ ]]; then
        IP_BLOCK_ENABLED=true
    else
        IP_BLOCK_ENABLED=false
    fi

    # Panel-triggered update of the node itself. The env var only makes the
    # node accept the request; the actual pull/recreate is done by the
    # host-side watcher install_command installs when this is enabled.
    read -p "Allow the panel to update this node itself (installs a host-side watcher)? (Y/n): " -r allow_node_update
    if [[ -z "$allow_node_update" || "$allow_node_update" =~ ^[Yy]$ ]]; then
        NODE_REMOTE_UPDATE_ENABLED=true
    else
        NODE_REMOTE_UPDATE_ENABLED=false
    fi

    get_occupied_ports
    
    # Prompt the user to enter ports with occupation check
    while true; do
        read -p "Enter the SERVICE_PORT (default 62050): " -r SERVICE_PORT
        if [[ -z "$SERVICE_PORT" ]]; then
            SERVICE_PORT=62050
        fi
        if [[ "$SERVICE_PORT" -ge 1 && "$SERVICE_PORT" -le 65535 ]]; then
            if is_port_occupied "$SERVICE_PORT"; then
                colorized_echo red "Port $SERVICE_PORT is already in use. Please enter another port."
            else
                break
            fi
        else
            colorized_echo red "Invalid port. Please enter a port between 1 and 65535."
        fi
    done
    
    while true; do
        read -p "Enter the XRAY_API_PORT (default 62051): " -r XRAY_API_PORT
        if [[ -z "$XRAY_API_PORT" ]]; then
            XRAY_API_PORT=62051
        fi
        if [[ "$XRAY_API_PORT" -ge 1 && "$XRAY_API_PORT" -le 65535 ]]; then
            if is_port_occupied "$XRAY_API_PORT"; then
                colorized_echo red "Port $XRAY_API_PORT is already in use. Please enter another port."
            elif [[ "$XRAY_API_PORT" -eq "$SERVICE_PORT" ]]; then
                colorized_echo red "Port $XRAY_API_PORT cannot be the same as SERVICE_PORT. Please enter another port."
            else
                break
            fi
        else
            colorized_echo red "Invalid port. Please enter a port between 1 and 65535."
        fi
    done
    
    colorized_echo blue "Generating compose file"

    # Write content to the file
    cat > "$COMPOSE_FILE" <<EOL
services:
  marzban-node:
    container_name: $APP_NAME
    image: ghcr.io/vovausername/marzban-node:latest
    restart: always
    network_mode: host
    environment:
      SSL_CLIENT_CERT_FILE: "/var/lib/marzban-node/cert.pem"
      SERVICE_PORT: "$SERVICE_PORT"
      XRAY_API_PORT: "$XRAY_API_PORT"
EOL

    # Always write SERVICE_PROTOCOL explicitly (upstream only wrote it for
    # REST and relied on config.py's default otherwise, which silently kept
    # the node on REST even when RPyC was selected here).
    if [[ "$USE_REST" = true ]]; then
        cat >> "$COMPOSE_FILE" <<EOL
      SERVICE_PROTOCOL: "rest"
EOL
    else
        cat >> "$COMPOSE_FILE" <<EOL
      SERVICE_PROTOCOL: "rpyc"
EOL
    fi

    # Written explicitly either way — config.py now defaults both of these
    # to true, so an omitted line would silently re-enable a feature the
    # user just said no to above.
    if [[ "$XRAY_REMOTE_UPDATE_ENABLED" = true ]]; then
        cat >> "$COMPOSE_FILE" <<EOL
      XRAY_REMOTE_UPDATE_ENABLED: "true"
EOL
    else
        cat >> "$COMPOSE_FILE" <<EOL
      XRAY_REMOTE_UPDATE_ENABLED: "false"
EOL
    fi

    if [[ "$IP_BLOCK_ENABLED" = true ]]; then
        cat >> "$COMPOSE_FILE" <<EOL
      IP_BLOCK_ENABLED: "true"
EOL
    else
        cat >> "$COMPOSE_FILE" <<EOL
      IP_BLOCK_ENABLED: "false"
EOL
    fi

    if [[ "$NODE_REMOTE_UPDATE_ENABLED" = true ]]; then
        cat >> "$COMPOSE_FILE" <<EOL
      NODE_REMOTE_UPDATE_ENABLED: "true"
EOL
    else
        cat >> "$COMPOSE_FILE" <<EOL
      NODE_REMOTE_UPDATE_ENABLED: "false"
EOL
    fi

    cat >> "$COMPOSE_FILE" <<EOL

    volumes:
      - $DATA_MAIN_DIR:/var/lib/marzban
      - $DATA_DIR:/var/lib/marzban-node
EOL

    # NET_ADMIN/NET_RAW are only needed for ip_block.py's ipset/iptables
    # rules, so only grant them when IP blocking was actually enabled above.
    if [[ "$IP_BLOCK_ENABLED" = true ]]; then
        cat >> "$COMPOSE_FILE" <<EOL

    cap_add:
      - NET_ADMIN
      - NET_RAW
EOL
    fi

    colorized_echo green "File saved in $APP_DIR/docker-compose.yml"
}


uninstall_marzban_node_script() {
    if [ -f "/usr/local/bin/$APP_NAME" ]; then
        colorized_echo yellow "Removing marzban-node script"
        rm "/usr/local/bin/$APP_NAME"
    fi
}

uninstall_marzban_node() {
    if [ -d "$APP_DIR" ]; then
        colorized_echo yellow "Removing directory: $APP_DIR"
        rm -r "$APP_DIR"
    fi
}

uninstall_marzban_node_docker_images() {
    images=$(docker images | grep marzban-node | awk '{print $3}')
    
    if [ -n "$images" ]; then
        colorized_echo yellow "Removing Docker images of Marzban-node"
        for image in $images; do
            if docker rmi "$image" >/dev/null 2>&1; then
                colorized_echo yellow "Image $image removed"
            fi
        done
    fi
}

uninstall_marzban_node_data_files() {
    if [ -d "$DATA_DIR" ]; then
        colorized_echo yellow "Removing directory: $DATA_DIR"
        rm -r "$DATA_DIR"
    fi
}

up_marzban_node() {
    $COMPOSE -f $COMPOSE_FILE -p "$APP_NAME" up -d --remove-orphans
}

down_marzban_node() {
    $COMPOSE -f $COMPOSE_FILE -p "$APP_NAME" down
}

show_marzban_node_logs() {
    $COMPOSE -f $COMPOSE_FILE -p "$APP_NAME" logs
}

follow_marzban_node_logs() {
    $COMPOSE -f $COMPOSE_FILE -p "$APP_NAME" logs -f
}

update_marzban_node_script() {
    colorized_echo blue "Updating marzban-node script"
    curl -sSL $SCRIPT_URL | install -m 755 /dev/stdin /usr/local/bin/$APP_NAME
    colorized_echo green "marzban-node script updated successfully"
}

update_marzban_node() {
    $COMPOSE -f $COMPOSE_FILE -p "$APP_NAME" pull
}

install_node_updater() {
    # Host-side half of the panel-triggered node update: a systemd path
    # unit watches for the request file the container writes and runs a
    # generated updater script that does what the `update` command does by
    # hand (pull the newest image, recreate the container), then writes the
    # outcome where the recreated container can report it back to the panel.
    if ! command -v systemctl >/dev/null 2>&1; then
        colorized_echo red "systemd (systemctl) not found — the node updater watcher requires it."
        return 1
    fi
    if ! command -v jq >/dev/null 2>&1; then
        detect_os
        install_package jq
    fi

    colorized_echo blue "Installing node updater watcher"
    mkdir -p "$DATA_DIR"

    # Install-time values baked in up top; everything below the second
    # heredoc is runtime logic (quoted delimiter, so no expansion here).
    # Assembled in a temp file and moved into place so regenerating the
    # script is atomic — the watcher re-runs install-updater to refresh
    # itself after a panel-triggered update, and truncating a script bash
    # may still be reading in place would corrupt that run.
    cat > "$UPDATER_SCRIPT.tmp" <<EOF
#!/usr/bin/env bash
# Host-side updater for $APP_NAME — generated by '$APP_NAME install-updater'.
# Triggered by $UPDATER_PATH_UNIT when the container writes
# $UPDATE_REQUEST_FILE (see node_updater.py in the marzban-node repo).
REQUEST_FILE="$UPDATE_REQUEST_FILE"
RESULT_FILE="$UPDATE_RESULT_FILE"
COMPOSE_FILE="$COMPOSE_FILE"
APP_NAME="$APP_NAME"
SCRIPT_URL="$SCRIPT_URL"
EOF
    cat >> "$UPDATER_SCRIPT.tmp" <<'EOF'
set -u
WORKING_FILE="$REQUEST_FILE.working"

stale=""
if [ -e "$REQUEST_FILE" ]; then
    # Claim the request atomically before doing anything: if this script
    # dies mid-update, no request file is left behind to re-trigger the
    # systemd path unit forever.
    mv -f "$REQUEST_FILE" "$WORKING_FILE" || exit 0
elif [ -e "$WORKING_FILE" ]; then
    # A previous run was cut short (host reboot, killed service) after
    # claiming the request but before cleaning up. The path unit also
    # watches this file, so we get here on the next boot/trigger: fail the
    # request deterministically instead of leaving the node reporting a
    # pending update forever with nothing ever resuming it.
    stale=1
else
    exit 0
fi

if docker compose >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    COMPOSE=""
fi

# Only parse the claimed request if it's a real file: the shared
# directory is container-writable, so never let this root process read
# through a planted symlink.
request_id=""
if [ -f "$WORKING_FILE" ] && ! [ -L "$WORKING_FILE" ]; then
    request_id=$(jq -r '.request_id // ""' "$WORKING_FILE" 2>/dev/null)
fi
started_at=$(date -u +%FT%TZ)
status="success"
detail="Pulled the latest image and recreated the container"

if [ -n "$stale" ]; then
    status="failed"
    detail="Previous update attempt was interrupted (host reboot or watcher restart) and has been abandoned; trigger the update again from the panel"
elif [ -z "$COMPOSE" ]; then
    status="failed"
    detail="docker compose not found on the host"
elif [ ! -f "$COMPOSE_FILE" ]; then
    status="failed"
    detail="compose file not found: $COMPOSE_FILE"
elif ! output=$($COMPOSE -f "$COMPOSE_FILE" -p "$APP_NAME" pull 2>&1); then
    status="failed"
    detail="docker compose pull failed: $(printf '%s' "$output" | tail -n 1)"
elif ! output=$($COMPOSE -f "$COMPOSE_FILE" -p "$APP_NAME" up -d --remove-orphans --force-recreate 2>&1); then
    status="failed"
    detail="docker compose up failed: $(printf '%s' "$output" | tail -n 1)"
fi

# Keep the management CLI in step with the image, like the manual
# "$APP_NAME update" command does. Best-effort only: download to a temp
# file and sanity-check it, so a network failure can't clobber the
# installed CLI with an empty file.
if [ "$status" = "success" ]; then
    tmp_script=$(mktemp 2>/dev/null) || tmp_script=""
    if [ -n "$tmp_script" ] && curl -sSL "$SCRIPT_URL" -o "$tmp_script" 2>/dev/null && head -c 2 "$tmp_script" | grep -q '#!'; then
        install -m 755 "$tmp_script" "/usr/local/bin/$APP_NAME" 2>/dev/null || true
    fi
    [ -n "$tmp_script" ] && rm -f "$tmp_script"
fi

finished_at=$(date -u +%FT%TZ)
# The shared directory is container-writable, so a compromised container
# could plant the .tmp path as a symlink to a host file and have this
# root process write through it. rm first (rm doesn't follow symlinks),
# then open with noclobber — O_CREAT|O_EXCL refuses to follow a symlink
# recreated in the gap — and rename into place (rename replaces, never
# follows, a symlinked destination).
rm -f "$RESULT_FILE.tmp"
if (set -o noclobber; jq -n --arg status "$status" --arg detail "$detail" --arg request_id "$request_id" \
      --arg started_at "$started_at" --arg finished_at "$finished_at" \
      '{status: $status, detail: $detail, request_id: $request_id, started_at: $started_at, finished_at: $finished_at}' \
      > "$RESULT_FILE.tmp") 2>/dev/null; then
    mv -f "$RESULT_FILE.tmp" "$RESULT_FILE"
fi
rm -f "$WORKING_FILE"

# Regenerate this watcher from the (possibly just-refreshed) CLI so
# watcher improvements ship with panel-triggered updates too, without
# ever touching the host by hand again. exec replaces this process
# outright, so the regeneration rewriting this very script file is safe.
if [ "$status" = "success" ] && [ -x "/usr/local/bin/$APP_NAME" ]; then
    exec "/usr/local/bin/$APP_NAME" install-updater >/dev/null 2>&1
fi
EOF
    chmod 755 "$UPDATER_SCRIPT.tmp"
    mv -f "$UPDATER_SCRIPT.tmp" "$UPDATER_SCRIPT"

    cat > "/etc/systemd/system/$UPDATER_SERVICE_UNIT" <<EOF
[Unit]
Description=$APP_NAME panel-requested update runner

[Service]
Type=oneshot
ExecStart=$UPDATER_SCRIPT
EOF

    cat > "/etc/systemd/system/$UPDATER_PATH_UNIT" <<EOF
[Unit]
Description=Watch for $APP_NAME node update requests from the panel

[Path]
PathExists=$UPDATE_REQUEST_FILE
# Also watch the claimed-but-unfinished marker, so a request stranded by
# a reboot or killed run gets failed out on the next trigger instead of
# blocking every future update (the script removes it in all paths).
PathExists=$UPDATE_REQUEST_FILE.working

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable --now "$UPDATER_PATH_UNIT" >/dev/null 2>&1

    # Tells the container (via the bind mount) that a watcher is actually
    # listening — node_updater.py refuses requests without it.
    touch "$UPDATER_MARKER_FILE"
    colorized_echo green "Node updater installed: $UPDATER_PATH_UNIT now watches $UPDATE_REQUEST_FILE"
}

uninstall_node_updater() {
    if [ -f "/etc/systemd/system/$UPDATER_PATH_UNIT" ] || [ -f "/etc/systemd/system/$UPDATER_SERVICE_UNIT" ] || [ -f "$UPDATER_SCRIPT" ]; then
        colorized_echo yellow "Removing node updater watcher"
        systemctl disable --now "$UPDATER_PATH_UNIT" >/dev/null 2>&1 || true
        rm -f "/etc/systemd/system/$UPDATER_PATH_UNIT" "/etc/systemd/system/$UPDATER_SERVICE_UNIT" "$UPDATER_SCRIPT"
        systemctl daemon-reload >/dev/null 2>&1 || true
    fi
    rm -f "$UPDATER_MARKER_FILE" "$UPDATE_REQUEST_FILE" "$UPDATE_REQUEST_FILE.working" "$UPDATE_RESULT_FILE"
}

is_marzban_node_installed() {
    if [ -d $APP_DIR ]; then
        return 0
    else
        return 1
    fi
}

is_marzban_node_up() {
    if [ -z "$($COMPOSE -f $COMPOSE_FILE ps -q -a)" ]; then
        return 1
    else
        return 0
    fi
}

install_command() {
    check_running_as_root
    # Check if marzban is already installed
    if is_marzban_node_installed; then
        colorized_echo red "Marzban-node is already installed at $APP_DIR"
        read -p "Do you want to override the previous installation? (y/n) "
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            colorized_echo red "Aborted installation"
            exit 1
        fi
    fi
    detect_os
    if ! command -v jq >/dev/null 2>&1; then
        install_package jq
    fi
    if ! command -v curl >/dev/null 2>&1; then
        install_package curl
    fi
    if ! command -v docker >/dev/null 2>&1; then
        install_docker
    fi
    detect_compose
    install_marzban_node_script
    install_marzban_node
    if [[ "$NODE_REMOTE_UPDATE_ENABLED" = true ]]; then
        install_node_updater
    fi
    up_marzban_node
    follow_marzban_node_logs
    echo "Use your IP: $NODE_IP and defaults ports: $SERVICE_PORT and $XRAY_API_PORT to setup your Marzban Main Panel"
}

uninstall_command() {
    check_running_as_root
    # Check if marzban is installed
    if ! is_marzban_node_installed; then
        colorized_echo red "Marzban-node not installed!"
        exit 1
    fi
    
    read -p "Do you really want to uninstall Marzban-node? (y/n) "
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        colorized_echo red "Aborted"
        exit 1
    fi
    
    detect_compose
    if is_marzban_node_up; then
        down_marzban_node
    fi
    uninstall_node_updater
    uninstall_marzban_node_script
    uninstall_marzban_node
    uninstall_marzban_node_docker_images
    
    read -p "Do you want to remove Marzban-node data files too ($DATA_DIR)? (y/n) "
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        colorized_echo green "Marzban-node uninstalled successfully"
    else
        uninstall_marzban_node_data_files
        colorized_echo green "Marzban-node uninstalled successfully"
    fi
}

up_command() {
    help() {
        colorized_echo red "Usage: marzban-node up [options]"
        echo ""
        echo "OPTIONS:"
        echo "  -h, --help        display this help message"
        echo "  -n, --no-logs     do not follow logs after starting"
    }
    
    local no_logs=false
    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            -n|--no-logs)
                no_logs=true
            ;;
            -h|--help)
                help
                exit 0
            ;;
            *)
                echo "Error: Invalid option: $1" >&2
                help
                exit 0
            ;;
        esac
        shift
    done
    
    # Check if marzban-node is installed
    if ! is_marzban_node_installed; then
        colorized_echo red "Marzban-node's not installed!"
        exit 1
    fi
    
    detect_compose
    
    if is_marzban_node_up; then
        colorized_echo red "Marzban-node's already up"
        exit 1
    fi
    
    up_marzban_node
    if [ "$no_logs" = false ]; then
        follow_marzban_node_logs
    fi
}

down_command() {
    # Check if marzban-node is installed
    if ! is_marzban_node_installed; then
        colorized_echo red "Marzban-node not installed!"
        exit 1
    fi
    
    detect_compose
    
    if ! is_marzban_node_up; then
        colorized_echo red "Marzban-node already down"
        exit 1
    fi
    
    down_marzban_node
}

restart_command() {
    help() {
        colorized_echo red "Usage: marzban-node restart [options]"
        echo
        echo "OPTIONS:"
        echo "  -h, --help        display this help message"
        echo "  -n, --no-logs     do not follow logs after starting"
    }
    
    local no_logs=false
    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            -n|--no-logs)
                no_logs=true
            ;;
            -h|--help)
                help
                exit 0
            ;;
            *)
                echo "Error: Invalid option: $1" >&2
                help
                exit 0
            ;;
        esac
        shift
    done
    
    # Check if marzban-node is installed
    if ! is_marzban_node_installed; then
        colorized_echo red "Marzban-node not installed!"
        exit 1
    fi
    
    detect_compose
    
    down_marzban_node
    up_marzban_node
    
}

status_command() {
    # Check if marzban-node is installed
    if ! is_marzban_node_installed; then
        echo -n "Status: "
        colorized_echo red "Not Installed"
        exit 1
    fi
    
    detect_compose
    
    if ! is_marzban_node_up; then
        echo -n "Status: "
        colorized_echo blue "Down"
        exit 1
    fi
    
    echo -n "Status: "
    colorized_echo green "Up"
    
    json=$($COMPOSE -f $COMPOSE_FILE ps -a --format=json)
    services=$(echo "$json" | jq -r 'if type == "array" then .[] else . end | .Service')
    states=$(echo "$json" | jq -r 'if type == "array" then .[] else . end | .State')
    # Print out the service names and statuses
    for i in $(seq 0 $(expr $(echo $services | wc -w) - 1)); do
        service=$(echo $services | cut -d' ' -f $(expr $i + 1))
        state=$(echo $states | cut -d' ' -f $(expr $i + 1))
        echo -n "- $service: "
        if [ "$state" == "running" ]; then
            colorized_echo green $state
        else
            colorized_echo red $state
        fi
    done
}

logs_command() {
    help() {
        colorized_echo red "Usage: marzban-node logs [options]"
        echo ""
        echo "OPTIONS:"
        echo "  -h, --help        display this help message"
        echo "  -n, --no-follow   do not show follow logs"
    }
    
    local no_follow=false
    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            -n|--no-follow)
                no_follow=true
            ;;
            -h|--help)
                help
                exit 0
            ;;
            *)
                echo "Error: Invalid option: $1" >&2
                help
                exit 0
            ;;
        esac
        shift
    done
    
    # Check if marzban is installed
    if ! is_marzban_node_installed; then
        colorized_echo red "Marzban-node's not installed!"
        exit 1
    fi
    
    detect_compose
    
    if ! is_marzban_node_up; then
        colorized_echo red "Marzban-node is not up."
        exit 1
    fi
    
    if [ "$no_follow" = true ]; then
        show_marzban_node_logs
    else
        follow_marzban_node_logs
    fi
}

update_command() {
    check_running_as_root
    # Check if marzban is installed
    if ! is_marzban_node_installed; then
        colorized_echo red "Marzban-node not installed!"
        exit 1
    fi
    
    detect_compose

    update_marzban_node_script

    # Install (or refresh) the watcher for panel-triggered node updates as
    # part of every manual update, so no separate 'install-updater' run is
    # ever needed per node — unless the compose file explicitly opts out.
    if grep -q 'NODE_REMOTE_UPDATE_ENABLED: "false"' "$COMPOSE_FILE" 2>/dev/null; then
        colorized_echo yellow "NODE_REMOTE_UPDATE_ENABLED is \"false\" in $COMPOSE_FILE — skipping the node updater watcher."
    else
        install_node_updater || colorized_echo yellow "Could not install the node updater watcher; run '$APP_NAME install-updater' manually to retry."
    fi

    colorized_echo blue "Pulling latest version"
    update_marzban_node
    
    colorized_echo blue "Restarting Marzban-node services"
    down_marzban_node
    up_marzban_node
    
    colorized_echo blue "Marzban-node updated successfully"
}

install_updater_command() {
    check_running_as_root
    if ! is_marzban_node_installed; then
        colorized_echo red "Marzban-node not installed!"
        exit 1
    fi
    install_node_updater
    colorized_echo yellow "Note: the node itself must also have NODE_REMOTE_UPDATE_ENABLED not set to \"false\" (it defaults to true) in $COMPOSE_FILE."
}

uninstall_updater_command() {
    check_running_as_root
    uninstall_node_updater
    colorized_echo green "Node updater watcher removed — the panel can no longer update this node."
}

migrate_command() {
    check_running_as_root
    if ! is_marzban_node_installed; then
        colorized_echo red "Marzban-node not installed!"
        exit 1
    fi

    detect_compose

    if ! command -v yq &>/dev/null; then
        echo "yq is not installed. Installing yq..."
        install_yq
    fi

    current_image=$(yq eval '.services."marzban-node".image // ""' "$COMPOSE_FILE")
    colorized_echo blue "Current image: ${current_image:-<none>}"

    if [[ "$current_image" != ghcr.io/vovausername/marzban-node:* ]]; then
        colorized_echo yellow "Switching to this fork's image: ghcr.io/vovausername/marzban-node:latest"
        yq eval '.services."marzban-node".image = "ghcr.io/vovausername/marzban-node:latest"' -i "$COMPOSE_FILE"
    else
        colorized_echo green "Already on this fork's image."
    fi

    if [[ "$ASSUME_YES" == "true" ]]; then
        enable_triggers="y"
    else
        read -p "Enable this fork's remote-control triggers (remote Xray update + IP blocking)? (Y/n): " -r enable_triggers
    fi
    if [[ -z "$enable_triggers" || "$enable_triggers" =~ ^[Yy]$ ]]; then
        yq eval '.services."marzban-node".environment.XRAY_REMOTE_UPDATE_ENABLED = "true"' -i "$COMPOSE_FILE"
        yq eval '.services."marzban-node".environment.IP_BLOCK_ENABLED = "true"' -i "$COMPOSE_FILE"
        yq eval '.services."marzban-node".cap_add = ["NET_ADMIN", "NET_RAW"]' -i "$COMPOSE_FILE"
        colorized_echo green "Remote-control triggers enabled (XRAY_REMOTE_UPDATE_ENABLED, IP_BLOCK_ENABLED, NET_ADMIN/NET_RAW)."
    else
        yq eval '.services."marzban-node".environment.XRAY_REMOTE_UPDATE_ENABLED = "false"' -i "$COMPOSE_FILE"
        yq eval '.services."marzban-node".environment.IP_BLOCK_ENABLED = "false"' -i "$COMPOSE_FILE"
        colorized_echo yellow "Remote-control triggers left disabled. Re-run '$APP_NAME migrate' anytime to turn them on."
    fi

    if [[ "$ASSUME_YES" == "true" ]]; then
        enable_node_update="y"
    else
        read -p "Also allow the panel to update this node itself (installs a host-side watcher)? (Y/n): " -r enable_node_update
    fi
    if [[ -z "$enable_node_update" || "$enable_node_update" =~ ^[Yy]$ ]]; then
        yq eval '.services."marzban-node".environment.NODE_REMOTE_UPDATE_ENABLED = "true"' -i "$COMPOSE_FILE"
        install_node_updater
    else
        yq eval '.services."marzban-node".environment.NODE_REMOTE_UPDATE_ENABLED = "false"' -i "$COMPOSE_FILE"
        colorized_echo yellow "Panel-triggered node updates left disabled. Run '$APP_NAME install-updater' anytime to turn them on."
    fi

    client_cert=$(yq eval '.services."marzban-node".environment.SSL_CLIENT_CERT_FILE // ""' "$COMPOSE_FILE")
    if [ -z "$client_cert" ]; then
        colorized_echo red "WARNING: SSL_CLIENT_CERT_FILE is not set in $COMPOSE_FILE."
        colorized_echo red "Running with the remote-control triggers enabled and no client cert lets ANY network peer that can reach this node invoke them. Set SSL_CLIENT_CERT_FILE before relying on this in production."
    fi

    colorized_echo blue "Pulling the new image..."
    $COMPOSE -f "$COMPOSE_FILE" -p "$APP_NAME" pull

    colorized_echo blue "Restarting Marzban-node services"
    down_marzban_node
    up_marzban_node

    colorized_echo green "Migration to vovausername/Marzban-node complete."
}

identify_the_operating_system_and_architecture() {
    if [[ "$(uname)" == 'Linux' ]]; then
        case "$(uname -m)" in
            'i386' | 'i686')
                ARCH='32'
            ;;
            'amd64' | 'x86_64')
                ARCH='64'
            ;;
            'armv5tel')
                ARCH='arm32-v5'
            ;;
            'armv6l')
                ARCH='arm32-v6'
                grep Features /proc/cpuinfo | grep -qw 'vfp' || ARCH='arm32-v5'
            ;;
            'armv7' | 'armv7l')
                ARCH='arm32-v7a'
                grep Features /proc/cpuinfo | grep -qw 'vfp' || ARCH='arm32-v5'
            ;;
            'armv8' | 'aarch64')
                ARCH='arm64-v8a'
            ;;
            'mips')
                ARCH='mips32'
            ;;
            'mipsle')
                ARCH='mips32le'
            ;;
            'mips64')
                ARCH='mips64'
                lscpu | grep -q "Little Endian" && ARCH='mips64le'
            ;;
            'mips64le')
                ARCH='mips64le'
            ;;
            'ppc64')
                ARCH='ppc64'
            ;;
            'ppc64le')
                ARCH='ppc64le'
            ;;
            'riscv64')
                ARCH='riscv64'
            ;;
            's390x')
                ARCH='s390x'
            ;;
            *)
                echo "error: The architecture is not supported."
                exit 1
            ;;
        esac
    else
        echo "error: This operating system is not supported."
        exit 1
    fi
}

# Function to update the Xray core
get_xray_core() {
    identify_the_operating_system_and_architecture
    clear
    
    
    validate_version() {
        local version="$1"
        
        local response=$(curl -s "https://api.github.com/repos/XTLS/Xray-core/releases/tags/$version")
        if echo "$response" | grep -q '"message": "Not Found"'; then
            echo "invalid"
        else
            echo "valid"
        fi
    }
    
    
    print_menu() {
        clear
        echo -e "\033[1;32m==============================\033[0m"
        echo -e "\033[1;32m      Xray-core Installer     \033[0m"
        echo -e "\033[1;32m==============================\033[0m"
       current_version=$(get_current_xray_core_version)
        echo -e "\033[1;33m>>>> Current Xray-core version: \033[1;1m$current_version\033[0m"
        echo -e "\033[1;32m==============================\033[0m"
        echo -e "\033[1;33mAvailable Xray-core versions:\033[0m"
        for ((i=0; i<${#versions[@]}; i++)); do
            echo -e "\033[1;34m$((i + 1)):\033[0m ${versions[i]}"
        done
        echo -e "\033[1;32m==============================\033[0m"
        echo -e "\033[1;35mM:\033[0m Enter a version manually"
        echo -e "\033[1;31mQ:\033[0m Quit"
        echo -e "\033[1;32m==============================\033[0m"
    }
    
    
    latest_releases=$(curl -s "https://api.github.com/repos/XTLS/Xray-core/releases?per_page=$LAST_XRAY_CORES")
    
    
    versions=($(echo "$latest_releases" | grep -oP '"tag_name": "\K(.*?)(?=")'))
    
    while true; do
        print_menu
        read -p "Choose a version to install (1-${#versions[@]}), or press M to enter manually, Q to quit: " choice
        
        if [[ "$choice" =~ ^[1-9][0-9]*$ ]] && [ "$choice" -le "${#versions[@]}" ]; then
            
            choice=$((choice - 1))
            
            selected_version=${versions[choice]}
            break
            elif [ "$choice" == "M" ] || [ "$choice" == "m" ]; then
            while true; do
                read -p "Enter the version manually (e.g., v1.2.3): " custom_version
                if [ "$(validate_version "$custom_version")" == "valid" ]; then
                    selected_version="$custom_version"
                    break 2
                else
                    echo -e "\033[1;31mInvalid version or version does not exist. Please try again.\033[0m"
                fi
            done
            elif [ "$choice" == "Q" ] || [ "$choice" == "q" ]; then
            echo -e "\033[1;31mExiting.\033[0m"
            exit 0
        else
            echo -e "\033[1;31mInvalid choice. Please try again.\033[0m"
            sleep 2
        fi
    done
    
    echo -e "\033[1;32mSelected version $selected_version for installation.\033[0m"
    
    
if ! dpkg -s unzip >/dev/null 2>&1; then
    echo -e "\033[1;33mInstalling required packages...\033[0m"
    detect_os
    install_package unzip
fi

    
    
    mkdir -p $DATA_MAIN_DIR/xray-core
    cd $DATA_MAIN_DIR/xray-core
    
    
    
    xray_filename="Xray-linux-$ARCH.zip"
    xray_download_url="https://github.com/XTLS/Xray-core/releases/download/${selected_version}/${xray_filename}"
    
    echo -e "\033[1;33mDownloading Xray-core version ${selected_version} in the background...\033[0m"
    wget "${xray_download_url}" -q &
    wait
    
    
    echo -e "\033[1;33mExtracting Xray-core in the background...\033[0m"
    unzip -o "${xray_filename}" >/dev/null 2>&1 &
    wait
    rm "${xray_filename}"
}
get_current_xray_core_version() {
    XRAY_BINARY="$DATA_MAIN_DIR/xray-core/xray"
    if [ -f "$XRAY_BINARY" ]; then
        version_output=$("$XRAY_BINARY" -version 2>/dev/null)
        if [ $? -eq 0 ]; then
            version=$(echo "$version_output" | head -n1 | awk '{print $2}')
            echo "$version"
            return
        fi
    fi

    # If local binary is not found or failed, check in the Docker container
    CONTAINER_NAME="$APP_NAME"
    if docker ps --format '{{.Names}}' | grep -q "^$CONTAINER_NAME$"; then
        version_output=$(docker exec "$CONTAINER_NAME" xray -version 2>/dev/null)
        if [ $? -eq 0 ]; then
            # Extract the version number from the first line
            version=$(echo "$version_output" | head -n1 | awk '{print $2}')
            echo "$version (in container)"
            return
        fi
    fi

    echo "Not installed"
}

install_yq() {
    if command -v yq &>/dev/null; then
        colorized_echo green "yq is already installed."
        return
    fi

    identify_the_operating_system_and_architecture

    local base_url="https://github.com/mikefarah/yq/releases/latest/download"
    local yq_binary=""

    case "$ARCH" in
        '64' | 'x86_64')
            yq_binary="yq_linux_amd64"
            ;;
        'arm32-v7a' | 'arm32-v6' | 'arm32-v5' | 'armv7l')
            yq_binary="yq_linux_arm"
            ;;
        'arm64-v8a' | 'aarch64')
            yq_binary="yq_linux_arm64"
            ;;
        '32' | 'i386' | 'i686')
            yq_binary="yq_linux_386"
            ;;
        *)
            colorized_echo red "Unsupported architecture: $ARCH"
            exit 1
            ;;
    esac

    local yq_url="${base_url}/${yq_binary}"
    colorized_echo blue "Downloading yq from ${yq_url}..."

    if ! command -v curl &>/dev/null && ! command -v wget &>/dev/null; then
        colorized_echo yellow "Neither curl nor wget is installed. Attempting to install curl."
        install_package curl || {
            colorized_echo red "Failed to install curl. Please install curl or wget manually."
            exit 1
        }
    fi


    if command -v curl &>/dev/null; then
        if curl -L "$yq_url" -o /usr/local/bin/yq; then
            chmod +x /usr/local/bin/yq
            colorized_echo green "yq installed successfully!"
        else
            colorized_echo red "Failed to download yq using curl. Please check your internet connection."
            exit 1
        fi
    elif command -v wget &>/dev/null; then
        if wget -O /usr/local/bin/yq "$yq_url"; then
            chmod +x /usr/local/bin/yq
            colorized_echo green "yq installed successfully!"
        else
            colorized_echo red "Failed to download yq using wget. Please check your internet connection."
            exit 1
        fi
    fi


    if ! echo "$PATH" | grep -q "/usr/local/bin"; then
        export PATH="/usr/local/bin:$PATH"
    fi


    hash -r

    if command -v yq &>/dev/null; then
        colorized_echo green "yq is ready to use."
    elif [ -x "/usr/local/bin/yq" ]; then

        colorized_echo yellow "yq is installed at /usr/local/bin/yq but not found in PATH."
        colorized_echo yellow "You can add /usr/local/bin to your PATH environment variable."
    else
        colorized_echo red "yq installation failed. Please try again or install manually."
        exit 1
    fi
}



update_core_command() {
    check_running_as_root
    get_xray_core

    if ! command -v yq &>/dev/null; then
        echo "yq is not installed. Installing yq..."
        install_yq
    fi

    if ! grep -q 'XRAY_EXECUTABLE_PATH: "/var/lib/marzban-node/xray-core/xray"' "$COMPOSE_FILE"; then
        yq eval '.services."marzban-node".environment.XRAY_EXECUTABLE_PATH = "/var/lib/marzban-node/xray-core/xray"' -i "$COMPOSE_FILE"
    fi

    if ! yq eval ".services.\"marzban-node\".volumes[] | select(. == \"${DATA_MAIN_DIR}:/var/lib/marzban-node\")" "$COMPOSE_FILE" &>/dev/null; then
        yq eval ".services.\"marzban-node\".volumes += \"${DATA_MAIN_DIR}:/var/lib/marzban-node\"" -i "$COMPOSE_FILE"
    fi
    
    # Restart Marzban-node
    colorized_echo red "Restarting Marzban-node..."
    $APP_NAME restart -n
    colorized_echo blue "Installation of XRAY-CORE version $selected_version completed."
}


check_editor() {
    if [ -z "$EDITOR" ]; then
        if command -v nano >/dev/null 2>&1; then
            EDITOR="nano"
            elif command -v vi >/dev/null 2>&1; then
            EDITOR="vi"
        else
            detect_os
            install_package nano
            EDITOR="nano"
        fi
    fi
}


edit_command() {
    detect_os
    check_editor
    if [ -f "$COMPOSE_FILE" ]; then
        $EDITOR "$COMPOSE_FILE"
    else
        colorized_echo red "Compose file not found at $COMPOSE_FILE"
        exit 1
    fi
}


usage() {
    colorized_echo blue "================================"
    colorized_echo magenta "       $APP_NAME Node CLI Help"
    colorized_echo blue "================================"
    colorized_echo cyan "Usage:"
    echo "  $APP_NAME [command] [-y|--yes]"
    echo

    colorized_echo cyan "Commands:"
    colorized_echo yellow "  up              $(tput sgr0)– Start services"
    colorized_echo yellow "  down            $(tput sgr0)– Stop services"
    colorized_echo yellow "  restart         $(tput sgr0)– Restart services"
    colorized_echo yellow "  status          $(tput sgr0)– Show status"
    colorized_echo yellow "  logs            $(tput sgr0)– Show logs"
    colorized_echo yellow "  install         $(tput sgr0)– Install/reinstall Marzban-node"
    colorized_echo yellow "  update          $(tput sgr0)– Update to latest version"
    colorized_echo yellow "  migrate         $(tput sgr0)– Switch an existing node to this fork's image + triggers (add -y/--yes to accept all prompts)"
    colorized_echo yellow "  uninstall       $(tput sgr0)– Uninstall Marzban-node"
    colorized_echo yellow "  install-script  $(tput sgr0)– Install Marzban-node script"
    colorized_echo yellow "  uninstall-script  $(tput sgr0)– Uninstall Marzban-node script"
    colorized_echo yellow "  install-updater $(tput sgr0)– Install host watcher for panel-triggered node updates"
    colorized_echo yellow "  uninstall-updater $(tput sgr0)– Remove that watcher"
    colorized_echo yellow "  edit            $(tput sgr0)– Edit docker-compose.yml (via nano or vi)"
    colorized_echo yellow "  core-update     $(tput sgr0)– Update/Change Xray core"
    
    echo
    colorized_echo cyan "Node Information:"
    colorized_echo magenta "  Cert file path: $CERT_FILE"
    colorized_echo magenta "  Node IP: $NODE_IP"
    echo
    current_version=$(get_current_xray_core_version)
    colorized_echo cyan "Current Xray-core version: " 1  # 1 for bold
    colorized_echo magenta "$current_version" 1
    echo
    DEFAULT_SERVICE_PORT="62050"
    DEFAULT_XRAY_API_PORT="62051"
    
    if [ -f "$COMPOSE_FILE" ]; then
        SERVICE_PORT=$(awk -F': ' '/SERVICE_PORT:/ {gsub(/"/, "", $2); print $2}' "$COMPOSE_FILE")
        XRAY_API_PORT=$(awk -F': ' '/XRAY_API_PORT:/ {gsub(/"/, "", $2); print $2}' "$COMPOSE_FILE")
    fi
    
    SERVICE_PORT=${SERVICE_PORT:-$DEFAULT_SERVICE_PORT}
    XRAY_API_PORT=${XRAY_API_PORT:-$DEFAULT_XRAY_API_PORT}

    colorized_echo cyan "Ports:"
    colorized_echo magenta "  Service port: $SERVICE_PORT"
    colorized_echo magenta "  API port: $XRAY_API_PORT"
    echo

    REMOTE_XRAY_UPDATE_STATUS="disabled"
    IP_BLOCK_STATUS="disabled"
    if [ -f "$COMPOSE_FILE" ]; then
        grep -q 'XRAY_REMOTE_UPDATE_ENABLED: "true"' "$COMPOSE_FILE" && REMOTE_XRAY_UPDATE_STATUS="enabled"
        grep -q 'IP_BLOCK_ENABLED: "true"' "$COMPOSE_FILE" && IP_BLOCK_STATUS="enabled"
    fi
    NODE_UPDATER_STATUS="not installed"
    [ -f "$UPDATER_MARKER_FILE" ] && NODE_UPDATER_STATUS="installed"
    colorized_echo cyan "Remote-control triggers (fork-specific):"
    colorized_echo magenta "  Remote Xray update: $REMOTE_XRAY_UPDATE_STATUS"
    colorized_echo magenta "  IP blocking: $IP_BLOCK_STATUS"
    colorized_echo magenta "  Node update watcher: $NODE_UPDATER_STATUS"
    colorized_echo blue "================================="
    echo
}

case "$COMMAND" in
    install)
        install_command
    ;;
    update)
        update_command
    ;;
    migrate)
        migrate_command
    ;;
    uninstall)
        uninstall_command
    ;;
    up)
        up_command
    ;;
    down)
        down_command
    ;;
    restart)
        restart_command
    ;;
    status)
        status_command
    ;;
    logs)
        logs_command
    ;;
    core-update)
        update_core_command
    ;;
    install-script)
        install_marzban_node_script
    ;;
    uninstall-script)
        uninstall_marzban_node_script
    ;;
    install-updater)
        install_updater_command
    ;;
    uninstall-updater)
        uninstall_updater_command
    ;;
    edit)
        edit_command
    ;;
    *)
        usage
    ;;
esac
