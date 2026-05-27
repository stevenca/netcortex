# =============================================================================
# NetCortex — Developer Makefile
# =============================================================================
REGISTRY    ?= localhost:32000
# Use the package version from pyproject.toml as the image tag. This gives
# every build a unique tag, so ``helm upgrade`` sees a spec change in the
# Deployment and triggers a real rollout. Falling back to ``:latest`` (the
# old behaviour) silently re-uses the previously-pulled image: the helm
# release reports success but pods keep running stale code. Override with
# ``make helm-upgrade TAG=foo`` if you want a one-off custom tag.
PKG_VERSION := $(shell sed -n 's/^version[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' pyproject.toml | head -1)
TAG         ?= $(PKG_VERSION)
RELEASE     ?= netcortex
NAMESPACE   ?= netcortex
HELM_VALUES  = deploy/helm/values.yaml
LOCAL_VALUES = deploy/values-local.yaml

.PHONY: help build push bootstrap-secret certmgr-secret helm-install helm-upgrade \
        helm-uninstall status logs shell

help:
	@echo ""
	@echo "  make build              Build the Docker image"
	@echo "  make push               Build and push to the local registry"
	@echo "  make bootstrap-secret   Create/update the k8s bootstrap Secret from .env"
	@echo "  make helm-install       Full deploy: push + secret + helm install"
	@echo "  make helm-upgrade       Re-deploy after code changes (push + helm upgrade)"
	@echo "  make helm-uninstall     Remove the Helm release (keeps PVCs)"
	@echo "  make status             Show pod/pvc/ingress status"
	@echo "  make logs               Tail netcortex-web logs"
	@echo "  make shell              Open a shell in the running netcortex-web pod"
	@echo ""

# -----------------------------------------------------------------------------
# Image
# -----------------------------------------------------------------------------
build:
	@test -n "$(PKG_VERSION)" || (echo "ERROR: could not parse version from pyproject.toml" && exit 1)
	docker build --build-arg EXTRAS=all \
	  -t $(REGISTRY)/netcortex:$(TAG) \
	  -t $(REGISTRY)/netcortex:latest \
	  -f docker/Dockerfile .

push: build
	docker push $(REGISTRY)/netcortex:$(TAG)
	docker push $(REGISTRY)/netcortex:latest

# -----------------------------------------------------------------------------
# Secrets — created out-of-band, never stored in Helm history
# -----------------------------------------------------------------------------
certmgr-secret:
	@export $$(grep -v '^#' .env | xargs -d '\n') && \
	microk8s kubectl create secret generic route53-credentials \
	  --from-literal=ACCESS_KEY_ID="$$AWS_ACCESS_KEY_ID" \
	  --from-literal=SECRET_ACCESS_KEY="$$AWS_SECRET_ACCESS_KEY" \
	  -n cert-manager \
	  --dry-run=client -o yaml | microk8s kubectl apply -f - && \
	microk8s kubectl apply -f deploy/cert-manager/cluster-issuer.yaml

# Usage: make certmgr-patch-zoneid ZONE_ID=Z1234567890ABC
certmgr-patch-zoneid:
	@test -n "$(ZONE_ID)" || (echo "Usage: make certmgr-patch-zoneid ZONE_ID=Z..." && exit 1)
	@sed -i 's|# hostedZoneID: Z1234567890ABC.*|hostedZoneID: $(ZONE_ID)|g' \
	  deploy/cert-manager/cluster-issuer.yaml
	microk8s kubectl apply -f deploy/cert-manager/cluster-issuer.yaml
	@echo "✓ ClusterIssuers updated with hostedZoneID=$(ZONE_ID)"

bootstrap-secret:
	microk8s kubectl create namespace $(NAMESPACE) --dry-run=client -o yaml \
	  | microk8s kubectl apply -f -
	microk8s kubectl create secret generic netcortex-bootstrap \
	  --from-env-file=.env \
	  --namespace $(NAMESPACE) \
	  --dry-run=client -o yaml \
	  | microk8s kubectl apply -f -

# -----------------------------------------------------------------------------
# Helm
# -----------------------------------------------------------------------------
helm-install: push bootstrap-secret
	microk8s helm3 upgrade --install $(RELEASE) deploy/helm/ \
	  --namespace $(NAMESPACE) \
	  --create-namespace \
	  -f $(HELM_VALUES) \
	  -f $(LOCAL_VALUES) \
	  --set image.tag=$(TAG) \
	  --wait --timeout 10m

# Override ``image.tag`` to the version-pinned tag pushed by ``push`` above
# so the Deployment spec actually changes, which is what forces Kubernetes
# to roll the pods. Plain ``:latest`` with ``pullPolicy: Always`` does NOT
# trigger a rollout on its own — the tag string has to change for k8s to
# diff the spec.
helm-upgrade: push
	microk8s helm3 upgrade $(RELEASE) deploy/helm/ \
	  --namespace $(NAMESPACE) \
	  -f $(HELM_VALUES) \
	  -f $(LOCAL_VALUES) \
	  --set image.tag=$(TAG) \
	  --wait --timeout 10m

helm-uninstall:
	microk8s helm3 uninstall $(RELEASE) --namespace $(NAMESPACE)

# -----------------------------------------------------------------------------
# Ops shortcuts
# -----------------------------------------------------------------------------
status:
	@echo "=== Pods ==="
	microk8s kubectl get pods -n $(NAMESPACE)
	@echo ""
	@echo "=== PVCs ==="
	microk8s kubectl get pvc -n $(NAMESPACE)
	@echo ""
	@echo "=== Ingress ==="
	microk8s kubectl get ingress -n $(NAMESPACE)

logs:
	microk8s kubectl logs -n $(NAMESPACE) \
	  -l "app.kubernetes.io/name=netcortex,app.kubernetes.io/component=web" \
	  --tail=100 -f

shell:
	microk8s kubectl exec -it -n $(NAMESPACE) \
	  $$(microk8s kubectl get pod -n $(NAMESPACE) \
	     -l "app.kubernetes.io/name=netcortex,app.kubernetes.io/component=web" \
	     -o jsonpath='{.items[0].metadata.name}') \
	  -- /bin/bash
