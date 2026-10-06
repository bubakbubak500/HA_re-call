"""REST identity is trusted only from the BFF.

The backend is reachable directly, not just through the web BFF (its MCP host is
public), so outside dev mode `resolve_identity` must reject forged X-User-*
headers unless they arrive with the BFF shared secret — in entra and betterauth
mode alike. Exercises src/auth.py (`resolve_identity`).
"""
from __future__ import annotations

import pytest
from starlette.requests import Request

from src import auth, config

SECRET = "s3cret-from-the-environment"
OID = "f50c6cd3-c8e6-4ec5-acbf-1a2edee50740"
UPN = "someone@example.com"


def _request(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/me",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
    )


def _identity_headers(**extra: str) -> dict[str, str]:
    return {"x-user-id": OID, "x-user-upn": UPN, "x-user-name": "Some One", **extra}


@pytest.fixture(params=["entra", "betterauth"])
def mode(request, monkeypatch):
    monkeypatch.setattr(config, "AUTH_MODE", request.param)
    monkeypatch.setattr(config, "BFF_SHARED_SECRET", SECRET)
    return request.param


def test_accepts_identity_with_matching_secret(mode):
    ident = auth.resolve_identity(_request(_identity_headers(**{"x-bff-secret": SECRET})))
    assert ident == {"oid": OID, "upn": UPN, "name": "Some One"}


def test_rejects_forged_identity_without_secret(mode):
    assert auth.resolve_identity(_request(_identity_headers())) is None


def test_rejects_wrong_secret(mode):
    req = _request(_identity_headers(**{"x-bff-secret": "guess"}))
    assert auth.resolve_identity(req) is None


def test_fails_closed_when_secret_unset(mode, monkeypatch):
    monkeypatch.setattr(config, "BFF_SHARED_SECRET", "")
    # Even a request presenting an empty secret must not match an unset one.
    req = _request(_identity_headers(**{"x-bff-secret": ""}))
    assert auth.resolve_identity(req) is None


def test_secret_without_identity_is_still_unauthenticated(mode):
    assert auth.resolve_identity(_request({"x-bff-secret": SECRET})) is None


def test_dev_mode_needs_no_secret(monkeypatch):
    monkeypatch.setattr(config, "AUTH_MODE", "dev")
    monkeypatch.setattr(config, "BFF_SHARED_SECRET", "")
    ident = auth.resolve_identity(_request({}))
    assert ident is not None and ident["oid"] == config.DEV_USER["oid"]
