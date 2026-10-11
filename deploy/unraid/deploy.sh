#!/usr/bin/env bash
# Deploy the committed HEAD to an Unraid server: copy the source over SSH, build the image there, and
# recreate the container from its Unraid template (installed from my-claude-board.xml on first deploy).
#
#   deploy/unraid/deploy.sh
#
# Settings come from the environment or the repo's .env (see .env.example):
#   DEPLOY_SSH_TARGET  (required) e.g. root@192.168.1.10; key-based SSH must work non-interactively
#   DEPLOY_APPDATA     where source and data live on the server (default /mnt/user/appdata/claude-board)
#   DEPLOY_PORT        port where the container answers on the server's 127.0.0.1, for the health check
#                      (default 8792)
#   SSH                ssh binary (default ssh; on Windows Git Bash, /c/Windows/System32/OpenSSH/ssh.exe
#                      uses the Windows ssh-agent)
set -euo pipefail
cd "$(dirname "$0")/../.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

: "${DEPLOY_SSH_TARGET:?Set DEPLOY_SSH_TARGET (e.g. root@192.168.1.10) in .env or the environment}"
APPDATA="${DEPLOY_APPDATA:-/mnt/user/appdata/claude-board}"
PORT="${DEPLOY_PORT:-8792}"
SSH="${SSH:-ssh}"

if ! git diff --quiet HEAD; then
  echo "Uncommitted changes; only HEAD ($(git rev-parse --short HEAD)) will be deployed." >&2
fi

# Runs on the server; the source tarball arrives on stdin.
REMOTE_SCRIPT=$(cat <<'REMOTE'
set -e
SRC="$APPDATA/src"
rm -rf "$SRC" && mkdir -p "$SRC" "$APPDATA/data" && tar -x -C "$SRC"
chown 99:100 "$APPDATA/data"
TEMPLATE=/boot/config/plugins/dockerMan/templates-user/my-claude-board.xml
[ -f "$TEMPLATE" ] || cp "$SRC/deploy/unraid/my-claude-board.xml" "$TEMPLATE"
docker build -q -t claude-board:latest "$SRC" >/dev/null
/usr/local/emhttp/plugins/dynamix.docker.manager/scripts/rebuild_container claude-board >/dev/null
docker start claude-board >/dev/null 2>&1 || true
docker image prune -f >/dev/null
for i in $(seq 1 20); do
  curl -sf -o /dev/null "http://127.0.0.1:$PORT/healthz" && { echo "claude-board is up"; exit 0; }
  sleep 1
done
echo "claude-board did not come up; recent logs:" >&2; docker logs --tail 30 claude-board >&2; exit 1
REMOTE
)

git archive HEAD | "$SSH" -o BatchMode=yes "$DEPLOY_SSH_TARGET" "APPDATA='$APPDATA'; PORT='$PORT'; $REMOTE_SCRIPT"
