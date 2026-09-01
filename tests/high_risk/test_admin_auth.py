from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from time import time
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from aicrm_next.channels.integration_gateway.wecom_jssdk_adapter import (
    build_sidebar_jssdk_config,
    reset_sidebar_jssdk_attempts,
)
from aicrm_next.crm.identity_contact.sidebar_jssdk import SIDEBAR_VIEWER_COOKIE
from aicrm_next.main import create_app
from aicrm_next.platform.admin_auth.action_token import issue_action_token, validate_action_token
from aicrm_next.platform.admin_auth.capabilities import capabilities_for_roles
from aicrm_next.platform.platform_foundation.auth_platform.context import AuthContext, PrincipalType
from aicrm_next.platform.shared.signed_context import validate_sidebar_owner_context
from aicrm_next.platform.shared.signed_session import sign_session_payload, sign_state_payload, verify_session_payload


pytestmark = pytest.mark.high_risk


def _admin_context(*, request_id: str = "session-current") -> AuthContext:
    return AuthContext(
        principal_type=PrincipalType.HUMAN,
        principal_id="admin-current",
        capabilities=tuple(capabilities_for_roles(["super_admin"])),
        scopes=("admin",),
        admin_user_id="admin-current",
        request_id=request_id,
    )


def test_unsafe_action_token_is_bound_to_session_capability_route_and_method() -> None:
    context = _admin_context()
    token = issue_action_token(
        context,
        capability="manage_config",
        method="POST",
        action="update_runtime_config",
        target="/api/admin/config/runtime",
        now=1_800_000_000,
    )
    valid = validate_action_token(
        token,
        context,
        capability="manage_config",
        method="POST",
        action="update_runtime_config",
        target="/api/admin/config/runtime",
        now=1_800_000_001,
    )
    wrong_target = validate_action_token(
        token,
        context,
        capability="manage_config",
        method="POST",
        action="update_runtime_config",
        target="/api/admin/config/secrets",
        now=1_800_000_001,
    )
    wrong_session = validate_action_token(
        token,
        _admin_context(request_id="another-session"),
        capability="manage_config",
        method="POST",
        action="update_runtime_config",
        target="/api/admin/config/runtime",
        now=1_800_000_001,
    )
    assert valid.ok is True
    assert wrong_target.error == "binding_mismatch:tgt"
    assert wrong_session.error == "binding_mismatch:sid"


def test_action_token_rejects_expiry_tampering_and_safe_methods() -> None:
    context = _admin_context()
    token = issue_action_token(
        context,
        capability="manage_customer",
        method="DELETE",
        action="delete_customer_tag",
        target="/api/admin/customer-tags/1",
        now=1_800_000_000,
        ttl_seconds=30,
    )
    expired = validate_action_token(
        token,
        context,
        capability="manage_customer",
        method="DELETE",
        action="delete_customer_tag",
        target="/api/admin/customer-tags/1",
        now=1_800_000_031,
    )
    tampered = validate_action_token(
        token[:-1] + ("A" if token[-1] != "A" else "B"),
        context,
        capability="manage_customer",
        method="DELETE",
        action="delete_customer_tag",
        target="/api/admin/customer-tags/1",
        now=1_800_000_001,
    )
    assert expired.error == "expired"
    assert tampered.error == "invalid"
    with pytest.raises(ValueError, match="safe method"):
        issue_action_token(
            context,
            capability="admin_read",
            method="GET",
            action="read_customer",
            target="/api/admin/customers/1",
        )


def test_sidebar_context_token_requires_current_active_follow_relation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WECOM_CORP_ID", "corp-a")

    class RelationService:
        def authorize(self, **kwargs) -> bool:
            return kwargs == {
                "corp_id": "corp-a",
                "user_id": "staff-a",
                "external_userid": "external-a",
            }

    monkeypatch.setattr(
        "aicrm_next.crm.identity_contact.sidebar_jssdk.build_sidebar_authorization_service",
        lambda: RelationService(),
        raising=False,
    )
    client = TestClient(create_app(), raise_server_exceptions=False)
    client.cookies.set(
        SIDEBAR_VIEWER_COOKIE,
        sign_session_payload(
            {
                "auth_source": "wecom_sidebar_oauth",
                "wecom_userid": "staff-a",
                # Cookies issued before the employee-scoped session migration carried
                # the customer that initiated OAuth. They remain valid for eight hours.
                "external_userid": "external-a",
                "corp_id": "corp-a",
                "session_id": "session-a",
                "iat": int(time()),
            }
        ),
    )
    issued = client.post("/api/sidebar/context-token", json={"external_userid": "external-a"})
    switched = client.post("/api/sidebar/context-token", json={"external_userid": "external-b"})
    assert issued.status_code == 200
    assert issued.json()["context_status"] == "ready"
    assert issued.json()["sidebar_owner_token_status"] == "issued"
    assert issued.json()["sidebar_owner_context"]["source"] == "sidebar_context_token_oauth_session"
    assert switched.status_code == 403
    assert switched.json()["context_status"] == "forbidden"
    assert switched.json()["sidebar_owner_token"] == ""

    viewer_session = client.cookies.get(SIDEBAR_VIEWER_COOKIE)
    assert validate_sidebar_owner_context(
        token=issued.json()["sidebar_owner_token"],
        viewer_session_cookie=viewer_session,
        external_userid="external-a",
        expected_corp_id="corp-a",
    )["ok"]
    replay = validate_sidebar_owner_context(
        token=issued.json()["sidebar_owner_token"],
        viewer_session_cookie=viewer_session,
        external_userid="external-b",
        expected_corp_id="corp-a",
    )
    assert replay["ok"] is False
    assert replay["status"] == "sidebar_customer_scope_forbidden"


def test_sidebar_context_token_returns_opaque_provisioning_state_for_unseen_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WECOM_CORP_ID", "corp-a")

    class RelationService:
        def authorize(self, **_kwargs) -> bool:
            return False

    class OneIDService:
        def customer_context_state(self, **_kwargs):
            return {"identity_exists": False, "relation_active": False}

    planned: list[dict] = []
    monkeypatch.setattr(
        "aicrm_next.crm.identity_contact.sidebar_jssdk.build_sidebar_authorization_service",
        lambda: RelationService(),
    )
    monkeypatch.setattr("aicrm_next.crm.identity_contact.sidebar_jssdk.database_mode", lambda: "postgres")
    monkeypatch.setattr("aicrm_next.crm.identity_contact.sidebar_jssdk.PostgresOneIDService", OneIDService)
    monkeypatch.setattr(
        "aicrm_next.crm.identity_contact.sidebar_jssdk.enqueue_sidebar_identity_verification",
        lambda **kwargs: planned.append(kwargs) or {"status": "queued", "real_external_call_executed": False},
    )
    client = TestClient(create_app(), raise_server_exceptions=False)
    client.cookies.set(
        SIDEBAR_VIEWER_COOKIE,
        sign_session_payload(
            {
                "auth_source": "wecom_sidebar_oauth",
                "wecom_userid": "staff-a",
                "corp_id": "corp-a",
                "session_id": "session-a",
                "iat": int(time()),
            }
        ),
    )

    response = client.post("/api/sidebar/context-token", json={"external_userid": "external-new"})

    assert response.status_code == 202
    assert response.json()["context_status"] == "provisioning"
    assert response.json()["sidebar_owner_token"] == ""
    assert response.json()["sync_token"]
    assert "external-new" not in response.json()["sync_token"]
    assert response.json()["retry_after"] == 2
    assert planned == [
        {
            "corp_id": "corp-a",
            "owner_userid": "staff-a",
            "external_userid": "external-new",
        }
    ]


def test_sidebar_oauth_cookie_is_employee_scoped_and_callback_url_is_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AICRM_SIDEBAR_WECOM_OAUTH_ENABLE_REAL", "1")
    monkeypatch.setenv("WECOM_CORP_ID", "corp-a")
    monkeypatch.setenv("WECOM_SECRET", "secret")
    monkeypatch.setenv("AICRM_SIDEBAR_OAUTH_REDIRECT_URI", "https://www.youcangogogo.com/api/sidebar/oauth/callback")

    class AuthClient:
        def fetch_access_token(self, **_kwargs) -> dict:
            return {"errcode": 0, "access_token": "token"}

        def fetch_user_info(self, **_kwargs) -> dict:
            return {"errcode": 0, "UserId": "staff-a"}

    class RelationService:
        def record_oauth_callback(self, *, repeated: bool) -> None:
            assert repeated is False

    monkeypatch.setattr(
        "aicrm_next.crm.identity_contact.sidebar_jssdk.build_wecom_admin_auth_client",
        lambda: AuthClient(),
    )
    monkeypatch.setattr(
        "aicrm_next.crm.identity_contact.sidebar_jssdk.build_sidebar_authorization_service",
        lambda: RelationService(),
    )
    client = TestClient(create_app(), base_url="https://www.youcangogogo.com", raise_server_exceptions=False)
    state = sign_state_payload(
        {
            "next": "/sidebar/bind-mobile?external_userid=external-a",
            "external_userid": "external-a",
            "nonce": "test",
        }
    )
    response = client.get(
        "/api/sidebar/oauth/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    session = verify_session_payload(client.cookies.get(SIDEBAR_VIEWER_COOKIE))
    assert response.status_code == 302
    assert response.headers["location"] == "/sidebar/bind-mobile?sidebar_oauth=1"
    assert session and session["wecom_userid"] == "staff-a"
    assert "external_userid" not in session


def test_jssdk_signing_material_refresh_is_singleflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WECOM_CORP_ID", "corp-a")
    monkeypatch.setenv("WECOM_AGENT_ID", "1000002")
    monkeypatch.setenv("WECOM_SECRET", "secret")
    reset_sidebar_jssdk_attempts()
    calls: list[str] = []
    lock = Lock()
    release = Event()

    def fake_get_json(url: str, *, timeout: int) -> dict:
        with lock:
            calls.append(urlparse(url).path)
            first = len(calls) == 1
        if first:
            release.wait(timeout=2)
        path = urlparse(url).path
        if path == "/cgi-bin/gettoken":
            return {"errcode": 0, "access_token": "token", "expires_in": 7200}
        return {"errcode": 0, "ticket": "ticket", "expires_in": 7200}

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(
                build_sidebar_jssdk_config,
                url=f"https://www.youcangogogo.com/sidebar/bind-mobile?request={index}",
                adapter_mode="real_enabled",
                http_get_json=fake_get_json,
            )
            for index in range(8)
        ]
        release.set()
        assert all(future.result(timeout=15)["ok"] for future in futures)
    assert calls.count("/cgi-bin/gettoken") == 1
    assert calls.count("/cgi-bin/get_jsapi_ticket") == 1
    assert calls.count("/cgi-bin/ticket/get") == 1
