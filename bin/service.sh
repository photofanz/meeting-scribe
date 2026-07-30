#!/bin/bash
# Upload service controller (launchd, macOS).
#   service.sh {start|stop|restart|disable|enable|status|url|log [n]|rotate}
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"

LABEL="$("$PY" "$ROOT/bin/config.py" service_label 2>/dev/null || echo com.meetingscribe.uploader)"
PORT="$("$PY" "$ROOT/bin/config.py" port 2>/dev/null || echo 8765)"
DOMAIN="gui/$(id -u)"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG="$ROOT/logs/server.log"

case "${1:-}" in
  start)   launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null; launchctl kickstart -k "$DOMAIN/$LABEL"; echo "started" ;;
  stop)    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null; echo "stopped (仍會在登入時自啟；要永久停用請用 disable)" ;;
  restart) launchctl kickstart -k "$DOMAIN/$LABEL"; echo "restarted" ;;
  disable) launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null; launchctl disable "$DOMAIN/$LABEL"; echo "disabled (登入不再自啟)" ;;
  enable)  launchctl enable "$DOMAIN/$LABEL"; launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null; echo "enabled" ;;
  status)
    PID=$(launchctl print "$DOMAIN/$LABEL" 2>/dev/null | awk -F'= ' '/^\tpid = /{print $2; exit}')
    if [ -z "$PID" ]; then
      PID=$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1)
    fi
    echo "root:   $ROOT"
    echo "label:  $LABEL"
    echo "pid:    ${PID:-(not running)}"
    echo "state:  $(launchctl print "$DOMAIN/$LABEL" 2>/dev/null | awk -F'= ' '/^\tstate/{print $2}')"
    echo "http:   $(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/" 2>/dev/null)"
    ;;
  url)
    IP="$(tailscale ip -4 2>/dev/null | head -1)"
    [ -z "$IP" ] && IP="$(ipconfig getifaddr en0 2>/dev/null || echo 127.0.0.1)"
    echo "http://${IP}:${PORT}/?k=$(cat "$ROOT/.token" 2>/dev/null)"
    ;;
  log)     tail -n "${2:-40}" "$LOG" ;;
  rotate)
    if [ -f "$LOG" ] && [ "$(stat -f%z "$LOG")" -gt 5242880 ]; then
      mv "$LOG" "${LOG}.1"; launchctl kickstart -k "$DOMAIN/$LABEL"; echo "rotated"
    else echo "no rotation needed ($(du -h "$LOG" 2>/dev/null | cut -f1))"; fi ;;
  *) echo "用法: service.sh {start|stop|restart|disable|enable|status|url|log [n]|rotate}"; exit 1 ;;
esac
