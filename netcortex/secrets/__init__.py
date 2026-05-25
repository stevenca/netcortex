"""Secret backend factory.

Usage:
    from netcortex.secrets import get_secret_backend
    backend = get_secret_backend()           # reads SECRET_BACKEND env var
    core = await backend.get_core()          # fetches netcortex/core
"""

from __future__ import annotations

import os

from netcortex.secrets.base import SecretBackend, SecretNotFoundError, SecretBackendError, SecretPaths

__all__ = ["get_secret_backend", "SecretBackend", "SecretNotFoundError", "SecretBackendError", "SecretPaths"]

_instance: SecretBackend | None = None


def get_secret_backend() -> SecretBackend:
    """Return the singleton secret backend, constructing it on first call.

    Reads from environment:
        SECRET_BACKEND   : "aws_sm" | "vault"   (required)
        NC_SECRET_PREFIX : secret path prefix     (default: "netcortex")
        NC_SECRET_CACHE_TTL: cache TTL seconds    (default: 300)

    See aws_sm.py and vault.py for backend-specific env vars.
    """
    global _instance
    if _instance is not None:
        return _instance

    backend_name = os.environ.get("SECRET_BACKEND", "").lower()
    prefix = os.environ.get("NC_SECRET_PREFIX", "netcortex")
    cache_ttl = int(os.environ.get("NC_SECRET_CACHE_TTL", "300"))

    if backend_name == "aws_sm":
        from netcortex.secrets.aws_sm import AwsSecretsManagerBackend
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        if not region:
            raise SecretBackendError(
                "AWS_REGION must be set when SECRET_BACKEND=aws_sm"
            )
        endpoint_url = os.environ.get("AWS_SM_ENDPOINT_URL")  # for LocalStack
        _instance = AwsSecretsManagerBackend(
            region=region,
            prefix=prefix,
            cache_ttl=cache_ttl,
            endpoint_url=endpoint_url or None,
        )

    elif backend_name == "vault":
        from netcortex.secrets.vault import VaultBackend
        addr = os.environ.get("VAULT_ADDR")
        if not addr:
            raise SecretBackendError(
                "VAULT_ADDR must be set when SECRET_BACKEND=vault"
            )
        _instance = VaultBackend(
            addr=addr,
            mount=os.environ.get("VAULT_MOUNT", "secret"),
            prefix=prefix,
            cache_ttl=cache_ttl,
            token=os.environ.get("VAULT_TOKEN"),
            role_id=os.environ.get("VAULT_ROLE_ID"),
            secret_id=os.environ.get("VAULT_SECRET_ID"),
            auth_method=os.environ.get("VAULT_AUTH_METHOD", "token"),
            aws_role=os.environ.get("VAULT_AWS_ROLE"),
            k8s_role=os.environ.get("VAULT_K8S_ROLE"),
            verify_ssl=os.environ.get("VAULT_SKIP_VERIFY", "").lower() != "true",
        )

    else:
        raise SecretBackendError(
            f"SECRET_BACKEND must be 'aws_sm' or 'vault', got: {backend_name!r}. "
            "Set the SECRET_BACKEND environment variable."
        )

    return _instance


def reset_backend() -> None:
    """Reset the singleton — used in tests."""
    global _instance
    _instance = None
