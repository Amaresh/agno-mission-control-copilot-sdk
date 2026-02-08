#!/usr/bin/env bash
# Mission Control service management
# Usage: ./mc {start|stop|restart|status|logs|install}

set -euo pipefail

SERVICES=(mc-mcp mc-api mc-bot mc-scheduler)
UNIT_DIR="$HOME/.config/systemd/user"
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)/infra/systemd"

_bold()  { printf "\033[1m%s\033[0m\n" "$*"; }
_green() { printf "\033[32m%s\033[0m\n" "$*"; }
_red()   { printf "\033[31m%s\033[0m\n" "$*"; }
_yellow(){ printf "\033[33m%s\033[0m\n" "$*"; }

cmd_install() {
    _bold "Installing Mission Control systemd services..."
    mkdir -p "$UNIT_DIR" "$(pwd)/logs"

    # Enable linger so user services survive logout
    if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
        _yellow "Enabling linger for $USER (requires sudo)..."
        sudo loginctl enable-linger "$USER"
    fi

    for svc in "${SERVICES[@]}"; do
        cp "$SOURCE_DIR/${svc}.service" "$UNIT_DIR/${svc}.service"
        _green "✓ Installed ${svc}.service"
    done

    systemctl --user daemon-reload
    for svc in "${SERVICES[@]}"; do
        systemctl --user enable "$svc"
    done
    _green "✓ Services enabled and daemon reloaded"
}

cmd_start() {
    local target="${1:-all}"
    if [[ "$target" == "all" ]]; then
        for svc in "${SERVICES[@]}"; do
            systemctl --user start "$svc"
            _green "✓ Started $svc"
        done
    else
        systemctl --user start "mc-${target}"
        _green "✓ Started mc-${target}"
    fi
}

cmd_stop() {
    local target="${1:-all}"
    if [[ "$target" == "all" ]]; then
        for svc in "${SERVICES[@]}"; do
            systemctl --user stop "$svc" 2>/dev/null || true
            _yellow "⏹ Stopped $svc"
        done
    else
        systemctl --user stop "mc-${target}" 2>/dev/null || true
        _yellow "⏹ Stopped mc-${target}"
    fi
}

cmd_restart() {
    local target="${1:-all}"
    if [[ "$target" == "all" ]]; then
        for svc in "${SERVICES[@]}"; do
            systemctl --user restart "$svc"
            _green "✓ Restarted $svc"
        done
    else
        systemctl --user restart "mc-${target}"
        _green "✓ Restarted mc-${target}"
    fi
}

cmd_status() {
    _bold "Mission Control Service Status"
    echo ""
    for svc in "${SERVICES[@]}"; do
        local state
        state=$(systemctl --user is-active "$svc" 2>/dev/null || echo "inactive")
        local pid=""
        if [[ "$state" == "active" ]]; then
            pid=" (PID $(systemctl --user show "$svc" --property=MainPID --value))"
            _green "  ● $svc: active${pid}"
        else
            _red "  ○ $svc: $state"
        fi
    done
    echo ""

    # Check ports
    _bold "Ports:"
    for port in 8000 8001; do
        if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
            _green "  ● :${port} listening"
        else
            _red "  ○ :${port} not listening"
        fi
    done
}

cmd_logs() {
    local target="${1:-bot}"
    journalctl --user -u "mc-${target}" -f --no-pager
}

case "${1:-help}" in
    install) cmd_install ;;
    start)   cmd_start "${2:-all}" ;;
    stop)    cmd_stop "${2:-all}" ;;
    restart) cmd_restart "${2:-all}" ;;
    status)  cmd_status ;;
    logs)    cmd_logs "${2:-bot}" ;;
    help|*)
        echo "Usage: ./mc {install|start|stop|restart|status|logs} [service]"
        echo ""
        echo "Services: mcp, api, bot, scheduler (or 'all')"
        echo ""
        echo "Examples:"
        echo "  ./mc install          # Install and enable systemd units"
        echo "  ./mc start            # Start all services"
        echo "  ./mc restart bot      # Restart just the bot"
        echo "  ./mc status           # Show service status"
        echo "  ./mc logs scheduler   # Follow scheduler logs"
        ;;
esac
