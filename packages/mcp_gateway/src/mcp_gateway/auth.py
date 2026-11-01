"""Bearer token checks. The role comes from a signed claim and nowhere else."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import jwt

from mcp_gateway.settings import Settings

ROLES = ("admin", "viewer")


@dataclass(frozen=True)
class Principal:
    subject: str
    role: str
    fingerprint: str


class Unauthenticated(Exception):
    """No usable token. The reason is for the log, never for the client."""

    def __init__(self, reason: str, fingerprint: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.fingerprint = fingerprint


def fingerprint(token: str) -> str:
    """First eight hex characters of the token SHA-256: enough to correlate, safe to log."""
    return hashlib.sha256(token.encode()).hexdigest()[:8]


def authenticate(authorization: list[str], settings: Settings) -> Principal:
    """Check the Authorization header values of one request and return who is calling."""
    if not authorization:
        raise Unauthenticated("no Authorization header")
    if len(authorization) > 1:
        raise Unauthenticated("more than one Authorization header")
    scheme, _, token = authorization[0].partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise Unauthenticated("not a bearer token")
    token_fp = fingerprint(token)
    try:
        # Only HS256 is accepted, so "none" and algorithm confusion tricks fail here.
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise Unauthenticated(f"token rejected: {type(exc).__name__}", token_fp) from None
    role = claims.get("role")
    subject = claims.get("sub")
    if role not in ROLES:
        raise Unauthenticated("role claim must be admin or viewer", token_fp)
    if not isinstance(subject, str):
        raise Unauthenticated("sub claim must be a string", token_fp)
    return Principal(subject=subject, role=role, fingerprint=token_fp)


def mint_token(
    settings: Settings, role: str, subject: str, ttl_seconds: int, now: int | None = None
) -> str:
    """A signed token for local use: the demo, the tests and the token command."""
    issued = int(time.time()) if now is None else now
    claims = {
        "sub": subject,
        "role": role,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": issued,
        "exp": issued + ttl_seconds,
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")
