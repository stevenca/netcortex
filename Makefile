# =============================================================================
# NetCortex — Developer Makefile
# =============================================================================
REGISTRY    ?= localhost:32000
TAG         ?= latest
RELEASE     ?= netcortex
NAMESPACE   ?= netcortex
HELM_VALUES  = deploy/helm/values.yaml
LOCAL_VALUES = deploy/values-local.yaml

.PHONY: help build push bootstrap-secret helm-install helm-upgrade helm-uninstall \
        status logs shell

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
	docker build --build-arg EXTRAS=all \
	  -t $(REGISTRY)/netcortex:$(TAG) \
	  -f docker/Dockerfile .

push: build
	docker push $(REGISTRY)/netcortex:$(TAG)

# -----------------------------------------------------------------------------
# Bootstrap secret — created from .env, never stored in Helm history
# -----------------------------------------------------------------------------
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
	  --wait --timeout 10m

helm-upgrade: push
	microk8s helm3 upgrade $(RELEASE) deploy/helm/ \
	  --namespace $(NAMESPACE) \
	  -f $(HELM_VALUES) \
	  -f $(LOCAL_VALUES) \
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
