from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import Response

from .runtime import production_environment, runtime_setting


WECHAT_PAYMENT_IDENTITY_COOKIE = "wechat_pay_h5_identity"
WECHAT_PAYMENT_IDENTITY_TTL_SECONDS = 86400 * 30
WECHAT_PAYMENT_OAUTH_STATE_COOKIE = "wechat_pay_h5_oauth_state"
WECHAT_PAYMENT_OAUTH_STATE_COOKIE_PATH = "/api/h5/wechat-pay/oauth"
_CLOCK_SKEW_SECONDS = 300


def _text(value: Any) -> str:
    return str(value or "").strip()


def _secret() -> str:
    configured = _text(runtime_setting("AICRM_NEXT_ACTION_TOKEN_SECRET")) or _text(runtime_setting("SECRET_KEY"))
    if configured:
        return configured
    if production_environment():
        return ""
    return "aicrm-next-h5-wechat-pay-dev-secret"


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def payment_session_signing_available() -> bool:
    return bool(_secret())


def sign_payment_session_payload(payload: dict[str, Any]) -> str:
    effective_payload = dict(payload)
    if _text(effective_payload.get("openid")):
        issued_at = int(effective_payload.get("iat") or time.time())
        effective_payload["iat"] = issued_at
        effective_payload.setdefault("exp", issued_at + WECHAT_PAYMENT_IDENTITY_TTL_SECONDS)
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(
                effective_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        .decode("ascii")
        .rstrip("=")
    )
    secret = _secret()
    if not secret:
        raise RuntimeError("h5_payment_session_secret_required")
    signature = hmac.new(
        secret.encode("utf-8"),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{encoded}.{signature}"


def load_signed_payment_session_payload(value: str) -> dict[str, Any]:
    try:
        encoded, supplied_signature = _text(value).split(".", 1)
    except ValueError:
        return {}
    secret = _secret()
    if not secret:
        return {}
    expected_signature = hmac.new(
        secret.encode("utf-8"),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_signature, supplied_signature):
        return {}
    try:
        payload = json.loads(_decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_signed_identity(value: str) -> dict[str, Any]:
    payload = load_signed_payment_session_payload(value)
    if not payload:
        return {}
    try:
        issued_at = int(payload.get("iat"))
        expires_at = int(payload.get("exp"))
    except (TypeError, ValueError):
        return {}
    now = int(time.time())
    if issued_at <= 0 or issued_at > now + _CLOCK_SKEW_SECONDS or expires_at <= now:
        return {}
    if expires_at <= issued_at or expires_at - issued_at > WECHAT_PAYMENT_IDENTITY_TTL_SECONDS + _CLOCK_SKEW_SECONDS:
        return {}
    return payload


def is_wechat_browser(request: Request) -> bool:
    headers = getattr(request, "headers", None) or {}
    return "micromessenger" in (headers.get("User-Agent") or "").lower()


def safe_local_return_url(value: Any) -> str:
    normalized = _text(value)
    if not normalized or not normalized.startswith("/") or normalized.startswith("//") or "\\" in normalized:
        return "/"
    return normalized


def payment_oauth_start_url(return_url: str) -> str:
    return f"/api/h5/wechat-pay/oauth/start?{urlencode({'return_url': safe_local_return_url(return_url)})}"


def wechat_oauth_authorize_url(*, app_id: str, redirect_uri: str, scope: str, state: str) -> str:
    query = urlencode(
        {
            "appid": app_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope or "snsapi_base",
            "state": state,
        }
    )
    return f"https://open.weixin.qq.com/connect/oauth2/authorize?{query}#wechat_redirect"


def clear_payment_oauth_state_cookie(response: Response, *, secure: bool) -> Response:
    response.delete_cookie(
        WECHAT_PAYMENT_OAUTH_STATE_COOKIE,
        path=WECHAT_PAYMENT_OAUTH_STATE_COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    return response


def payment_identity_from_request(request: Request) -> dict[str, str]:
    payload = _load_signed_identity(request.cookies.get(WECHAT_PAYMENT_IDENTITY_COOKIE) or "")
    openid = _text(payload.get("openid"))
    if not openid:
        return {}
    return {
        "openid": openid,
        "app_id": _text(payload.get("app_id")),
        "customer_id": _text(payload.get("customer_id")),
        "payer_identity_id": _text(payload.get("payer_identity_id")),
        "unionid": _text(payload.get("unionid")),
        "respondent_key": _text(payload.get("respondent_key")),
        "external_userid": _text(payload.get("external_userid")),
        "payer_name": _text(payload.get("payer_name")),
    }


__all__ = [
    "WECHAT_PAYMENT_IDENTITY_COOKIE",
    "WECHAT_PAYMENT_IDENTITY_TTL_SECONDS",
    "WECHAT_PAYMENT_OAUTH_STATE_COOKIE",
    "WECHAT_PAYMENT_OAUTH_STATE_COOKIE_PATH",
    "clear_payment_oauth_state_cookie",
    "is_wechat_browser",
    "load_signed_payment_session_payload",
    "payment_identity_from_request",
    "payment_oauth_start_url",
    "payment_session_signing_available",
    "safe_local_return_url",
    "sign_payment_session_payload",
    "wechat_oauth_authorize_url",
]
