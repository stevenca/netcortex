"""HashiCorp Vault backend (KV v2).

Bootstrap env vars required:

    SECRET_BACKEND=vault
    VAULT_ADDR=https://vault.example.com   # required
    NC_SECRET_PREFIX=netcortex             # optional, default "netcortex"
    VAULT_MOUNT=secret                     # KV v2 mount path, default "secret"

Authentication — choose one:

    # Token auth (simplest, good for development):
    VAULT_TOKEN=s.xxxxxxxxxx

    # AppRole auth (recommended for services):
    VAULT_ROLE_ID=<role-id>
    VAULT_SECRET_ID=<secret-id>

    # AWS IAM auth (for services running on AWS):
    VAULT_AUTH_METHOD=aws
    VAULT_AWS_ROLE=netcortex

    # Kubernetes auth (for K8s deployments):
    VAULT_AUTH_METHOD=kubernetes
    VAULT_K8S_ROLE=netcortex

Secret path format in Vault (KV v2):
    {mount}/data/{prefix}/{path}
    e.g.  secret/data/netcortex/core
          secret/data/netcortex/adapters/meraki

The {prefix}/{path} portion mirrors the AWS SM naming exactly so configs
are portable between backends.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from netcortex.secrets.base import SecretBackend, SecretBackendError, SecretNotFoundError

log = structlog.get_logger(__name__)


class VaultBackend(SecretBackend):
    """HashiCorp Vault KV v2 secret backend using hvac."""

    def __init__(
        self,
        addr: str,
        mount: str = "secret",
        prefix: str = "netcortex",
        cache_ttl: int = 300,
        # Auth options — provide one of the following sets:
        token: str | None = None,
        role_id: str | None = None,
        secret_id: str | None = None,
        auth_method: str = "token",    # token | approle | aws | kubernetes
        aws_role: str | None = None,
        k8s_role: str | None = None,
        k8s_jwt_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/token",
        verify_ssl: bool = True,
    ) -> None:
        super().__init__(prefix=prefix, cache_ttl=cache_ttl)
        self._addr = addr.rstrip("/")
        self._mount = mount
        self._token = token
        self._role_id = role_id
        self._secret_id = secret_id
        self._auth_method = auth_method
        self._aws_role = aws_role
        self._k8s_role = k8s_role
        self._k8s_jwt_path = k8s_jwt_path
        self._verify_ssl = verify_ssl
        self._client = None  # lazy init

    def _get_client(self):  # type: ignore[return]
        """Lazy hvac client init with auth."""
        if self._client is not None:
            return self._client
        try:
            import hvac
        except ImportError as exc:
            raise SecretBackendError(
                "hvac is required for the Vault backend. "
                "Install it with: pip install hvac"
            ) from exc

        client = hvac.Client(url=self._addr, verify=self._verify_ssl)

        method = self._auth_method.lower()

        if method == "token":
            if not self._token:
                raise SecretBackendError(
                    "VAULT_TOKEN is required for token auth method"
                )
            client.token = self._token

        elif method == "approle":
            if not self._role_id or not self._secret_id:
                raise SecretBackendError(
                    "VAULT_ROLE_ID and VAULT_SECRET_ID are required for approle auth"
                )
            resp = client.auth.approle.login(
                role_id=self._role_id,
                secret_id=self._secret_id,
            )
            client.token = resp["auth"]["client_token"]

        elif method == "aws":
            if not self._aws_role:
                raise SecretBackendError(
                    "VAULT_AWS_ROLE is required for AWS IAM auth"
                )
            import boto3
            session = boto3.Session()
            credentials = session.get_credentials().get_frozen_credentials()
            client.auth.aws.iam_login(
                access_key=credentials.access_key,
                secret_key=credentials.secret_key,
                session_token=credentials.token,
                role=self._aws_role,
            )

        elif method == "kubernetes":
            if not self._k8s_role:
                raise SecretBackendError(
                    "VAULT_K8S_ROLE is required for Kubernetes auth"
                )
            with open(self._k8s_jwt_path) as f:
                jwt = f.read().strip()
            client.auth.kubernetes.login(role=self._k8s_role, jwt=jwt)

        else:
            raise SecretBackendError(f"Unknown Vault auth method: {self._auth_method!r}")

        if not client.is_authenticated():
            raise SecretBackendError("Vault authentication failed — client is not authenticated")

        self._client = client
        return client

    def _kv_path(self, full_path: str) -> str:
        """Convert full path to Vault KV v2 path (strip prefix from mount)."""
        # full_path is already  prefix/sub/path (e.g. netcortex/adapters/meraki)
        return full_path

    async def _fetch(self, full_path: str) -> dict[str, Any]:
        client = self._get_client()
        kv_path = self._kv_path(full_path)

        def _read() -> dict[str, Any]:
            try:
                resp = client.secrets.kv.v2.read_secret_version(
                    path=kv_path, mount_point=self._mount, raise_on_deleted_version=True
                )
                return resp["data"]["data"]  # type: ignore[index]
            except Exception as exc:
                msg = str(exc)
                if "404" in msg or "does not exist" in msg.lower():
                    raise SecretNotFoundError(f"Vault secret not found: {kv_path!r}")
                raise SecretBackendError(f"Vault error reading {kv_path!r}: {exc}") from exc

        try:
            return await asyncio.get_event_loop().run_in_executor(None, _read)
        except (SecretNotFoundError, SecretBackendError):
            raise
        except Exception as exc:
            raise SecretBackendError(f"Vault fetch error for {kv_path!r}: {exc}") from exc

    async def _store(self, full_path: str, values: dict[str, Any]) -> None:
        client = self._get_client()
        kv_path = self._kv_path(full_path)

        def _write() -> None:
            client.secrets.kv.v2.create_or_update_secret(
                path=kv_path,
                secret=values,
                mount_point=self._mount,
            )

        try:
            await asyncio.get_event_loop().run_in_executor(None, _write)
        except Exception as exc:
            raise SecretBackendError(
                f"Vault error writing {kv_path!r}: {exc}"
            ) from exc

    async def health_check(self) -> dict:
        try:
            client = self._get_client()

            def _check() -> bool:
                return client.is_authenticated()  # type: ignore[return-value]

            authed = await asyncio.get_event_loop().run_in_executor(None, _check)
            if authed:
                return {"status": "ok", "backend": "vault", "addr": self._addr}
            return {"status": "error", "backend": "vault", "message": "not authenticated"}
        except Exception as exc:
            return {"status": "error", "backend": "vault", "message": str(exc)}
