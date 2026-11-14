#!/usr/bin/env bash
# Auto-deploy: pulls latest code, rebuilds containers, restarts.
# Called by webhook endpoint (/webhook/deploy) or manually.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/app}"
LOG_FILE="${LOG_FILE:-/app/data/deploy.log}"
BRANCH="${DEPLOY_BRANCH:-main}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

log "=== Auto-deploy started ==="
log "Project: $PROJECT_DIR"
log "Branch: $BRANCH"

cd "$PROJECT_DIR"

log "Pulling latest code from origin/$BRANCH..."
git fetch origin "$BRANCH" 2>&1 | tee -a "$LOG_FILE"
git reset --hard "origin/$BRANCH" 2>&1 | tee -a "$LOG_FILE"
log "Code updated to $(git rev-parse --short HEAD)"

log "Rebuilding containers..."
docker compose down 2>&1 | tee -a "$LOG_FILE"
docker compose up --build -d 2>&1 | tee -a "$LOG_FILE"

log "Waiting for containers to start..."
sleep 5

log "Container status:"
docker compose ps 2>&1 | tee -a "$LOG_FILE"

log "=== Auto-deploy completed ==="
