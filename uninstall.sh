#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/backupdock"
BIN_LINK="/usr/bin/backupdock"
CONFIG_DIR="/etc/backupdock"
STATE_DIR="/var/lib/backupdock"
PURGE=false

log() {
    printf 'BackupDock uninstaller: %s\n' "$*"
}

fail() {
    printf 'BackupDock uninstaller: error: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage: sudo ./uninstall.sh [--purge]

Without --purge, the configuration and BackupDock state directory are kept.
With --purge, /etc/backupdock and /var/lib/backupdock are removed as well.
EOF
}

for argument in "$@"; do
    case "${argument}" in
        --purge)
            PURGE=true
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown argument: ${argument}"
            ;;
    esac
done

if [[ ${EUID} -ne 0 ]]; then
    fail "run this uninstaller as root, for example: sudo ./uninstall.sh"
fi

if [[ -L "${BIN_LINK}" ]]; then
    target="$(readlink -- "${BIN_LINK}")"
    if [[ "${target}" == "${INSTALL_DIR}/venv/bin/backupdock" ]]; then
        rm -f -- "${BIN_LINK}"
        log "removed ${BIN_LINK}"
    else
        log "warning: ${BIN_LINK} points to ${target}; leaving it untouched"
    fi
elif [[ -e "${BIN_LINK}" ]]; then
    log "warning: ${BIN_LINK} is not a BackupDock symlink; leaving it untouched"
fi

if [[ -d "${INSTALL_DIR}" ]]; then
    rm -rf -- "${INSTALL_DIR}"
    log "removed ${INSTALL_DIR}"
fi

if [[ "${PURGE}" == true ]]; then
    rm -rf -- "${CONFIG_DIR}" "${STATE_DIR}"
    log "removed ${CONFIG_DIR} and ${STATE_DIR}"
else
    log "preserved ${CONFIG_DIR} and ${STATE_DIR}"
    log "use --purge to remove configuration and state as well"
fi

log "system packages such as Python and Restic were not removed"
