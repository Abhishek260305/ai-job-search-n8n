#!/bin/sh
# Forward the n8n UI and the web dashboard from a remote host to this machine.
# Both are bound to 127.0.0.1 on the server on purpose: nothing is exposed publicly.
#
# Usage: scripts/tunnel.sh user@host [path/to/ssh-key]
#   N8N_LOCAL_PORT=5678 DASHBOARD_LOCAL_PORT=8080   the ports on this machine (change them if they are taken)
#   DASHBOARD_PORT=8080                             the dashboard's port on the server, if you changed it in .env
# Without the dashboard enabled its forward simply has nothing to connect to; n8n works as before.
set -e
[ -n "$1" ] || { echo "usage: $0 user@host [ssh-key]" >&2; exit 1; }
host=$1
key=${2:-}
n8n_port=${N8N_LOCAL_PORT:-5678}
dash_port=${DASHBOARD_LOCAL_PORT:-8080}
remote_dash=${DASHBOARD_PORT:-8080}
for p in "$n8n_port" "$dash_port" "$remote_dash"; do
  case $p in
    ''|*[!0-9]*) echo "not a port number: $p" >&2; exit 1 ;;
  esac
done

if [ -n "$key" ]; then set -- -i "$key"; else set --; fi
echo "n8n: http://localhost:$n8n_port, dashboard: http://localhost:$dash_port (Ctrl-C closes the tunnel)"
exec ssh "$@" -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L "$n8n_port:127.0.0.1:5678" -L "$dash_port:127.0.0.1:$remote_dash" "$host"
