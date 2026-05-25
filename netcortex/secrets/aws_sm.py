"""AWS Secrets Manager backend.

Bootstrap env vars required (minimal — IAM role auth needs only region):

    SECRET_BACKEND=aws_sm
    AWS_REGION=us-east-1          # required
    NC_SECRET_PREFIX=netcortex    # optional, default "netcortex"

    # Only needed when NOT running on EC2/ECS/Lambda with an IAM role:
    AWS_ACCESS_KEY_ID=...
    AWS_SECRET_ACCESS_KEY=...
    AWS_SESSION_TOKEN=...         # if using temporary credentials

Secret name format in AWS SM:
    {prefix}/{path}    e.g.  netcortex/core
                             netcortex/adapters/meraki
                             netcortex/devices/host/sw-bldga-01

Secrets are stored as JSON strings. Each secret is a JSON object whose
keys map directly to the values NetCortex needs.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog

from netcortex.secrets.base import SecretBackend, SecretBackendError, SecretNotFoundError

log = structlog.get_logger(__name__)


class AwsSecretsManagerBackend(SecretBackend):
    """AWS Secrets Manager secret backend.

    Uses boto3 in a thread-pool executor to avoid blocking the event loop.
    On EC2/ECS/EKS/Lambda, boto3 picks up credentials automatically from the
    instance/task/pod IAM role — no explicit credentials required.
    """

    def __init__(
        self,
        region: str,
        prefix: str = "netcortex",
        cache_ttl: int = 300,
        endpoint_url: str | None = None,
    ) -> None:
        super().__init__(prefix=prefix, cache_ttl=cache_ttl)
        self._region = region
        self._endpoint_url = endpoint_url  # allows LocalStack for testing
        self._client = None  # lazy init

    def _get_client(self):  # type: ignore[return]
        """Lazy boto3 client init — deferred so the import error is clear."""
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:
                raise SecretBackendError(
                    "boto3 is required for the AWS Secrets Manager backend. "
                    "Install it with: pip install boto3"
                ) from exc
            kwargs: dict[str, Any] = {"region_name": self._region}
            if self._endpoint_url:
                kwargs["endpoint_url"] = self._endpoint_url
            self._client = boto3.client("secretsmanager", **kwargs)
        return self._client

    async def _fetch(self, full_path: str) -> dict[str, Any]:
        client = self._get_client()
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.get_secret_value(SecretId=full_path),
            )
            raw = result.get("SecretString") or ""
            return json.loads(raw)
        except Exception as exc:
            # botocore raises ClientError; the specific class is not always
            # available as a typed exception attribute on the client.
            error_code: str = ""
            if hasattr(exc, "response") and isinstance(exc.response, dict):  # type: ignore[union-attr]
                error_code = exc.response.get("Error", {}).get("Code", "")  # type: ignore[union-attr]
            if error_code == "ResourceNotFoundException":
                raise SecretNotFoundError(f"AWS SM secret not found: {full_path!r}")
            if error_code in ("AccessDeniedException", "AccessDenied"):
                raise SecretBackendError(
                    f"Access denied reading AWS SM secret {full_path!r}: {exc}"
                ) from exc
            raise SecretBackendError(
                f"AWS SM error reading {full_path!r}: {exc}"
            ) from exc

    async def _store(self, full_path: str, values: dict[str, Any]) -> None:
        client = self._get_client()
        secret_string = json.dumps(values)

        def _put() -> None:
            try:
                client.put_secret_value(SecretId=full_path, SecretString=secret_string)
            except client.exceptions.ResourceNotFoundException:
                # Secret doesn't exist yet — create it
                client.create_secret(Name=full_path, SecretString=secret_string)

        try:
            await asyncio.get_event_loop().run_in_executor(None, _put)
        except Exception as exc:
            raise SecretBackendError(
                f"AWS SM error writing {full_path!r}: {exc}"
            ) from exc

    async def health_check(self) -> dict:
        client = self._get_client()
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.list_secrets(MaxResults=1),
            )
            return {"status": "ok", "backend": "aws_sm", "region": self._region}
        except Exception as exc:
            return {"status": "error", "backend": "aws_sm", "message": str(exc)}
