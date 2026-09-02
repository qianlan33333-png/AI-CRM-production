from __future__ import annotations

import secrets
from time import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse, Response

from aicrm_next.integration_ports import (
    SidebarJSSDKConfigError,
    SidebarJSSDKInputError,
    build_sidebar_jssdk_config,
    normalize_jssdk_url,
)
from aicrm_next.integration_ports import (
    WeComAdminAuthClientError,
    build_wecom_admin_auth_client,
)
from aicrm_next.platform.shared.runtime import database_mode, production_environment
from aicrm_next.platform.shared.runtime_settings import managed_runtime_setting, runtime_setting
from aicrm_next.platform.shared.signed_context import (
    SIDEBAR_VIEWER_SESSION_COOKIE,
    build_sidebar_owner_context_token,
    sidebar_owner_context_ttl_seconds,
)
from aicrm_next.platform.shared.signed_session import (
    DEFAULT_SESSION_MAX_AGE_SECONDS,
    session_cookie_secure,
    sign_session_payload,
    sign_state_payload,
    verify_session_payload,
    verify_state_payload,
)

from .sidebar_authorization import build_sidebar_authorization_service
from .oneid_repository import PostgresOneIDService
from .resolution_effects import enqueue_sidebar_identity_verification

router = APIRouter()
DEFAULT_SIDEBAR_JSSDK_ALLOWED_HOSTS = {"youcangogogo.com", "www.youcangogogo.com"}
SIDEBAR_OAUTH_ENABLE_ENV = "AICRM_SIDEBAR_WECOM_OAUTH_ENABLE_REAL"
SIDEBAR_OAUTH_REDIRECT_URI_ENV = "AICRM_SIDEBAR_OAUTH_REDIRECT_URI"
ADMIN_AUTH_ENABLE_ENV = "AICRM_WECOM_ADMIN_AUTH_ENABLE_REAL"
# Import compatibility only; the cookie now carries a customer-bound OAuth session.
SIDEBAR_VIEWER_COOKIE = SIDEBAR_VIEWER_SESSION_COOKIE


@router.api_route("/api/sidebar/jssdk-config", methods=["GET", "HEAD", "OPTIONS"])
async def sidebar_jssdk_config(request: Request) -> Response:
    if request.method == "HEAD":
        return Response(status_code=204)
    if request.method == "OPTIONS":
        return JSONResponse(
            {
                "ok": True,
                "source_status": "next_jssdk_adapter",
                "route_owner": "ai_crm_next",
                "fallback_used": False,
                "adapter_mode": "real_blocked",
                "real_external_call_executed": False,
                "allowed_methods": ["GET", "HEAD", "OPTIONS"],
            },
            status_code=200,
        )

    params = request.query_params
    corp_context = {
        "corp_id": str(params.get("corp_id") or params.get("corpId") or params.get("corpid") or "").strip(),
        "agent_id": str(params.get("agent_id") or params.get("agentId") or params.get("agentid") or "").strip(),
    }
    corp_context = {key: value for key, value in corp_context.items() if value}
    debug = str(params.get("debug") or "").strip().lower() in {"1", "true", "yes", "on"}
    try:
        _validate_jssdk_url_host(request, str(params.get("url") or ""))
        payload = build_sidebar_jssdk_config(
            url=str(params.get("url") or ""),
            debug=debug,
            corp_context=corp_context,
        )
        payload = _with_sidebar_owner_context(request, payload)
    except SidebarJSSDKInputError as exc:
        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
                "source_status": "input_error",
                "adapter_mode": "real_blocked",
                "route_owner": "ai_crm_next",
                "fallback_used": False,
                "real_external_call_executed": False,
            },
            status_code=400,
        )
    except SidebarJSSDKConfigError as exc:
        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
                "source_status": "config_error",
                "adapter_mode": "real_enabled",
                "route_owner": "ai_crm_next",
                "fallback_used": False,
                "real_external_call_executed": bool(getattr(exc, "real_external_call_executed", False)),
            },
            status_code=502,
        )
    return JSONResponse(jsonable_encoder(payload), status_code=200)


@router.post("/api/sidebar/context-token")
async def sidebar_context_token(request: Request) -> Response:
    try:
        body = await request.json()
    except (TypeError, ValueError):
        body = {}
    external_userid = str((body or {}).get("external_userid") or "").strip()
    # The OAuth session is employee-scoped. Ignore a legacy customer field in
    # the cookie and bind the new token to the current request instead.
    if not external_userid:
        return JSONResponse(
            {
                "ok": False,
                "error_code": "external_userid_missing",
                "sidebar_owner_token": "",
                "sidebar_owner_token_status": "external_userid_missing",
                "route_owner": "ai_crm_next",
            },
            status_code=400,
        )
    viewer_session = _viewer_session_from_request(request)
    viewer_userid = str(viewer_session.get("wecom_userid") or "").strip()
    if not viewer_userid:
        return JSONResponse(
            _without_sidebar_owner_token(
                request,
                {"ok": True, "route_owner": "ai_crm_next"},
                status="viewer_session_required",
                external_userid=external_userid,
                source="sidebar_context_token_viewer_required",
            ),
            status_code=200,
        )
    corp_id = str(viewer_session.get("corp_id") or "").strip()
    configured_corp_id = str(managed_runtime_setting("WECOM_CORP_ID") or "").strip()
    if not corp_id or (configured_corp_id and corp_id != configured_corp_id):
        return JSONResponse(
            {
                "ok": False,
                "error_code": "sidebar_corp_scope_forbidden",
                "sidebar_owner_token": "",
                "sidebar_owner_token_status": "sidebar_corp_scope_forbidden",
                "route_owner": "ai_crm_next",
            },
            status_code=403,
        )
    access = _sidebar_customer_access(
        corp_id=corp_id,
        viewer_userid=viewer_userid,
        external_userid=external_userid,
    )
    if access["status"] == "forbidden":
        return JSONResponse(
            {
                "ok": False,
                "context_status": "forbidden",
                "error": "sidebar_customer_scope_forbidden",
                "sidebar_owner_token": "",
                "sidebar_owner_token_status": "forbidden",
                "route_owner": "ai_crm_next",
                "real_external_call_executed": False,
            },
            status_code=403,
        )
    if access["status"] == "provisioning":
        return JSONResponse(
            {
                "ok": True,
                "context_status": "provisioning",
                "sidebar_owner_token": "",
                "sidebar_owner_token_status": "provisioning",
                "sync_token": _provisioning_sync_token(
                    corp_id=corp_id,
                    viewer_userid=viewer_userid,
                    external_userid=external_userid,
                ),
                "retry_after": 2,
                "unionid_status": str(access.get("unionid_status") or "pending"),
                "route_owner": "ai_crm_next",
                "real_external_call_executed": False,
            },
            status_code=202,
        )
    ttl_seconds = sidebar_owner_context_ttl_seconds()
    token = build_sidebar_owner_context_token(
        viewer_userid=viewer_userid,
        external_userid=external_userid,
        session_id=str(viewer_session.get("session_id") or ""),
        corp_id=corp_id,
        ttl_seconds=ttl_seconds,
    )
    return JSONResponse(
        {
            "ok": True,
            "context_status": "ready",
            "unionid_status": str(access.get("unionid_status") or "pending"),
            "sidebar_owner_token": token,
            "sidebar_owner_token_status": "issued",
            "sidebar_owner_context": {
                "viewer_userid": viewer_userid,
                "owner_userid": viewer_userid,
                "bind_by_userid": viewer_userid,
                "corp_id": corp_id,
                "external_userid": external_userid,
                "expires_in": ttl_seconds,
                "source": "sidebar_context_token_oauth_session",
            },
            "route_owner": "ai_crm_next",
            "real_external_call_executed": False,
        },
        status_code=200,
    )


@router.api_route("/api/sidebar/oauth/start", methods=["GET", "OPTIONS"])
def sidebar_oauth_start(request: Request) -> Response:
    if request.method == "OPTIONS":
        return JSONResponse(
            {
                "ok": True,
                "route": "/api/sidebar/oauth/start",
                "route_owner": "ai_crm_next",
                "source_status": "next_sidebar_oauth",
                "adapter_mode": "real_blocked",
                "fallback_used": False,
                "real_external_call_executed": False,
                "allowed_methods": ["GET", "OPTIONS"],
            },
            status_code=200,
        )

    external_userid = _external_userid_from_request(request) or _external_userid_from_path(
        str(request.query_params.get("next") or "")
    )
    next_path = _safe_sidebar_next_path(request.query_params.get("next"), external_userid=external_userid)
    oauth = _sidebar_oauth_config(request)
    if not external_userid:
        return _sidebar_oauth_error_response("external_userid_missing", next_path, status_code=400)
    if not oauth["enabled"]:
        return _sidebar_oauth_error_response("sidebar_oauth_not_enabled", next_path, status_code=503)
    missing = _sidebar_oauth_missing(oauth)
    if missing:
        return _sidebar_oauth_error_response("sidebar_oauth_config_missing", next_path, status_code=503)

    state = sign_state_payload(
        {
            "next": next_path,
            "external_userid": external_userid,
            "nonce": secrets.token_urlsafe(16),
            "iat": int(time()),
        }
    )
    query = urlencode(
        {
            "appid": oauth["corp_id"],
            "redirect_uri": oauth["redirect_uri"],
            "response_type": "code",
            "scope": "snsapi_base",
            "state": state,
        }
    )
    return RedirectResponse(
        f"https://open.weixin.qq.com/connect/oauth2/authorize?{query}#wechat_redirect",
        status_code=302,
        headers=_sidebar_oauth_headers(real_external_call_executed=False),
    )


@router.api_route("/api/sidebar/oauth/callback", methods=["GET", "OPTIONS"])
def sidebar_oauth_callback(request: Request) -> Response:
    if request.method == "OPTIONS":
        return JSONResponse(
            {
                "ok": True,
                "route": "/api/sidebar/oauth/callback",
                "route_owner": "ai_crm_next",
                "source_status": "next_sidebar_oauth",
                "adapter_mode": "real_blocked",
                "fallback_used": False,
                "real_external_call_executed": False,
                "allowed_methods": ["GET", "OPTIONS"],
            },
            status_code=200,
        )

    state_payload = verify_state_payload(str(request.query_params.get("state") or ""))
    external_userid = str((state_payload or {}).get("external_userid") or "").strip()
    next_path = _safe_sidebar_next_path((state_payload or {}).get("next"), external_userid=external_userid)
    if not state_payload:
        return _sidebar_oauth_error_redirect(next_path, "invalid_state")
    if not str(request.query_params.get("code") or "").strip():
        return _sidebar_oauth_error_redirect(next_path, "missing_code")
    oauth = _sidebar_oauth_config(request)
    if not oauth["enabled"]:
        return _sidebar_oauth_error_redirect(next_path, "sidebar_oauth_not_enabled")
    if _sidebar_oauth_missing(oauth):
        return _sidebar_oauth_error_redirect(next_path, "sidebar_oauth_config_missing")

    client = build_wecom_admin_auth_client()
    try:
        token_payload = client.fetch_access_token(corp_id=oauth["corp_id"], corp_secret=oauth["corp_secret"])
        if _wecom_errcode(token_payload):
            return _sidebar_oauth_error_redirect(next_path, "wecom_access_token_failed")
        access_token = str(token_payload.get("access_token") or "").strip()
        if not access_token:
            return _sidebar_oauth_error_redirect(next_path, "wecom_access_token_missing")
        user_payload = client.fetch_user_info(access_token=access_token, code=str(request.query_params.get("code") or "").strip())
        if _wecom_errcode(user_payload):
            return _sidebar_oauth_error_redirect(next_path, "wecom_userinfo_failed")
    except WeComAdminAuthClientError as exc:
        return _sidebar_oauth_error_redirect(next_path, exc.error_code or "wecom_sidebar_oauth_failed")

    viewer_userid = str(user_payload.get("UserId") or user_payload.get("userid") or user_payload.get("user_id") or "").strip()
    if not viewer_userid:
        return _sidebar_oauth_error_redirect(next_path, "wecom_userid_missing")

    previous_session = _viewer_session_from_request(request)
    same_employee_session = (
        str(previous_session.get("wecom_userid") or "").strip() == viewer_userid
        and str(previous_session.get("corp_id") or "").strip() == oauth["corp_id"]
    )
    try:
        previous_oauth_count = int(previous_session.get("oauth_count") or 0) if same_employee_session else 0
    except (TypeError, ValueError):
        previous_oauth_count = 0
    oauth_count = previous_oauth_count + 1
    session_id = (
        str(previous_session.get("session_id") or "").strip()
        if same_employee_session
        else secrets.token_urlsafe(24)
    )
    build_sidebar_authorization_service().record_oauth_callback(repeated=oauth_count > 1)

    clean_next_path = _remove_query_keys(
        next_path,
        {"external_userid", "externalUserid", "externalUserId", "user_id", "userId", "sidebar_oauth_error"},
    )
    response = RedirectResponse(
        _append_query(clean_next_path, {"sidebar_oauth": "1"}),
        status_code=302,
        headers=_sidebar_oauth_headers(real_external_call_executed=True),
    )
    response.set_cookie(
        SIDEBAR_VIEWER_SESSION_COOKIE,
        sign_session_payload(
            {
                "auth_source": "wecom_sidebar_oauth",
                "wecom_userid": viewer_userid,
                "corp_id": oauth["corp_id"],
                "session_id": session_id,
                "oauth_count": oauth_count,
                "iat": int(time()),
            }
        ),
        max_age=DEFAULT_SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=session_cookie_secure(),
        path="/",
    )
    return response


def _with_sidebar_owner_context(request: Request, payload: dict) -> dict:
    result = dict(payload)
    viewer_session = _viewer_session_from_request(request)
    viewer_userid = str(viewer_session.get("wecom_userid") or "").strip()
    external_userid = _external_userid_from_request(request)
    source = "sidebar_jssdk_oauth_session"
    status = "issued"
    if not viewer_userid:
        return _without_sidebar_owner_token(
            request,
            result,
            status="viewer_session_required",
            external_userid=external_userid,
            source="sidebar_jssdk_viewer_required",
        )
    if not external_userid:
        return _without_sidebar_owner_token(
            request,
            result,
            status="context_token_required",
            external_userid=external_userid,
            source="sidebar_context_token_required",
        )
    corp_id = str(result.get("corp_id") or result.get("corpId") or viewer_session.get("corp_id") or "").strip()
    access = _sidebar_customer_access(
        corp_id=corp_id,
        viewer_userid=viewer_userid,
        external_userid=external_userid,
    )
    if access["status"] != "ready":
        result = _without_sidebar_owner_token(
            request,
            result,
            status=access["status"],
            external_userid=external_userid,
            source=f"sidebar_{access['status']}",
        )
        result["context_status"] = access["status"]
        if access["status"] == "provisioning":
            result["sync_token"] = _provisioning_sync_token(
                corp_id=corp_id,
                viewer_userid=viewer_userid,
                external_userid=external_userid,
            )
            result["retry_after"] = 2
            result["unionid_status"] = str(access.get("unionid_status") or "pending")
        return result
    ttl_seconds = sidebar_owner_context_ttl_seconds()
    result["sidebar_owner_token"] = build_sidebar_owner_context_token(
        viewer_userid=viewer_userid,
        external_userid=external_userid,
        session_id=str(viewer_session.get("session_id") or ""),
        corp_id=str(result.get("corp_id") or result.get("corpId") or ""),
        ttl_seconds=ttl_seconds,
    )
    result["sidebar_owner_token_status"] = status
    result["context_status"] = "ready"
    result["unionid_status"] = str(access.get("unionid_status") or "pending")
    result["sidebar_owner_context"] = {
        "viewer_userid": viewer_userid,
        "owner_userid": viewer_userid,
        "bind_by_userid": viewer_userid,
        "corp_id": str(result.get("corp_id") or result.get("corpId") or ""),
        "external_userid": _external_userid_from_request(request),
        "expires_in": ttl_seconds,
        "source": source,
    }
    return result


def _sidebar_customer_access(*, corp_id: str, viewer_userid: str, external_userid: str) -> dict[str, Any]:
    authorized = build_sidebar_authorization_service().authorize(
        corp_id=str(corp_id or "").strip(),
        user_id=str(viewer_userid or "").strip(),
        external_userid=str(external_userid or "").strip(),
    )
    oneid_read_setting = str(managed_runtime_setting("AICRM_ONEID_READ_ENABLED") or "").strip().lower()
    if oneid_read_setting in {"0", "false", "off", "no"}:
        return {"status": "ready" if authorized else "forbidden", "unionid_status": "pending"}
    if database_mode() != "postgres":
        return {"status": "ready" if authorized else "forbidden", "unionid_status": "pending"}
    oneid_service = PostgresOneIDService()
    state = oneid_service.customer_context_state(
        corp_id=corp_id,
        owner_userid=viewer_userid,
        external_userid=external_userid,
    )
    if state.get("identity_exists"):
        return {"status": "ready" if state.get("relation_active") else "forbidden", **state}
    if authorized:
        ensured = oneid_service.ensure_verified_wecom_identity(
            corp_id=corp_id,
            owner_userid=viewer_userid,
            external_userid=external_userid,
            source_type="sidebar_verified_follow_relation",
        )
        return {
            "status": "ready",
            "identity_exists": True,
            "relation_active": True,
            "identity_id": ensured.identity_id,
            "customer_id": ensured.customer_id,
            "unionid_status": "pending",
        }
    planned = enqueue_sidebar_identity_verification(
        corp_id=corp_id,
        owner_userid=viewer_userid,
        external_userid=external_userid,
    )
    return {"status": "provisioning", "verification": planned}


def _provisioning_sync_token(*, corp_id: str, viewer_userid: str, external_userid: str) -> str:
    return sign_state_payload(
        {
            "kind": "sidebar_customer_provisioning",
            "corp_id": str(corp_id or "").strip(),
            "viewer_userid": str(viewer_userid or "").strip(),
            "external_userid": str(external_userid or "").strip(),
            "nonce": secrets.token_urlsafe(12),
        }
    )


def _without_sidebar_owner_token(
    request: Request,
    payload: dict,
    *,
    status: str,
    external_userid: str = "",
    source: str = "missing",
) -> dict:
    result = dict(payload)
    result["sidebar_owner_token"] = ""
    result["sidebar_owner_token_status"] = status
    context = {"source": source}
    if external_userid:
        context["external_userid"] = external_userid
    if status != "context_token_required":
        oauth = _sidebar_oauth_metadata(request, external_userid)
        if oauth["status"]:
            context["sidebar_oauth_status"] = oauth["status"]
        if oauth["url"]:
            result["sidebar_oauth_url"] = oauth["url"]
    result["sidebar_owner_context"] = context
    return result


def _viewer_session_from_request(request: Request) -> dict[str, Any]:
    session = verify_session_payload(request.cookies.get(SIDEBAR_VIEWER_SESSION_COOKIE))
    if not session or str(session.get("auth_source") or "").strip() != "wecom_sidebar_oauth":
        return {}
    required = ("wecom_userid", "corp_id", "session_id")
    if any(not str(session.get(field) or "").strip() for field in required):
        return {}
    return dict(session)


def _external_userid_from_request(request: Request) -> str:
    params = request.query_params
    for value in (
        params.get("external_userid"),
        params.get("externalUserid"),
        params.get("external_userId"),
        params.get("externalUserId"),
        params.get("user_id"),
        params.get("userId"),
        request.headers.get("x-wecom-external-userid"),
        request.headers.get("x-aicrm-sidebar-external-userid"),
    ):
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return ""


def _sidebar_oauth_metadata(request: Request, external_userid: str) -> dict[str, str]:
    normalized_external = str(external_userid or "").strip()
    if not normalized_external:
        return {"status": "external_userid_missing", "url": ""}
    oauth = _sidebar_oauth_config(request)
    if not oauth["enabled"]:
        return {"status": "disabled", "url": ""}
    if _sidebar_oauth_missing(oauth):
        return {"status": "config_missing", "url": ""}
    next_path = _safe_sidebar_next_path(str(request.query_params.get("url") or ""), external_userid=normalized_external)
    return {
        "status": "ready",
        "url": _append_query(
            "/api/sidebar/oauth/start",
            {"external_userid": normalized_external, "next": next_path},
        ),
    }


def _sidebar_oauth_config(request: Request) -> dict[str, Any]:
    request_base = f"{request.url.scheme}://{request.url.netloc}"
    return {
        "enabled": _truthy(managed_runtime_setting(SIDEBAR_OAUTH_ENABLE_ENV))
        or _truthy(managed_runtime_setting(ADMIN_AUTH_ENABLE_ENV)),
        "corp_id": str(managed_runtime_setting("WECOM_CORP_ID") or "").strip(),
        "corp_secret": runtime_setting("WECOM_SECRET"),
        "redirect_uri": managed_runtime_setting(SIDEBAR_OAUTH_REDIRECT_URI_ENV).strip()
        or f"{request_base.rstrip('/')}/api/sidebar/oauth/callback",
    }


def _sidebar_oauth_missing(config: dict[str, Any]) -> list[str]:
    missing = []
    if not str(config.get("corp_id") or "").strip():
        missing.append("WECOM_CORP_ID")
    if not str(config.get("corp_secret") or "").strip():
        missing.append("WECOM_SECRET")
    if not str(config.get("redirect_uri") or "").strip():
        missing.append(SIDEBAR_OAUTH_REDIRECT_URI_ENV)
    return missing


def _sidebar_oauth_error_response(error_code: str, next_path: str, *, status_code: int) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": error_code,
            "error_code": error_code,
            "next": _append_query(next_path, {"sidebar_oauth_error": error_code}),
            "route_owner": "ai_crm_next",
            "source_status": "next_sidebar_oauth",
            "fallback_used": False,
            "real_external_call_executed": False,
        },
        status_code=status_code,
        headers=_sidebar_oauth_headers(real_external_call_executed=False),
    )


def _sidebar_oauth_error_redirect(next_path: str, error_code: str) -> RedirectResponse:
    return RedirectResponse(
        _append_query(next_path, {"sidebar_oauth_error": error_code}),
        status_code=302,
        headers=_sidebar_oauth_headers(real_external_call_executed=False),
    )


def _sidebar_oauth_headers(*, real_external_call_executed: bool) -> dict[str, str]:
    return {
        "X-AICRM-Route-Owner": "ai_crm_next",
        "X-AICRM-Fallback-Used": "false",
        "X-AICRM-Real-External-Call-Executed": "true" if real_external_call_executed else "false",
    }


def _safe_sidebar_next_path(value: Any, *, external_userid: str = "") -> str:
    raw = str(value or "").strip()
    if not raw:
        return _append_query("/sidebar/bind-mobile", {"external_userid": external_userid} if external_userid else {})
    if "://" in raw:
        parsed = urlparse(raw)
        raw = urlunparse(("", "", parsed.path or "", "", parsed.query or "", ""))
    if raw.startswith("//") or raw.startswith("\\") or not raw.startswith("/"):
        raw = "/sidebar/bind-mobile"
    parsed = urlparse(raw)
    if parsed.path != "/sidebar/bind-mobile":
        raw = "/sidebar/bind-mobile"
        parsed = urlparse(raw)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key not in {"sidebar_oauth_error"}]
    cleaned = urlunparse(("", "", parsed.path, "", urlencode(query), ""))
    return _append_query(cleaned or "/sidebar/bind-mobile", {"external_userid": external_userid} if external_userid and "external_userid=" not in cleaned else {})


def _external_userid_from_path(value: str) -> str:
    parsed = urlparse(str(value or ""))
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key in ("external_userid", "externalUserid", "externalUserId", "user_id", "userId"):
        normalized = str(params.get(key) or "").strip()
        if normalized:
            return normalized
    return ""


def _append_query(path: str, params: dict[str, str]) -> str:
    parsed = urlparse(str(path or "/sidebar/bind-mobile"))
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key not in params]
    for key, value in (params or {}).items():
        normalized = str(value or "").strip()
        if normalized:
            query.append((key, normalized))
    return urlunparse(("", "", parsed.path or "/sidebar/bind-mobile", "", urlencode(query), ""))


def _remove_query_keys(path: str, keys: set[str]) -> str:
    parsed = urlparse(str(path or "/sidebar/bind-mobile"))
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key not in keys]
    return urlunparse(("", "", parsed.path or "/sidebar/bind-mobile", "", urlencode(query), ""))


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _wecom_errcode(payload: dict[str, Any]) -> bool:
    errcode = payload.get("errcode")
    return errcode not in (None, 0, "0")


def _validate_jssdk_url_host(request: Request, raw_url: str) -> None:
    if not production_environment():
        return
    normalized_url = normalize_jssdk_url(raw_url)
    requested_host = str(urlparse(normalized_url).hostname or "").strip().lower()
    if not requested_host:
        raise SidebarJSSDKInputError("url host is required")
    allowed_hosts = _allowed_jssdk_hosts(request)
    if requested_host not in allowed_hosts:
        raise SidebarJSSDKInputError("url host is not allowed for sidebar jssdk signing")


def _allowed_jssdk_hosts(request: Request) -> set[str]:
    hosts = {
        *DEFAULT_SIDEBAR_JSSDK_ALLOWED_HOSTS,
        str(request.url.hostname or "").strip().lower(),
        str(request.headers.get("host") or "").split(":", 1)[0].strip().lower(),
    }
    configured = managed_runtime_setting("AICRM_SIDEBAR_JSSDK_ALLOWED_HOSTS")
    hosts.update(item.strip().lower() for item in configured.split(",") if item.strip())
    return {host for host in hosts if host}
