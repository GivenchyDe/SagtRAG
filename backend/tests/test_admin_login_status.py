"""Regression: disabled accounts must fail even when timing-padding verifies.

Found during full QA on 2026-10-30; see MD/FULL_TEST_REPORT_20261030.md.
"""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import admin_auth
from app.schemas import AdminLoginRequest


class LoginSession:
    def __init__(self, admin):
        self.admin = admin
        self.events = []

    async def scalar(self, query):
        return self.admin

    def add(self, event):
        self.events.append(event)

    async def commit(self):
        pass


def prepare(monkeypatch, account_status):
    tenant_id = uuid4()
    monkeypatch.setattr(admin_auth, "get_settings", lambda: SimpleNamespace(we_com_tenant_id=tenant_id))
    async def context(*args, **kwargs):
        pass
    monkeypatch.setattr(admin_auth, "set_tenant_rls_context", context)
    monkeypatch.setattr(admin_auth, "password_hasher", SimpleNamespace(verify=lambda encoded, password: encoded == f"hash:{password}"))
    monkeypatch.setattr(admin_auth, "DUMMY_PASSWORD_HASH", "hash:timing-fixture")
    monkeypatch.setattr(admin_auth, "create_access_token", lambda **kwargs: "fixture-token")
    admin = None if account_status is None else SimpleNamespace(id=uuid4(),tenant_id=tenant_id,
        username="fixture",status=account_status,password_hash="hash:valid-fixture",
        role="tenant_admin",must_change_password=False)
    return LoginSession(admin)


@pytest.mark.parametrize("account_status,password", [
    ("disabled", "timing-fixture"),
    ("disabled", "valid-fixture"),
    (None, "timing-fixture"),
])
async def test_inactive_or_missing_admin_never_receives_login_token(monkeypatch, account_status, password):
    session = prepare(monkeypatch, account_status)
    with pytest.raises(HTTPException) as failure:
        await admin_auth.admin_login(AdminLoginRequest(username="fixture",password=password), session)
    assert failure.value.status_code == 401
    assert [event.action for event in session.events] == ["admin.login_failed"]


async def test_active_admin_with_correct_password_can_still_login(monkeypatch):
    session = prepare(monkeypatch, "active")
    result = await admin_auth.admin_login(AdminLoginRequest(username="fixture",password="valid-fixture"), session)
    assert result.access_token == "fixture-token"
    assert [event.action for event in session.events] == ["admin.login_succeeded"]
