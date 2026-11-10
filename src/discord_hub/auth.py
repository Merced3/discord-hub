"""Project identity for the hub API and callback signatures.

Tokens are read from settings and never included in errors or logs. Network
access remains a separate Tailscale/Docker boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping
from dataclasses import dataclass


class AuthenticationError(ValueError):
    """The supplied API credential is missing, malformed, or unknown."""


@dataclass(frozen=True, slots=True)
class Principal:
    project_id: str | None = None
    operator: bool = False


LEGACY_PRINCIPAL = Principal()
OPERATOR_PRINCIPAL = Principal(operator=True)


class HubAuth:
    def __init__(
        self,
        *,
        mode: str = "disabled",
        project_tokens: Mapping[str, str] | None = None,
        operator_token: str | None = None,
    ) -> None:
        self.mode = mode
        self._project_tokens = dict(project_tokens or {})
        self._operator_token = operator_token

    def authenticate(self, authorization: str | None) -> Principal:
        if self.mode == "disabled":
            return LEGACY_PRINCIPAL
        if authorization is None:
            if self.mode == "optional":
                return LEGACY_PRINCIPAL
            raise AuthenticationError("a bearer token is required")
        scheme, separator, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not separator or not token:
            raise AuthenticationError("a valid bearer token is required")
        if self._operator_token and hmac.compare_digest(token, self._operator_token):
            return OPERATOR_PRINCIPAL
        for project_id, expected in self._project_tokens.items():
            if hmac.compare_digest(token, expected):
                return Principal(project_id=project_id)
        raise AuthenticationError("a valid bearer token is required")

    def signature_headers(self, project_id: str | None, body: bytes) -> dict[str, str]:
        """Sign known-project callbacks; unsigned legacy routes remain possible
        during the accept-both rollout."""
        if not project_id:
            return {}
        token = self._project_tokens.get(project_id)
        if not token:
            return {}
        timestamp = str(int(time.time()))
        digest = hmac.new(
            token.encode(), timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        return {
            "X-Discord-Hub-Timestamp": timestamp,
            "X-Discord-Hub-Signature": f"v1={digest}",
            "X-Discord-Hub-Project": project_id,
        }
