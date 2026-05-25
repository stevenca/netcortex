#!/usr/bin/env bash
# Build the netcortex image and push it to the local microk8s registry.
# Run from the repo root:  ./deploy/build-push.sh
set -euo pipefail

REGISTRY="${REGISTRY:-localhost:32000}"
TAG="${TAG:-latest}"
IMAGE="$REGISTRY/netcortex:$TAG"

cd "$(dirname "$0")/.."

echo "→ Building $IMAGE"
docker build \
  --build-arg EXTRAS=all \
  -t "$IMAGE" \
  -f docker/Dockerfile \
  .

echo "→ Pushing $IMAGE"
docker push "$IMAGE"

echo "✓ Done: $IMAGE"
