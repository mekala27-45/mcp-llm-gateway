import base64
import json
import time
from typing import Any

import jwt
import pytest

from mcp_gateway.auth import Principal, Unauthenticated, authenticate, fingerprint, mint_token
from mcp_gateway.settings import Settings

# At least 64 bytes, so the HS512 case is refused for its algorithm and not its key size.
SETTINGS = Settings(jwt_secret="auth-test-secret-" + "0123456789abcdef" * 3)


def claims(**changes: Any) -> dict[str, Any]:
    now = int(time.time())
    base = {
        "sub": "user-1",
        "role": "viewer",
        "iss": SETTINGS.jwt_issuer,
        "aud": SETTINGS.jwt_audience,
        "iat": now,
        "exp": now + 300,
    }
    return {k: v for k, v in {**base, **changes}.items() if v is not None}


def signed(payload: dict[str, Any], key: str = SETTINGS.jwt_secret, alg: str = "HS256") -> str:
    return jwt.encode(payload, key, algorithm=alg)


def unsigned(payload: dict[str, Any]) -> str:
    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none', 'typ': 'JWT'})}.{part(payload)}."


def check(token: str) -> Principal:
    return authenticate([f"Bearer {token}"], SETTINGS)


def test_a_valid_token_gives_the_role_and_a_fingerprint() -> None:
    token = mint_token(SETTINGS, "admin", "alice", 60)
    principal = check(token)
    assert principal == Principal(subject="alice", role="admin", fingerprint=fingerprint(token))
    assert len(principal.fingerprint) == 8
    assert principal.fingerprint not in token


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(signed(claims(exp=int(time.time()) - 60)), id="expired"),
        pytest.param(unsigned(claims(role="admin")), id="algorithm none"),
        pytest.param(signed(claims(), key="another-secret-0123456789abcdef01"), id="another key"),
        pytest.param(signed(claims(), alg="HS512"), id="HS512 with the right key"),
        pytest.param(signed(claims(aud="some-other-service")), id="wrong audience"),
        pytest.param(signed(claims(iss="someone-else")), id="wrong issuer"),
        pytest.param(signed(claims(role="superuser")), id="unknown role"),
        pytest.param(signed(claims(role=None)), id="no role"),
        pytest.param(signed(claims(sub=None)), id="no subject"),
        pytest.param(signed(claims(exp=None)), id="no expiry"),
        pytest.param("not-a-jwt", id="garbage"),
    ],
)
def test_bad_tokens_are_refused(token: str) -> None:
    with pytest.raises(Unauthenticated):
        check(token)


def test_a_tampered_payload_is_refused() -> None:
    header, _, signature = mint_token(SETTINGS, "viewer", "bob", 60).split(".")
    forged = base64.urlsafe_b64encode(json.dumps(claims(role="admin")).encode()).rstrip(b"=")
    with pytest.raises(Unauthenticated):
        check(f"{header}.{forged.decode()}.{signature}")


@pytest.mark.parametrize(
    "values",
    [[], ["Basic dXNlcjpwYXNz"], ["Bearer "], ["Bearer a", "Bearer b"]],
    ids=["missing", "basic", "empty bearer", "two headers"],
)
def test_the_authorization_header_must_be_exactly_one_bearer_token(values: list[str]) -> None:
    with pytest.raises(Unauthenticated):
        authenticate(values, SETTINGS)


def test_settings_refuse_a_short_secret_and_need_one_in_the_environment() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        Settings(jwt_secret="short")
    with pytest.raises(ValueError, match="GATEWAY_JWT_SECRET is required"):
        Settings.from_env({})
    settings = Settings.from_env(
        {"GATEWAY_JWT_SECRET": "x" * 32, "GATEWAY_HIDE_ADMIN_TOOLS": "true"}
    )
    assert settings.hide_admin_tools
    assert not settings.listed_tools_only
