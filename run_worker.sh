#!/usr/bin/env bash
# Run the NetCortex worker natively on the Mac so it has full network access
# (Docker containers can't reach device management IPs on macOS)
#
# Usage: ./run_worker.sh
#
# Prerequisites:
#   1. Neo4j and Redis still running in Docker: docker compose up -d neo4j redis netcortex
#   2. netcortex installed: pip install -e ".[all]"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env
set -a
source .env
set +a

# Point to Docker-hosted services
export NEO4J_URI="${NEO4J_URI:-bolt://localhost:7687}"
export REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"
export SYNC_BACKEND="${SYNC_BACKEND:-celery}"

echo "Starting NetCortex worker (native — full network access)"
echo "  NEO4J_URI : $NEO4J_URI"
echo "  REDIS_URL : $REDIS_URL"
echo ""

exec /opt/homebrew/Caskroom/miniforge/base/bin/python3 -m netcortex.worker
