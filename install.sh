#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/backupdock"
VENV_DIR="${INSTALL_DIR}/venv"
BIN_LINK="/usr/bin/backupdock"
CONFIG_DIR="/etc/backupdock"
CONFIG_FILE="${CONFIG_DIR}/config.yaml"
CONFIG_EXAMPLE_FILE="${CONFIG_DIR}/config.yaml.example"
LEGACY_CONFIG_FILE="${CONFIG_DIR}/config.toml"
STATE_DIR="/var/lib/backupdock"
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

log() {
    printf 'BackupDock installer: %s\n' "$*"
}

warn() {
    printf 'BackupDock installer: warning: %s\n' "$*" >&2
}

fail() {
    printf 'BackupDock installer: error: %s\n' "$*" >&2
    exit 1
}

if [[ ${EUID} -ne 0 ]]; then
    fail "run this installer as root, for example: sudo ./install.sh"
fi

[[ -f "${SOURCE_DIR}/pyproject.toml" ]] || fail "pyproject.toml not found next to install.sh"
[[ -f "${SOURCE_DIR}/config.yaml.example" ]] || fail "config.yaml.example not found next to install.sh"

venv_available() {
    command -v python3 >/dev/null 2>&1 || return 1
    local probe
    probe="$(mktemp -d)"
    if python3 -m venv "${probe}/venv" >/dev/null 2>&1; then
        rm -rf -- "${probe}"
        return 0
    fi
    rm -rf -- "${probe}"
    return 1
}

install_system_dependencies() {
    log "installing missing system dependencies"

    if command -v apt-get >/dev/null 2>&1; then
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv restic
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y python3 restic
    elif command -v yum >/dev/null 2>&1; then
        yum install -y python3 restic
    elif command -v zypper >/dev/null 2>&1; then
        zypper --non-interactive install python3 restic
    elif command -v pacman >/dev/null 2>&1; then
        pacman -Sy --noconfirm python restic
    else
        fail "no supported package manager found; install Python 3.11+, Python venv support and Restic manually"
    fi
}

if ! command -v python3 >/dev/null 2>&1 || ! command -v restic >/dev/null 2>&1 || ! venv_available; then
    install_system_dependencies
fi

command -v python3 >/dev/null 2>&1 || fail "python3 is not available"
command -v restic >/dev/null 2>&1 || fail "restic is not available"
venv_available || fail "python3 venv support is not available"

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    fail "Python 3.11 or newer is required"
fi

install -d -m 0755 "${INSTALL_DIR}"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    log "creating virtual environment at ${VENV_DIR}"
    python3 -m venv "${VENV_DIR}"
fi

log "installing/updating BackupDock inside the virtual environment"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip setuptools
"${VENV_DIR}/bin/python" -m pip install --upgrade "${SOURCE_DIR}"

ln -sfn "${VENV_DIR}/bin/backupdock" "${BIN_LINK}"

install -d -m 0755 "${CONFIG_DIR}"
install -m 0644 "${SOURCE_DIR}/config.yaml.example" "${CONFIG_EXAMPLE_FILE}"
log "installed/updated example configuration at ${CONFIG_EXAMPLE_FILE}"

if [[ ! -e "${CONFIG_FILE}" ]]; then
    warn "${CONFIG_FILE} does not exist; copy and edit ${CONFIG_EXAMPLE_FILE} before running backups"
fi
if [[ -e "${LEGACY_CONFIG_FILE}" ]]; then
    warn "legacy ${LEGACY_CONFIG_FILE} found; BackupDock 0.2+ uses YAML"
fi

install -d -m 0700 "${STATE_DIR}"

if ! "${VENV_DIR}/bin/python" -c 'import docker; client = docker.from_env(); client.ping(); client.close()' >/dev/null 2>&1; then
    warn "Docker daemon is not reachable with the current environment; installation itself completed"
fi

log "installed $("${BIN_LINK}" --version)"
log "command: ${BIN_LINK}"
log "configuration example: ${CONFIG_EXAMPLE_FILE}"
log "configuration: ${CONFIG_FILE}"
