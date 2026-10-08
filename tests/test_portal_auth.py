"""Synthetic tests for Journal Check external-gateway mode.

Legacy behaviour must remain unchanged when the gateway mode is off. Gateway
mode validates the signed portal context, rejects forged/expired/wrongly-bound
contexts, and keeps the same business security (Origin + CSRF).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from journalcheck.server.app import BASE, COOKIE, create_app  # noqa: E402
from journalcheck.server.security import private_write  # noqa: E402

OWNER = "owner@example.test"
ORIGIN = {"Origin": "https://journal.example.test"}
GATEWAY_VERSION = "gw1"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign(key: bytes, login: str, sid: str, *, exp_offset: int = 120, iat_offset: int = 0) -> str:
    now = int(time.time())
    payload = {"v": 1, "sub": login, "sid": sid, "iat": now + iat_offset, "exp": now + exp_offset}
    body = b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = hmac.new(key, f"{GATEWAY_VERSION}.{body}".encode(), hashlib.sha256).digest()
    return f"{GATEWAY_VERSION}.{body}.{b64url(sig)}"


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    key = bytes(range(32))
    key_path = tmp_path / "gateway.key"
    private_write(key_path, (b64url(key) + "\n").encode())
    monkeypatch.setenv("JOURNALCHECK_RUNTIME", str(tmp_path))
    monkeypatch.setenv("JOURNALCHECK_GATEWAY_MODE", "1")
    monkeypatch.setenv("JOURNALCHECK_GATEWAY_KEY", str(key_path))
    app = create_app(tmp_path)
    app.state.store.bootstrap(OWNER)
    app.state.store.set_meta("must_change_password", "1")
    app.state.store.set_meta("public_origin", "https://journal.example.test")
    client = TestClient(app, base_url="https://journal.example.test", root_path=BASE)
    headers = {"X-Journal-Gateway": sign(key, OWNER, "sess-1")}
    try:
        yield client, app, key, headers, tmp_path
    finally:
        client.close()


def test_gateway_requires_valid_signed_context(gateway):
    client, _, key, headers, _ = gateway
    assert client.get("/journal/api/session").status_code == 403
    assert client.get("/journal/api/session", headers={"X-Journal-Gateway": "gw1.not.a.real.token"}).status_code == 403
    assert client.get("/journal/api/session", headers=headers).status_code == 200


def test_gateway_session_reports_authenticated_and_usable_csrf(gateway):
    client, _, key, headers, _ = gateway
    body = client.get("/journal/api/session", headers=headers).json()
    assert body["authenticated"] is True
    assert body["gateway"] is True
    assert body["csrf"]
    # No separate journal password prompt or legacy cookie is required.
    assert COOKIE not in client.cookies
    # CSRF from the gateway is accepted on a mutation.
    put = client.put("/journal/api/settings", json={"interval_minutes": 120},
                     headers={**headers, **ORIGIN, "X-CSRF-Token": body["csrf"]})
    assert put.status_code == 200


def test_gateway_wrong_bound_login_rejected(gateway):
    client, _, key, _, _ = gateway
    wrong = sign(key, "someone-else@example.test", "sess-2")
    assert client.get("/journal/api/session", headers={"X-Journal-Gateway": wrong}).status_code == 403


def test_gateway_expired_context_rejected(gateway):
    client, _, key, _, _ = gateway
    expired = sign(key, OWNER, "sess-3", exp_offset=-30)
    assert client.get("/journal/api/session", headers={"X-Journal-Gateway": expired}).status_code == 403


def test_gateway_wrong_key_rejected(gateway):
    client, _, _, _, _ = gateway
    forged = sign(b"x" * 32, OWNER, "sess-4")
    assert client.get("/journal/api/session", headers={"X-Journal-Gateway": forged}).status_code == 403


def test_gateway_requires_unsigned_header_rejected(gateway):
    client, _, _, _, _ = gateway
    assert client.get("/journal/api/session",
                      headers={"Tailscale-User-Login": OWNER}).status_code == 403


def test_gateway_mutations_require_origin_and_csrf(gateway):
    client, _, key, headers, _ = gateway
    # Valid gateway but cross-origin is refused.
    assert client.post("/journal/api/refresh", json={},
                       headers={**headers, "Origin": "https://evil.test",
                                "X-CSRF-Token": "x"}).status_code == 403
    # Valid gateway and origin but no CSRF is refused.
    assert client.post("/journal/api/refresh", json={}, headers={**headers, **ORIGIN}).status_code == 403


def test_gateway_login_and_password_routes_removed(gateway):
    client, _, _, headers, _ = gateway
    csrf = client.get("/journal/api/session", headers=headers).json()["csrf"]
    # Origin is required before the request reaches the (removed) route.
    assert client.post("/journal/api/login", json={"password": "x"},
                       headers={**headers, **ORIGIN}).status_code == 404
    assert client.post("/journal/api/password", json={},
                       headers={**headers, **ORIGIN, "X-CSRF-Token": csrf}).status_code == 404


def test_gateway_logout_points_to_central(gateway):
    client, _, _, headers, _ = gateway
    csrf = client.get("/journal/api/session", headers=headers).json()["csrf"]
    body = client.post("/journal/api/logout", json={}, headers={**headers, **ORIGIN,
                                                               "X-CSRF-Token": csrf}).json()
    assert body.get("gateway_logout") == "/portal-auth/logout"


def test_gateway_fails_closed_without_key(tmp_path, monkeypatch):
    monkeypatch.setenv("JOURNALCHECK_GATEWAY_MODE", "1")
    monkeypatch.setenv("JOURNALCHECK_GATEWAY_KEY", str(tmp_path / "missing.key"))
    with pytest.raises(RuntimeError):
        create_app(tmp_path)


def test_gateway_mode_does_not_touch_legacy_password_hash(gateway):
    _, app, _, _, _ = gateway
    before = app.state.store.meta("admin_password")
    assert before
    # Gateway login route removed, but the stored hash and identity are untouched.
    assert app.state.store.meta("allowed_login") == OWNER


# -- legacy mode unchanged ---------------------------------------------------
def test_legacy_mode_still_uses_password(tmp_path, monkeypatch):
    monkeypatch.delenv("JOURNALCHECK_GATEWAY_MODE", raising=False)
    monkeypatch.delenv("JOURNALCHECK_GATEWAY_KEY", raising=False)
    app = create_app(tmp_path)
    app.state.store.bootstrap(OWNER)
    app.state.store.set_meta("public_origin", "https://journal.example.test")
    password = (tmp_path / "initial-password.txt").read_text(encoding="utf-8").strip()
    client = TestClient(app, base_url="https://journal.example.test", root_path=BASE)
    try:
        owner = {"Tailscale-User-Login": OWNER}
        assert client.get("/journal/api/session", headers=owner).json()["authenticated"] is False
        response = client.post("/journal/api/login", json={"password": password},
                               headers={**owner, **ORIGIN})
        assert response.status_code == 200
        assert response.json()["gateway"] is False
        assert client.get("/journal/api/session", headers=owner).json()["authenticated"] is True
    finally:
        client.close()
