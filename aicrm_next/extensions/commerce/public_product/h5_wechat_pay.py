from __future__ import annotations

from datetime import datetime, timezone
import hmac
import json
import logging
import re
import secrets
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from aicrm_next.extensions.commerce.commerce.domain import completion_redirect_projection, safe_completion_redirect_url
from aicrm_next.extensions.commerce.commerce.wechat_pay_order_write_port import (
    WeChatPayOrderCreate,
    build_wechat_pay_order_write_port,
)
from aicrm_next.platform.navigation_target import (
    completion_action_for_target,
    completion_action_with_lead_qr,
    completion_target_projection,
)
from aicrm_next.extensions.commerce.commerce.order_expiration import close_expired_wechat_pay_orders, pending_order_expires_at_text
from aicrm_next.platform.shared.product_code_aliases import product_code_filter_values
from aicrm_next.crm.identity_contact.application import ResolvePersonIdentityQuery
from aicrm_next.crm.identity_contact.payment_projection import project_payment_order_mobile
from aicrm_next.crm.identity_contact.oneid_repository import PostgresOneIDService
from aicrm_next.crm.identity_contact.wechat_unionid_guard import evaluate_wechat_unionid_access, resolve_oauth_unionid
from aicrm_next.platform.shared.postgres_connection import PostgresConnection
from aicrm_next.integration_ports import WeChatPayClient, WeChatPayClientConfig, WeChatPayClientError
from aicrm_next.integration_ports import WeChatOAuthClientError, build_wechat_oauth_client
from aicrm_next.platform.platform_foundation.internal_events.outbox import enqueue_transactional_internal_event_outbox
from aicrm_next.platform.platform_foundation.internal_events.payment import PAYMENT_SUCCEEDED_EVENT_TYPE, build_payment_succeeded_event_request
from aicrm_next.platform.shared.errors import ContractError
from aicrm_next.platform.shared.runtime import (
    production_data_ready,
    production_environment,
    runtime_setting,
    secure_cookie_environment,
)
from aicrm_next.platform.shared.runtime_settings import managed_runtime_setting
from aicrm_next.platform.shared.safe_logging import safe_log_exception, safe_log_fields
from aicrm_next.platform.shared.wechat_identity_page import wechat_full_service_required_response, wechat_identity_failure_response
from aicrm_next.platform.shared.wechat_h5_session import (
    WECHAT_PAYMENT_IDENTITY_COOKIE,
    WECHAT_PAYMENT_IDENTITY_TTL_SECONDS,
    WECHAT_PAYMENT_OAUTH_STATE_COOKIE,
    WECHAT_PAYMENT_OAUTH_STATE_COOKIE_PATH,
    clear_payment_oauth_state_cookie,
    is_wechat_browser,
    load_signed_payment_session_payload as _load_signed_blob,
    payment_identity_from_request,
    payment_oauth_start_url as shared_payment_oauth_start_url,
    payment_session_signing_available,
    safe_local_return_url,
    sign_payment_session_payload as _signed_blob,
    wechat_oauth_authorize_url,
)

from .repo import connect_h5_wechat_pay_db as _connect
from aicrm_next.platform.shared.signed_context import SIDEBAR_PRODUCT_CONTEXT_COOKIE, load_sidebar_product_context_token
from .sidebar_order_context import resolve_sidebar_order_context
from .service import format_price, get_public_product, product_not_found_payload, route_headers


COOKIE_NAME = WECHAT_PAYMENT_IDENTITY_COOKIE
OAUTH_STATE_COOKIE_NAME = WECHAT_PAYMENT_OAUTH_STATE_COOKIE
OAUTH_STATE_COOKIE_PATH = WECHAT_PAYMENT_OAUTH_STATE_COOKIE_PATH
STATE_TTL_SECONDS = 600
_OAUTH_STATE_CLOCK_SKEW_SECONDS = 300
_OAUTH_STATE_PATTERN = re.compile(r"^[a-f0-9]{48}$")
LOGGER = logging.getLogger(__name__)
_OAUTH_CLIENT_FACTORY = build_wechat_oauth_client
RUNTIME_SETTING_KEYS = frozenset(
    {
        "WECHAT_MP_APP_ID",
        "WECHAT_MP_APP_SECRET",
        "WECHAT_PAY_API_BASE",
        "WECHAT_PAY_API_V3_KEY",
        "WECHAT_PAY_APP_ID",
        "WECHAT_PAY_APP_SECRET",
        "WECHAT_PAY_CERT_SERIAL_NO",
        "WECHAT_PAY_ENABLED",
        "WECHAT_PAY_MCH_ID",
        "WECHAT_PAY_NOTIFY_URL",
        "WECHAT_PAY_OAUTH_SCOPE",
        "WECHAT_PAY_PLATFORM_CERT_SERIAL_NO",
        "WECHAT_PAY_PLATFORM_PUBLIC_KEY_PATH",
        "WECHAT_PAY_PRIVATE_KEY_PATH",
        "WECHAT_PAY_TIMEOUT_SECONDS",
    }
)


def _normalized_text(value: Any) -> str:
    return str(value or "").strip()


def _env(name: str, default: str = "") -> str:
    return _normalized_text(managed_runtime_setting(name, default))


def _sensitive_setting(name: str, default: str = "") -> str:
    return _normalized_text(runtime_setting(name, default))


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env(name).lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except (TypeError, ValueError):
        return int(default)


def set_h5_wechat_pay_oauth_client_factory(factory):
    global _OAUTH_CLIENT_FACTORY
    _OAUTH_CLIENT_FACTORY = factory


def reset_h5_wechat_pay_oauth_client_factory():
    global _OAUTH_CLIENT_FACTORY
    _OAUTH_CLIENT_FACTORY = build_wechat_oauth_client


def _oauth_client():
    return _OAUTH_CLIENT_FACTORY()


def sidebar_product_context_status(context_token: str) -> str:
    result = load_sidebar_product_context_token(_normalized_text(context_token))
    return _normalized_text(result.get("status")) or "missing"


def _external_base_url(request: Request) -> str:
    configured = (
        _normalized_text(runtime_setting("AICRM_PUBLIC_BASE_URL"))
        or _normalized_text(runtime_setting("PUBLIC_BASE_URL"))
        or _normalized_text(runtime_setting("APP_BASE_URL"))
    )
    candidate = configured or str(request.base_url).rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("public_base_url_invalid")
    if production_environment() and not configured:
        raise RuntimeError("public_base_url_required")
    if production_environment() and parsed.scheme != "https":
        raise RuntimeError("public_base_url_https_required")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def _oauth_configured() -> bool:
    return bool(_payment_oauth_app_id() and _payment_oauth_app_secret() and payment_session_signing_available())


def _payment_oauth_app_id() -> str:
    return _env("WECHAT_PAY_APP_ID") or _env("WECHAT_MP_APP_ID")


def _payment_oauth_app_secret() -> str:
    pay_app_id = _env("WECHAT_PAY_APP_ID")
    mp_app_id = _env("WECHAT_MP_APP_ID")
    if pay_app_id and pay_app_id != mp_app_id:
        return _sensitive_setting("WECHAT_PAY_APP_SECRET")
    return _sensitive_setting("WECHAT_PAY_APP_SECRET") or _sensitive_setting("WECHAT_MP_APP_SECRET")


def _wechat_oauth_scope() -> str:
    return _env("WECHAT_PAY_OAUTH_SCOPE") or "snsapi_userinfo"


def _is_wechat_browser(request: Request) -> bool:
    return is_wechat_browser(request)


def _identity_from_request(request: Request) -> dict[str, str]:
    # Questionnaire identity cookies can contain caller-provided identity hints
    # and are therefore not payer credentials.  Payment, coupon and order
    # ownership checks accept only the dedicated expiring OAuth session.
    return payment_identity_from_request(request)


def h5_payment_identity_from_request(request: Request) -> dict[str, str]:
    return _identity_from_request(request)


payment_oauth_start_url = shared_payment_oauth_start_url


def payment_oauth_start(request: Request) -> RedirectResponse | JSONResponse:
    return_url = safe_local_return_url(request.query_params.get("return_url") or "/")
    if _identity_from_request(request).get("unionid"):
        return RedirectResponse(return_url, status_code=302, headers=route_headers())
    if not _oauth_configured():
        return JSONResponse({"ok": False, "error": "wechat_pay_oauth_not_configured"}, status_code=501, headers=route_headers())
    try:
        public_base_url = _external_base_url(request)
    except RuntimeError:
        return JSONResponse(
            {"ok": False, "error": "public_base_url_not_configured"},
            status_code=503,
            headers=route_headers(),
        )
    now = int(datetime.now(timezone.utc).timestamp())
    nonce = secrets.token_hex(24)
    state_cookie = _signed_blob(
        {
            "return_url": return_url,
            "nonce": nonce,
            "iat": now,
            "exp": now + STATE_TTL_SECONDS,
        }
    )
    authorize_url = wechat_oauth_authorize_url(
        app_id=_payment_oauth_app_id(),
        redirect_uri=f"{public_base_url}/api/h5/wechat-pay/oauth/callback",
        scope=_wechat_oauth_scope(),
        state=nonce,
    )
    response = RedirectResponse(authorize_url, status_code=302, headers=route_headers())
    response.set_cookie(
        OAUTH_STATE_COOKIE_NAME,
        state_cookie,
        max_age=STATE_TTL_SECONDS,
        httponly=True,
        secure=secure_cookie_environment() or public_base_url.startswith("https://"),
        samesite="lax",
        path=OAUTH_STATE_COOKIE_PATH,
    )
    return response


def payment_oauth_callback(request: Request) -> Response:
    try:
        public_base_url = _external_base_url(request)
    except RuntimeError:
        return JSONResponse(
            {"ok": False, "error": "public_base_url_not_configured"},
            status_code=503,
            headers=route_headers(),
        )
    state_nonce = _normalized_text(request.query_params.get("state"))
    if not _OAUTH_STATE_PATTERN.fullmatch(state_nonce):
        return JSONResponse({"ok": False, "error": "state_invalid"}, status_code=400, headers=route_headers())
    state_cookie = _normalized_text(request.cookies.get(OAUTH_STATE_COOKIE_NAME))
    if not state_cookie:
        return wechat_full_service_required_response(headers=route_headers())
    state_payload = _load_signed_blob(state_cookie)
    if not state_payload:
        return JSONResponse({"ok": False, "error": "oauth_state_cookie_invalid"}, status_code=400, headers=route_headers())
    cookie_nonce = _normalized_text(state_payload.get("nonce"))
    if not _OAUTH_STATE_PATTERN.fullmatch(cookie_nonce):
        return JSONResponse({"ok": False, "error": "oauth_state_cookie_invalid"}, status_code=400, headers=route_headers())
    if not hmac.compare_digest(state_nonce, cookie_nonce):
        return JSONResponse({"ok": False, "error": "oauth_state_cookie_mismatch"}, status_code=400, headers=route_headers())
    secure_cookie = secure_cookie_environment() or public_base_url.startswith("https://")

    def attributed_response(response: Response) -> Response:
        return clear_payment_oauth_state_cookie(response, secure=secure_cookie)

    return_url = safe_local_return_url(state_payload.get("return_url"))

    def identity_failure(*, message: str, status_code: int = 409) -> Response:
        return attributed_response(
            wechat_identity_failure_response(
                message=message,
                retry_url=payment_oauth_start_url(return_url),
                return_url=return_url,
                status_code=status_code,
                headers=route_headers(),
            )
        )

    try:
        issued_at = int(state_payload.get("iat"))
        expires_at = int(state_payload.get("exp"))
    except (TypeError, ValueError):
        return attributed_response(JSONResponse({"ok": False, "error": "state_invalid"}, status_code=400, headers=route_headers()))
    now = int(datetime.now(timezone.utc).timestamp())
    if issued_at <= 0 or issued_at > now + _OAUTH_STATE_CLOCK_SKEW_SECONDS or expires_at <= issued_at or expires_at - issued_at > STATE_TTL_SECONDS:
        return attributed_response(JSONResponse({"ok": False, "error": "state_invalid"}, status_code=400, headers=route_headers()))
    if expires_at <= now:
        return attributed_response(JSONResponse({"ok": False, "error": "state_expired"}, status_code=400, headers=route_headers()))
    code = _normalized_text(request.query_params.get("code"))
    if not code:
        return attributed_response(JSONResponse({"ok": False, "error": "code_required"}, status_code=400, headers=route_headers()))
    try:
        client = _oauth_client()
        oauth_payload = client.exchange_code(
            app_id=_payment_oauth_app_id(),
            app_secret=_payment_oauth_app_secret(),
            code=code,
        )
    except (WeChatOAuthClientError, Exception):
        return attributed_response(JSONResponse({"ok": False, "error": "wechat_oauth_failed"}, status_code=502, headers=route_headers()))
    if oauth_payload.get("errcode") not in (None, 0):
        return attributed_response(
            JSONResponse(
                {"ok": False, "error": oauth_payload.get("errmsg") or "wechat_oauth_failed"},
                status_code=502,
                headers=route_headers(),
            )
        )
    openid = _normalized_text(oauth_payload.get("openid"))
    if not openid:
        return attributed_response(
            JSONResponse(
                {"ok": False, "error": "wechat_oauth_openid_missing"},
                status_code=502,
                headers=route_headers(),
            )
        )
    unionid = _normalized_text(oauth_payload.get("unionid"))
    payer_name = ""
    access_token = _normalized_text(oauth_payload.get("access_token"))
    if _wechat_oauth_scope() == "snsapi_userinfo" and access_token and openid:
        try:
            userinfo = client.fetch_userinfo(access_token=access_token, openid=openid)
            if userinfo.get("errcode") in (None, 0):
                unionid = unionid or _normalized_text(userinfo.get("unionid"))
                payer_name = _normalized_text(userinfo.get("nickname"))
        except (WeChatOAuthClientError, Exception):
            payer_name = ""
    unionid = resolve_oauth_unionid(
        {"openid": openid, "unionid": unionid},
        identity_query=ResolvePersonIdentityQuery(),
    )
    if not unionid:
        return identity_failure(message="微信未返回 UnionID，暂时无法继续，请确认授权后重试。")
    customer_id = 0
    payer_identity_id = 0
    if production_data_ready():
        try:
            with _connect() as conn:
                resolution = PostgresOneIDService().ensure_verified_wechat_identity_with_db(
                    PostgresConnection(conn),
                    app_id=_payment_oauth_app_id(),
                    openid=openid,
                    unionid=unionid,
                    source_type="wechat_oauth",
                    source_event_id=code,
                )
                if resolution.get("status") == "conflict":
                    conn.commit()
                    return identity_failure(message="当前微信身份存在冲突，请联系工作人员处理。")
                customer_id = int(resolution.get("customer_id") or 0)
                payer_identity_id = int(resolution.get("openid_identity_id") or resolution.get("identity_id") or 0)
                conn.commit()
        except Exception as exc:
            safe_log_exception(LOGGER, "wechat_pay_oauth_identity_projection_failed", exc)
            return attributed_response(
                JSONResponse(
                    {"ok": False, "error": "wechat_oauth_identity_projection_failed"},
                    status_code=503,
                    headers=route_headers(),
                )
            )
    response = RedirectResponse(return_url, status_code=302, headers=route_headers())
    issued_at = int(datetime.now(timezone.utc).timestamp())
    response.set_cookie(
        COOKIE_NAME,
        _signed_blob(
            {
                "openid": openid,
                "app_id": _payment_oauth_app_id(),
                "customer_id": customer_id,
                "payer_identity_id": payer_identity_id,
                "unionid": unionid,
                "payer_name": payer_name,
                "iat": issued_at,
                "exp": issued_at + WECHAT_PAYMENT_IDENTITY_TTL_SECONDS,
            }
        ),
        max_age=WECHAT_PAYMENT_IDENTITY_TTL_SECONDS,
        httponly=True,
        secure=secure_cookie_environment() or public_base_url.startswith("https://"),
        samesite="lax",
        path="/",
    )
    return attributed_response(response)


def checkout_page_state(product: dict[str, Any], request: Request) -> dict[str, Any]:
    from aicrm_next.extensions.commerce.commerce.coupons.application import target_ref_for_product_id

    identity = _identity_from_request(request)
    code = _normalized_text(product.get("product_code"))
    access = evaluate_wechat_unionid_access(
        identity,
        is_wechat_browser=_is_wechat_browser(request),
        oauth_start_url=payment_oauth_start_url(f"/pay/{code}"),
    )
    paid_order = _existing_paid_order_for_checkout(product, identity) if access.allowed else None
    context_token = _normalized_text(request.cookies.get(SIDEBAR_PRODUCT_CONTEXT_COOKIE))
    context_result = load_sidebar_product_context_token(context_token)
    pay_path = f"/pay/{code}"
    product_id = product.get("id")
    coupon_target_ref = target_ref_for_product_id(product_id) if _normalized_text(product_id) else ""
    return {
        "product": {
            "product_code": code,
            "name": _normalized_text(product.get("title") or product.get("name")),
            "amount_total": int(product.get("price_cents") or product.get("amount_total") or 0),
            "currency": _normalized_text(product.get("currency")) or "CNY",
        },
        "identity_ready": access.allowed,
        "identity_error": access.error,
        "identity_message": access.message,
        "is_wechat_browser": _is_wechat_browser(request),
        "oauth_start_url": payment_oauth_start_url(pay_path),
        "create_order_url": "/api/h5/wechat-pay/jsapi/orders",
        "status_url_template": "/api/h5/wechat-pay/orders/{out_trade_no}",
        "enabled": _env_bool("WECHAT_PAY_ENABLED", False),
        "require_mobile": bool(product.get("require_mobile")),
        "cta_text": _normalized_text(product.get("buy_button_text")) or "确认支付",
        "completion_target": product.get("completion_target") or {},
        "completion_action": product.get("completion_action") or {"type": "default", "redirect_url": ""},
        "paid_order": paid_order,
        "price_display": format_price(product),
        "context_status": _normalized_text(context_result.get("status")) or "missing",
        "coupon_target_ref": coupon_target_ref,
        "available_coupon_url": (f"/api/h5/coupons/available?{urlencode({'target_ref': coupon_target_ref})}" if coupon_target_ref else ""),
    }


def _client_config() -> WeChatPayClientConfig:
    app_id = _env("WECHAT_PAY_APP_ID") or _env("WECHAT_MP_APP_ID")
    return WeChatPayClientConfig(
        app_id=app_id,
        mch_id=_env("WECHAT_PAY_MCH_ID"),
        api_v3_key=_sensitive_setting("WECHAT_PAY_API_V3_KEY"),
        private_key_path=_env("WECHAT_PAY_PRIVATE_KEY_PATH"),
        merchant_serial_no=_sensitive_setting("WECHAT_PAY_CERT_SERIAL_NO"),
        platform_public_key_path=_env("WECHAT_PAY_PLATFORM_PUBLIC_KEY_PATH"),
        platform_serial_no=_env("WECHAT_PAY_PLATFORM_CERT_SERIAL_NO"),
        api_base=_env("WECHAT_PAY_API_BASE") or "https://api.mch.weixin.qq.com",
        timeout_seconds=_env_int("WECHAT_PAY_TIMEOUT_SECONDS", 10),
    )


def _require_payment_ready() -> WeChatPayClientConfig:
    if not _env_bool("WECHAT_PAY_ENABLED", False):
        raise RuntimeError("wechat_pay_disabled")
    config = _client_config()
    missing = [
        key
        for key, value in {
            "WECHAT_PAY_APP_ID/WECHAT_MP_APP_ID": config.app_id,
            "WECHAT_PAY_MCH_ID": config.mch_id,
            "WECHAT_PAY_PRIVATE_KEY_PATH": config.private_key_path,
            "WECHAT_PAY_CERT_SERIAL_NO": config.merchant_serial_no,
        }.items()
        if not _normalized_text(value)
    ]
    if missing:
        raise RuntimeError("missing WeChat Pay config: " + ", ".join(missing))
    if not production_data_ready():
        raise RuntimeError("production_database_required")
    return config


def _jsonb(value: Any):
    from psycopg.types.json import Jsonb

    return Jsonb(value, dumps=lambda data: json.dumps(data, ensure_ascii=False, default=str))


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _public_coupon_summary(value: Any) -> dict[str, Any]:
    summary = _json_object(value)
    for key in ("claim_no", "claim_id", "coupon_claim_id", "idempotency_key_hash"):
        summary.pop(key, None)
    return summary


def _out_trade_no() -> str:
    return "WXP" + datetime.now(timezone.utc).strftime("%y%m%d%H%M%S") + secrets.token_hex(6).upper()


def _expires_at() -> str:
    return pending_order_expires_at_text()


def _safe_success_url(value: Any) -> str:
    normalized = _normalized_text(value)
    if normalized.startswith("/") and not normalized.startswith("//"):
        return normalized
    if normalized.startswith(("https://", "http://")):
        return normalized
    return ""


def _is_order_fully_refunded(row: dict[str, Any]) -> bool:
    amount_total = int(row.get("amount_total") or 0)
    refunded_amount_total = int(row.get("refunded_amount_total") or 0)
    return _normalized_text(row.get("refund_status")) == "full_refunded" or (amount_total > 0 and refunded_amount_total >= amount_total)


def _is_order_effectively_paid(row: dict[str, Any]) -> bool:
    if _is_order_fully_refunded(row):
        return False
    return _normalized_text(row.get("status")) == "paid" or _normalized_text(row.get("trade_state")) == "SUCCESS"


def _enqueue_payment_succeeded_internal_event_outbox(
    conn: Any,
    *,
    order: dict[str, Any],
    transaction: dict[str, Any],
    source_route: str,
) -> dict[str, Any] | None:
    request = build_payment_succeeded_event_request(
        order=order,
        transaction=transaction,
        domain_event_outbox_id=None,
        source_route=source_route,
    )
    if request is None:
        return None
    result = enqueue_transactional_internal_event_outbox(conn, request)
    LOGGER.info(
        "payment_succeeded_internal_event_outbox_ensured",
        extra=safe_log_fields(source_command_id=request.source_command_id, outbox_id=result.get("outbox_id")),
    )
    return result


def _completion_redirect_from_product(product: dict[str, Any]) -> dict[str, Any]:
    completion_redirect = completion_redirect_projection(
        product.get("completion_redirect_enabled"),
        product.get("completion_redirect_url"),
    )
    completion_target = completion_target_projection(
        product.get("completion_target_json") if product.get("completion_target_json") is not None else product.get("completion_target"),
        legacy_h5_url=completion_redirect.get("completion_redirect_url"),
        legacy_enabled=completion_redirect.get("completion_redirect_enabled"),
    )
    return {
        **completion_redirect,
        **completion_target,
        "completion_action": completion_action_for_target(
            completion_target["completion_target"],
            legacy_redirect_url=completion_redirect.get("completion_redirect_url"),
            legacy_enabled=completion_redirect.get("completion_redirect_enabled"),
        ),
    }


def _completion_redirect_for_product_code(conn: Any, product_code: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT completion_redirect_enabled, completion_redirect_url, completion_target_json
        FROM wechat_pay_products
        WHERE product_code = %s
        LIMIT 1
        """,
        (_normalized_text(product_code),),
    ).fetchone()
    if not row:
        return completion_redirect_projection(False, "")
    return _completion_redirect_from_product(dict(row))


def _lead_qr_payload(row: dict[str, Any] | None) -> dict[str, Any]:
    row = dict(row or {})
    qr_url = _normalized_text(row.get("qr_url"))
    if not qr_url:
        return {}
    return {
        "channel_id": int(row.get("channel_id") or 0),
        "channel_name": _normalized_text(row.get("channel_name")),
        "qr_url": qr_url,
        "status": _normalized_text(row.get("status")),
    }


def _lead_qr_with_product_copy(qr: dict[str, Any], product: dict[str, Any]) -> dict[str, Any]:
    if not qr:
        return {}
    return {
        **qr,
        "title": _normalized_text(product.get("lead_qr_title")),
        "subtitle": _normalized_text(product.get("lead_qr_subtitle")),
    }


def _resolve_lead_channel_qr(conn: Any, *, channel_id: int | None = None) -> dict[str, Any]:
    if channel_id:
        row = conn.execute(
            """
            SELECT
                c.id AS channel_id,
                c.channel_name,
                COALESCE(NULLIF(active_asset.qr_url, ''), NULLIF(c.qr_url, ''), '') AS qr_url,
                c.status
            FROM automation_channel c
            LEFT JOIN LATERAL (
                SELECT qa.qr_url
                FROM automation_channel_qrcode_asset qa
                WHERE qa.channel_id = c.id
                  AND qa.status = 'active'
                  AND NULLIF(qa.qr_url, '') IS NOT NULL
                ORDER BY qa.generated_at DESC, qa.id DESC
                LIMIT 1
            ) active_asset ON TRUE
            WHERE c.id = %s
            LIMIT 1
            """,
            (int(channel_id),),
        ).fetchone()
        return _lead_qr_payload(row)
    return {}


def _lead_qr_for_product_code(conn: Any, product_code: str) -> dict[str, Any]:
    product = conn.execute(
        """
        SELECT lead_channel_id, lead_qr_title, lead_qr_subtitle
        FROM wechat_pay_products
        WHERE product_code = %s
        LIMIT 1
        """,
        (_normalized_text(product_code),),
    ).fetchone()
    if not product:
        return {}
    channel_id = int(product.get("lead_channel_id") or 0) or None
    return _lead_qr_with_product_copy(_resolve_lead_channel_qr(conn, channel_id=channel_id), dict(product))


def _completion_projection_blocks_lead_qr(completion: dict[str, Any]) -> bool:
    redirect = completion.get("completion_redirect") if isinstance(completion.get("completion_redirect"), dict) else {}
    target = completion.get("completion_target") if isinstance(completion.get("completion_target"), dict) else {}
    return bool(redirect.get("enabled") or target.get("enabled"))


def _lead_qr_for_product(conn: Any, product: dict[str, Any]) -> dict[str, Any]:
    completion = _completion_redirect_from_product(product)
    if _completion_projection_blocks_lead_qr(completion):
        return {}
    try:
        channel_id = int(product.get("lead_channel_id") or 0) or None
    except (TypeError, ValueError):
        channel_id = None
    if channel_id:
        return _lead_qr_with_product_copy(_resolve_lead_channel_qr(conn, channel_id=channel_id), product)
    return _lead_qr_for_product_code(conn, _normalized_text(product.get("product_code")))


def resolve_product_lead_qr(product: dict[str, Any]) -> dict[str, Any]:
    """Resolve the configured post-purchase channel QR without external calls."""

    if not production_data_ready():
        return {}
    normalized = dict(product or {})
    if _completion_projection_blocks_lead_qr(_completion_redirect_from_product(normalized)):
        return {}
    try:
        with _connect() as conn:
            return _lead_qr_for_product(conn, normalized)
    except Exception as exc:
        safe_log_exception(
            LOGGER,
            "public_product_lead_qr_resolution_failed",
            exc,
            product_code=_normalized_text(normalized.get("product_code")),
        )
        return {}


def _resolve_payment_oneid(conn: Any, identity: dict[str, str]) -> dict[str, Any]:
    app_id = _normalized_text(identity.get("app_id"))
    openid = _normalized_text(identity.get("openid"))
    unionid = _normalized_text(identity.get("unionid"))
    if not app_id or not openid or not unionid or app_id != _payment_oauth_app_id():
        return {"status": "invalid"}
    return PostgresOneIDService().ensure_verified_wechat_identity_with_db(
        PostgresConnection(conn),
        app_id=app_id,
        openid=openid,
        unionid=unionid,
        source_type="wechat_oauth_session",
    )


def _sidebar_recipient_customer_id(conn: Any, context: dict[str, Any]) -> int:
    corp_id = _env("WECOM_CORP_ID")
    external_userid = _normalized_text(context.get("external_userid"))
    owner_userid = _normalized_text(context.get("owner_userid"))
    if not corp_id or not external_userid or not owner_userid:
        return 0
    row = conn.execute(
        """
        SELECT aicrm_customer_root_id(identity.customer_id) AS customer_id
        FROM customer_identities identity
        JOIN wecom_external_contact_follow_users relation
          ON relation.corp_id = identity.scope_key
         AND relation.external_userid = identity.normalized_value
         AND relation.user_id = %s
         AND relation.relation_status = 'active'
        WHERE identity.provider = 'wecom'
          AND identity.identity_type = 'external_userid'
          AND identity.scope_key = %s
          AND identity.normalized_value = %s
          AND identity.status = 'active'
        LIMIT 1
        """,
        (owner_userid, corp_id, external_userid),
    ).fetchone()
    return int((row or {}).get("customer_id") or 0)


def _paid_order_for_product_identity(
    conn: Any,
    *,
    product: dict[str, Any],
    identity: dict[str, str],
    payer_identity_id: int = 0,
) -> dict[str, Any] | None:
    product_codes = product_code_filter_values(product.get("product_code"))
    identity_id = int(payer_identity_id or 0)
    if not product_codes or not identity_id:
        return None
    params: list[Any] = [*product_codes, identity_id]
    product_placeholders = ", ".join(["%s"] * len(product_codes))
    row = conn.execute(
        f"""
        SELECT *
        FROM wechat_pay_orders
        WHERE product_code IN ({product_placeholders})
          AND (status = 'paid' OR trade_state = 'SUCCESS')
          AND NOT (
            COALESCE(refund_status, '') = 'full_refunded'
            OR (amount_total > 0 AND COALESCE(refunded_amount_total, 0) >= amount_total)
          )
          AND payer_identity_id = %s
        ORDER BY paid_at DESC NULLS LAST, updated_at DESC NULLS LAST, created_at DESC NULLS LAST, id DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    return dict(row) if row else None


def _paid_order_payload_for_product_identity(
    conn: Any,
    *,
    product: dict[str, Any],
    identity: dict[str, str],
    payer_identity_id: int = 0,
) -> dict[str, Any] | None:
    order = _paid_order_for_product_identity(
        conn,
        product=product,
        identity=identity,
        payer_identity_id=payer_identity_id,
    )
    if not order:
        return None
    completion_redirect = _completion_redirect_from_product(product)
    lead_qr = _lead_qr_for_product(conn, product)
    return _order_payload(order, completion_redirect=completion_redirect, lead_qr=lead_qr)


def _active_order_for_client_reference(
    conn: Any,
    *,
    order_source: str,
    client_order_ref: str,
    payer_identity_id: int,
    product_code: str,
) -> dict[str, Any] | None:
    """Serialize retries for one browser checkout and return its active order."""

    reference = _normalized_text(client_order_ref)
    if not reference:
        return None
    lock_key = "|".join(
        (
            _normalized_text(order_source) or "h5_checkout",
            str(int(payer_identity_id)),
            _normalized_text(product_code),
            reference,
        )
    )
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,))
    row = conn.execute(
        """
        SELECT *
        FROM wechat_pay_orders
        WHERE order_source = %s
          AND client_order_ref = %s
          AND payer_identity_id = %s
          AND product_code = %s
          AND COALESCE(status, '') NOT IN ('failed', 'closed')
          AND COALESCE(trade_state, '') NOT IN ('CLOSED', 'REVOKED')
        ORDER BY id DESC
        LIMIT 1
        FOR UPDATE
        """,
        (
            _normalized_text(order_source) or "h5_checkout",
            reference,
            int(payer_identity_id),
            _normalized_text(product_code),
        ),
    ).fetchone()
    return dict(row) if row else None


def _existing_paid_order_for_checkout(product: dict[str, Any], identity: dict[str, str]) -> dict[str, Any] | None:
    if not production_data_ready():
        return None
    try:
        with _connect() as conn:
            resolution = _resolve_payment_oneid(conn, identity)
            if resolution.get("status") != "resolved":
                return None
            conn.commit()
            return _paid_order_payload_for_product_identity(
                conn,
                product=product,
                identity=identity,
                payer_identity_id=int(resolution.get("openid_identity_id") or resolution.get("identity_id") or 0),
            )
    except Exception:
        return None


def _order_payload(
    row: dict[str, Any],
    *,
    completion_redirect: dict[str, Any] | None = None,
    lead_qr: dict[str, Any] | None = None,
) -> dict[str, Any]:
    effective_completion = completion_redirect or completion_redirect_projection(
        row.get("completion_redirect_enabled"),
        row.get("completion_redirect_url"),
    )
    completion = dict(effective_completion.get("completion_redirect") or {})
    completion_url = safe_completion_redirect_url(completion.get("url") or effective_completion.get("completion_redirect_url"))
    completion_enabled = bool(completion.get("enabled")) and bool(completion_url)
    completion_target = (
        (effective_completion.get("completion_target") or effective_completion.get("completion_target_json") or {})
        if isinstance(effective_completion, dict)
        else {}
    )
    completion_action = completion_action_with_lead_qr(
        completion_target if isinstance(completion_target, dict) else {},
        lead_qr=lead_qr if _is_order_effectively_paid(row) else None,
        legacy_redirect_url=completion_url,
        legacy_enabled=completion_enabled,
    )
    status = _normalized_text(row.get("status"))
    if _is_order_fully_refunded(row):
        status = "full_refunded"
    payload = {
        "out_trade_no": _normalized_text(row.get("out_trade_no")),
        "product_code": _normalized_text(row.get("product_code")),
        "product_name": _normalized_text(row.get("product_name")),
        "amount_total": int(row.get("amount_total") or 0),
        "subtotal_amount_total": int(row.get("subtotal_amount_total") or row.get("amount_total") or 0),
        "discount_amount_total": int(row.get("discount_amount_total") or 0),
        "coupon_summary": _public_coupon_summary(row.get("coupon_snapshot_json")),
        "currency": _normalized_text(row.get("currency")) or "CNY",
        "status": status,
        "trade_state": _normalized_text(row.get("trade_state")),
        "refund_status": _normalized_text(row.get("refund_status")),
        "refunded_amount_total": int(row.get("refunded_amount_total") or 0),
        "success_url": _safe_success_url(row.get("success_url")),
        "paid_at": _normalized_text(row.get("paid_at")),
        "created_at": _normalized_text(row.get("created_at")),
        "completion_redirect_enabled": bool(effective_completion.get("completion_redirect_enabled")),
        "completion_redirect_url": completion_url,
        "completion_redirect": {"enabled": completion_enabled, "url": completion_url if completion_enabled else ""},
        "completion_target": completion_target if isinstance(completion_target, dict) else {},
        "completion_action": completion_action,
    }
    if completion_action.get("type") == "lead_qr":
        payload["lead_qr"] = completion_action.get("lead_qr") or lead_qr
        payload["completion_action"] = {"type": "lead_qr", "redirect_url": ""}
    return payload


def _insert_order(
    conn: Any,
    *,
    product: dict[str, Any],
    identity: dict[str, str],
    mobile: str,
    out_trade_no: str,
    request_meta: dict[str, Any] | None = None,
    order_source: str = "h5_checkout",
    client_order_ref: str = "",
) -> dict[str, Any]:
    return build_wechat_pay_order_write_port().create_dbapi(
        conn,
        WeChatPayOrderCreate(
            out_trade_no=out_trade_no,
            order_source=_normalized_text(order_source) or "h5_checkout",
            product_code=product["product_code"],
            product_name=product["title"],
            description=product.get("description") or product["title"],
            amount_total=int(product.get("price_cents") or 0),
            currency=product.get("currency") or "CNY",
            customer_id=int(identity.get("customer_id") or 0),
            payer_identity_id=int(identity.get("payer_identity_id") or 0),
            unionid=identity.get("unionid") or "",
            payer_name_snapshot=identity.get("payer_name") or "",
            success_url=_safe_success_url(product.get("completion_redirect_url")),
            metadata_json=_jsonb(
                {
                    "completion_redirect": product.get("completion_redirect") or {},
                    "payer_identity": {
                        "openid": identity.get("openid") or "",
                        "respondent_key": identity.get("respondent_key") or "",
                        "external_userid": identity.get("external_userid") or "",
                        "owner_userid": identity.get("owner_userid") or "",
                        "mobile": mobile,
                    },
                }
            ),
            request_meta_json=_jsonb(request_meta or {}),
            expires_at=_expires_at(),
            client_order_ref=_normalized_text(client_order_ref),
        ),
    )


def _update_payment_request(
    conn: Any, out_trade_no: str, *, prepay_id: str, request_payload: dict[str, Any], response_payload: dict[str, Any]
) -> dict[str, Any]:
    return build_wechat_pay_order_write_port().update_payment_request_dbapi(
        conn,
        out_trade_no=out_trade_no,
        prepay_id=prepay_id,
        request_payload_json=_jsonb(request_payload),
        response_payload_json=_jsonb(response_payload),
    )


def _mark_order_failed(conn: Any, out_trade_no: str, error_message: str) -> None:
    build_wechat_pay_order_write_port().mark_failed_dbapi(
        conn,
        out_trade_no=out_trade_no,
        last_error=error_message,
    )


def _mark_order_provider_unknown(conn: Any, out_trade_no: str, error_message: str) -> None:
    """Keep a provider-uncertain order eligible for the reconciliation worker.

    A transport failure can happen after WeChat accepted the transaction.  In
    that case releasing the coupon would allow the same claim to fund two
    orders, so the reservation is deliberately retained until provider state is
    queried by out_trade_no.
    """

    build_wechat_pay_order_write_port().mark_provider_unknown_dbapi(
        conn,
        out_trade_no=out_trade_no,
        last_error=error_message,
    )


def _project_order_mobile_to_identity(conn: Any, order: dict[str, Any], *, source_route: str) -> dict[str, Any]:
    return project_payment_order_mobile(conn, order, source_route=source_route)


def _safe_project_order_mobile_to_identity(conn: Any, order: dict[str, Any], *, source_route: str) -> dict[str, Any]:
    try:
        return _project_order_mobile_to_identity(conn, order, source_route=source_route)
    except Exception as exc:
        safe_log_exception(
            LOGGER,
            "wechat_pay_order_mobile_projection_failed",
            exc,
            out_trade_no=_normalized_text(order.get("out_trade_no")),
            source_route=source_route,
        )
        return {"ok": False, "projected": False, "reason": "projection_error"}


def _assert_transaction_amount(order: dict[str, Any], transaction: dict[str, Any]) -> tuple[int, str]:
    amount = transaction.get("amount") if isinstance(transaction.get("amount"), dict) else {}
    strict_coupon_order = bool(order.get("coupon_claim_id"))
    raw_total = amount.get("total")
    if raw_total is None and strict_coupon_order:
        raise RuntimeError("wechat_pay_total_required_for_coupon_order")
    provider_total = int(raw_total if raw_total is not None else amount.get("payer_total") or 0)
    provider_currency = _normalized_text(amount.get("currency"))
    order_total = int(order.get("amount_total") or 0)
    order_currency = _normalized_text(order.get("currency")) or "CNY"
    if provider_total != order_total:
        raise RuntimeError("wechat_pay_order_amount_mismatch")
    if strict_coupon_order and not provider_currency:
        raise RuntimeError("wechat_pay_currency_required_for_coupon_order")
    if provider_currency and provider_currency != order_currency:
        raise RuntimeError("wechat_pay_order_currency_mismatch")
    return provider_total, provider_currency or order_currency


def _apply_transaction(conn: Any, transaction: dict[str, Any], *, source_route: str = "/api/h5/wechat-pay/notify") -> dict[str, Any]:
    trade_no = _normalized_text(transaction.get("out_trade_no"))
    trade_state = _normalized_text(transaction.get("trade_state"))
    status = "paid" if trade_state == "SUCCESS" else ("closed" if trade_state in {"CLOSED", "REVOKED"} else "paying")
    amount = transaction.get("amount") if isinstance(transaction.get("amount"), dict) else {}
    previous = conn.execute("SELECT * FROM wechat_pay_orders WHERE out_trade_no = %s LIMIT 1 FOR UPDATE", (trade_no,)).fetchone()
    previous_payload = dict(previous or {})
    if not previous_payload:
        raise RuntimeError("wechat_pay_order_not_found")
    transaction_app_id = _normalized_text(transaction.get("appid"))
    if transaction_app_id != _client_config().app_id:
        raise RuntimeError("wechat_pay_appid_mismatch")
    payer = transaction.get("payer") if isinstance(transaction.get("payer"), dict) else {}
    payer_openid = _normalized_text(payer.get("openid"))
    if not payer_openid:
        raise RuntimeError("wechat_pay_payer_openid_missing")
    payer_resolution = PostgresOneIDService().ensure_verified_wechat_identity_with_db(
        PostgresConnection(conn),
        app_id=transaction_app_id,
        openid=payer_openid,
        source_type="wechat_pay_transaction",
        source_event_id=_normalized_text(transaction.get("transaction_id")),
    )
    callback_identity_id = int(payer_resolution.get("openid_identity_id") or payer_resolution.get("identity_id") or 0)
    if callback_identity_id != int(previous_payload.get("payer_identity_id") or 0):
        raise RuntimeError("wechat_pay_payer_identity_mismatch")
    if trade_state == "SUCCESS":
        _assert_transaction_amount(previous_payload, transaction)
    was_paid = _normalized_text((previous or {}).get("status")) == "paid" or _normalized_text((previous or {}).get("trade_state")) == "SUCCESS"
    order_payload = build_wechat_pay_order_write_port().apply_provider_transaction_dbapi(
        conn,
        out_trade_no=trade_no,
        status=status,
        trade_state=trade_state,
        transaction_id=_normalized_text(transaction.get("transaction_id")),
        bank_type=_normalized_text(transaction.get("bank_type")),
        payer_total=int(amount.get("payer_total") or amount.get("total") or 0),
        success_time=_normalized_text(transaction.get("success_time")),
        notify_payload_json=_jsonb(transaction),
    )
    is_paid = _normalized_text(order_payload.get("status")) == "paid" or _normalized_text(order_payload.get("trade_state")) == "SUCCESS"
    if is_paid and order_payload.get("coupon_claim_id"):
        from aicrm_next.extensions.commerce.commerce.coupons.application import consume_coupon_for_paid_order

        amount_total, currency = _assert_transaction_amount(order_payload, transaction)
        consume_coupon_for_paid_order(
            conn,
            out_trade_no=trade_no,
            provider_total=amount_total,
            provider_currency=currency,
        )
    elif trade_state in {"CLOSED", "REVOKED"} and order_payload.get("coupon_claim_id"):
        from aicrm_next.extensions.commerce.commerce.coupons.application import release_coupon_for_order

        release_coupon_for_order(conn, out_trade_no=trade_no, reason=f"wechat_pay_{trade_state.lower()}")
    if is_paid:
        mobile_projection = _safe_project_order_mobile_to_identity(conn, order_payload, source_route=source_route)
        internal_event_outbox = _enqueue_payment_succeeded_internal_event_outbox(
            conn,
            order=order_payload,
            transaction=transaction,
            source_route=source_route,
        )
        LOGGER.info(
            "wechat_pay_payment_succeeded_outbox_ensured",
            extra=safe_log_fields(
                order_id=order_payload.get("id"),
                out_trade_no=_normalized_text(order_payload.get("out_trade_no")),
                event_type=PAYMENT_SUCCEEDED_EVENT_TYPE,
                internal_event_outbox_id=(internal_event_outbox or {}).get("outbox_id"),
                was_paid=was_paid,
                mobile_projected=bool(mobile_projection.get("projected")),
                mobile_projection_reason=_normalized_text(mobile_projection.get("reason")),
            ),
        )
    return order_payload


def create_jsapi_order_response(
    request: Request,
    payload: dict[str, Any],
    *,
    product_override: dict[str, Any] | None = None,
    checkout_return_path: str = "",
    allow_paid_reuse: bool = True,
    order_source: str = "h5_checkout",
    request_meta_extra: dict[str, Any] | None = None,
) -> JSONResponse:
    from aicrm_next.extensions.commerce.commerce.coupons.application import (
        CouponPublicApplication,
        normalize_coupon_choice,
        release_coupon_for_order,
        reserve_coupon_for_order,
        target_ref_for_product_id,
    )

    if not _is_wechat_browser(request):
        return JSONResponse(
            {"ok": False, "error": "wechat_browser_required", "message": "请在微信中打开后完成授权。"},
            status_code=403,
            headers=route_headers(),
        )
    product_code = _normalized_text(payload.get("product_code"))
    if product_override is None:
        try:
            product = get_public_product(product_code)
        except Exception:
            return JSONResponse(product_not_found_payload(product_code), status_code=404, headers=route_headers())
    else:
        product = dict(product_override)
        product_code = _normalized_text(product.get("product_code") or product_code)
    identity = _identity_from_request(request)
    access = evaluate_wechat_unionid_access(
        identity,
        is_wechat_browser=True,
        oauth_start_url=payment_oauth_start_url(checkout_return_path or f"/pay/{product_code}"),
    )
    if not access.allowed:
        return JSONResponse(
            access.payload(),
            status_code=access.status_code,
            headers=route_headers(),
        )
    try:
        config = _require_payment_ready()
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503, headers=route_headers())
    mobile = _normalized_text(payload.get("mobile"))
    context_token = _normalized_text(request.cookies.get(SIDEBAR_PRODUCT_CONTEXT_COOKIE))
    resolved_context = resolve_sidebar_order_context(
        context_token=context_token,
        payment_identity=identity,
        product=product,
        payload_mobile=mobile,
    )
    if product.get("require_mobile") and not _normalized_text(resolved_context.get("mobile")):
        error_code = "mobile_invalid" if mobile else "mobile_required"
        return JSONResponse({"ok": False, "error": error_code}, status_code=400, headers=route_headers())
    order_identity = {
        **identity,
        "external_userid": _normalized_text(resolved_context.get("external_userid")),
        "owner_userid": _normalized_text(resolved_context.get("owner_userid")),
    }
    request_meta = {
        "sidebar_product_context": {
            "context_status": resolved_context.get("context_status"),
            "context_source": resolved_context.get("context_source"),
            "external_userid_present": bool(resolved_context.get("external_userid")),
            "owner_userid_present": bool(resolved_context.get("owner_userid")),
            "mobile_source": resolved_context.get("mobile_source"),
        }
    }
    if request_meta_extra:
        request_meta.update(request_meta_extra)
    out_trade_no = _out_trade_no()
    client_order_ref = _normalized_text(payload.get("client_order_ref"))
    try:
        coupon_choice = normalize_coupon_choice(payload)
    except ContractError as exc:
        return JSONResponse(
            {"ok": False, "error": "coupon_choice_invalid", "message": str(exc)},
            status_code=400,
            headers=route_headers(),
        )
    notify_url = _env("WECHAT_PAY_NOTIFY_URL") or f"{_external_base_url(request)}/api/h5/wechat-pay/notify"
    order_persisted = False
    provider_invoked = False
    canonical_unionid = ""
    payer_identity_id = 0
    try:
        # Phase 1: order creation and coupon reservation are one local
        # transaction.  It commits before any external payment request.
        with _connect() as conn:
            oneid_resolution = _resolve_payment_oneid(conn, identity)
            if oneid_resolution.get("status") != "resolved":
                conflict = oneid_resolution.get("status") == "conflict"
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "identity_conflict" if conflict else "identity_resolution_required",
                        "identity_status": oneid_resolution.get("status"),
                        "retryable": not conflict,
                    },
                    status_code=409,
                    headers=route_headers(),
                )
            payer_identity_id = int(oneid_resolution.get("openid_identity_id") or oneid_resolution.get("identity_id") or 0)
            payer_customer_id = int(oneid_resolution.get("customer_id") or 0)
            canonical_unionid = _normalized_text(identity.get("unionid"))
            recipient_customer_id = _sidebar_recipient_customer_id(conn, resolved_context) or payer_customer_id
            order_identity.update(
                {
                    "customer_id": recipient_customer_id,
                    "payer_identity_id": payer_identity_id,
                    "unionid": canonical_unionid,
                }
            )
            existing_paid_order = (
                _paid_order_payload_for_product_identity(
                    conn,
                    product=product,
                    identity=identity,
                    payer_identity_id=payer_identity_id,
                )
                if allow_paid_reuse
                else None
            )
            if existing_paid_order is not None:
                return JSONResponse({"ok": True, "already_paid": True, "order": existing_paid_order}, headers=route_headers())
            existing_client_order = _active_order_for_client_reference(
                conn,
                order_source=order_source,
                client_order_ref=client_order_ref,
                payer_identity_id=payer_identity_id,
                product_code=product_code,
            )
            if existing_client_order is not None:
                conn.commit()
                completion_redirect = _completion_redirect_from_product(product)
                if _is_order_effectively_paid(existing_client_order):
                    return JSONResponse(
                        {
                            "ok": True,
                            "already_paid": True,
                            "order": _order_payload(
                                existing_client_order,
                                completion_redirect=completion_redirect,
                            ),
                        },
                        headers=route_headers(),
                    )
                existing_prepay_id = _normalized_text(existing_client_order.get("prepay_id"))
                if existing_prepay_id and _normalized_text(existing_client_order.get("status")) == "paying":
                    pay_params = WeChatPayClient(config).build_jsapi_pay_params(existing_prepay_id)
                    return JSONResponse(
                        {
                            "ok": True,
                            "idempotent_replay": True,
                            "order": _order_payload(
                                existing_client_order,
                                completion_redirect=completion_redirect,
                            ),
                            "pay_params": pay_params,
                        },
                        headers=route_headers(),
                    )
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "order_reconciliation_pending",
                        "retryable": True,
                        "out_trade_no": _normalized_text(existing_client_order.get("out_trade_no")),
                    },
                    status_code=409,
                    headers=route_headers(),
                )
            order = _insert_order(
                conn,
                product=product,
                identity=order_identity,
                mobile=_normalized_text(resolved_context.get("mobile")),
                out_trade_no=out_trade_no,
                request_meta=request_meta,
                order_source=order_source,
                client_order_ref=client_order_ref,
            )
            reserved = reserve_coupon_for_order(
                conn,
                order=order,
                coupon_choice=coupon_choice,
                unionid=canonical_unionid,
                trade_product_id=product.get("id"),
            )
            order = dict(reserved or order)
            conn.commit()
        order_persisted = True

        transaction_payload = {
            "appid": config.app_id,
            "mchid": config.mch_id,
            "description": _normalized_text(product.get("title"))[:127],
            "out_trade_no": out_trade_no,
            "notify_url": notify_url,
            "amount": {
                "total": int(order.get("amount_total") or 0),
                "currency": _normalized_text(order.get("currency")) or "CNY",
            },
            "payer": {"openid": identity["openid"]},
            "attach": json.dumps(
                {"product_code": product["product_code"], "client_order_ref": client_order_ref},
                ensure_ascii=False,
                separators=(",", ":"),
            )[:128],
        }

        # Phase 2: WeChat is called without an open database transaction.  A
        # transport-uncertain result retains the coupon reservation.
        client = WeChatPayClient(config)
        provider_invoked = True
        response_payload = client.create_jsapi_transaction(transaction_payload)
        prepay_id = _normalized_text(response_payload.get("prepay_id"))
        if not prepay_id:
            raise WeChatPayClientError("missing prepay_id from WeChat Pay")
        pay_params = client.build_jsapi_pay_params(prepay_id)
        with _connect() as conn:
            order = _update_payment_request(
                conn,
                out_trade_no,
                prepay_id=prepay_id,
                request_payload=transaction_payload,
                response_payload=response_payload,
            )
            conn.commit()
        completion_redirect = _completion_redirect_from_product(product)
        return JSONResponse(
            {"ok": True, "order": _order_payload(order, completion_redirect=completion_redirect), "pay_params": pay_params},
            headers=route_headers(
                real_external_call_executed=True,
                payment_request_executed=True,
                order_create_executed=True,
            ),
        )
    except ContractError as exc:
        target_ref = target_ref_for_product_id(product.get("id"))
        latest_available: list[dict[str, Any]] = []
        if canonical_unionid:
            try:
                latest_payload = CouponPublicApplication().list_available_claims(
                    target_ref,
                    identity={**identity, "unionid": canonical_unionid},
                )
                latest_available = list(latest_payload.get("items") or [])
            except Exception:
                latest_available = []
        return JSONResponse(
            {
                "ok": False,
                "error": "coupon_unavailable",
                "message": str(exc),
                "available_coupons": latest_available,
                "available_coupon_url": ("/api/h5/coupons/available?" + urlencode({"target_ref": target_ref})),
            },
            status_code=409,
            headers=route_headers(),
        )
    except Exception as exc:
        definitive_provider_failure = (
            isinstance(exc, WeChatPayClientError) and exc.status_code is not None and 400 <= int(exc.status_code) < 500 and int(exc.status_code) != 429
        )
        if order_persisted:
            try:
                with _connect() as conn:
                    if provider_invoked and not definitive_provider_failure:
                        _mark_order_provider_unknown(conn, out_trade_no, str(exc))
                    else:
                        _mark_order_failed(conn, out_trade_no, str(exc))
                        release_coupon_for_order(conn, out_trade_no=out_trade_no, reason="payment_create_failed")
                    conn.commit()
            except Exception:
                pass
        error_code = (
            "wechat_pay_provider_outcome_unknown"
            if order_persisted and provider_invoked and not definitive_provider_failure
            else "create_wechat_pay_order_failed"
        )
        return JSONResponse(
            {
                "ok": False,
                "error": error_code,
                "retryable": error_code == "wechat_pay_provider_outcome_unknown",
                "out_trade_no": out_trade_no if order_persisted else "",
            },
            status_code=502,
            headers=route_headers(
                real_external_call_executed=provider_invoked,
                payment_request_executed=provider_invoked,
                order_create_executed=order_persisted,
            ),
        )


def order_status_response(out_trade_no: str, request: Request) -> JSONResponse:
    if not production_data_ready():
        return JSONResponse({"ok": False, "error": "production_database_required"}, status_code=503, headers=route_headers())
    trade_no = _normalized_text(out_trade_no)
    identity = _identity_from_request(request)
    access = evaluate_wechat_unionid_access(
        identity,
        is_wechat_browser=_is_wechat_browser(request),
        oauth_start_url=payment_oauth_start_url(f"/api/h5/wechat-pay/orders/{trade_no}"),
    )
    if not access.allowed:
        return JSONResponse(
            access.payload(),
            status_code=access.status_code,
            headers=route_headers(),
        )
    with _connect() as conn:
        identity_result = _resolve_payment_oneid(conn, identity)
        payer_identity_id = int(identity_result.get("openid_identity_id") or identity_result.get("identity_id") or 0)
        if identity_result.get("status") != "resolved" or not payer_identity_id:
            return JSONResponse(
                {"ok": False, "error": "payment_identity_unresolved"},
                status_code=403,
                headers=route_headers(),
            )
        locked_order = conn.execute(
            "SELECT * FROM wechat_pay_orders WHERE out_trade_no = %s LIMIT 1 FOR UPDATE",
            (trade_no,),
        ).fetchone()
        if not locked_order or int(locked_order.get("payer_identity_id") or 0) != payer_identity_id:
            conn.rollback()
            return JSONResponse({"ok": False, "error": "order_not_found"}, status_code=404, headers=route_headers())
        close_expired_wechat_pay_orders(conn=conn, out_trade_no=trade_no, limit=1)
        order = conn.execute("SELECT * FROM wechat_pay_orders WHERE out_trade_no = %s LIMIT 1", (trade_no,)).fetchone()
        if not order:
            return JSONResponse({"ok": False, "error": "order_not_found"}, status_code=404, headers=route_headers())
        conn.commit()
        provider_refreshed = False
        if _normalized_text(request.query_params.get("refresh")).lower() in {"1", "true", "yes", "on"}:
            try:
                provider_refreshed = True
                transaction = WeChatPayClient(_client_config()).query_order_by_out_trade_no(trade_no)
                order = _apply_transaction(conn, transaction, source_route=f"/api/h5/wechat-pay/orders/{trade_no}")
                conn.commit()
            except Exception as exc:
                conn.rollback()
                safe_log_exception(
                    LOGGER,
                    "wechat_pay_order_status_refresh_failed",
                    exc,
                    out_trade_no=trade_no,
                    event_type="transaction.paid",
                )
                return JSONResponse(
                    {"ok": False, "error": "wechat_pay_order_refresh_failed"},
                    status_code=502,
                    headers=route_headers(real_external_call_executed=True),
                )
        order_payload = dict(order)
        product_code = _normalized_text(order_payload.get("product_code"))
        completion_redirect = _completion_redirect_for_product_code(conn, product_code)
        lead_qr = {} if _completion_projection_blocks_lead_qr(completion_redirect) else _lead_qr_for_product_code(conn, product_code)
    return JSONResponse(
        {"ok": True, "order": _order_payload(order_payload, completion_redirect=completion_redirect, lead_qr=lead_qr)},
        headers=route_headers(real_external_call_executed=provider_refreshed),
    )


def notify_response(request: Request, body: bytes) -> JSONResponse:
    body_text = body.decode("utf-8")
    try:
        transaction = WeChatPayClient(_client_config()).verify_and_decrypt_notification(body=body_text, headers=dict(request.headers))
        if not production_data_ready():
            raise RuntimeError("production_database_required")
        with _connect() as conn:
            _apply_transaction(conn, transaction, source_route="/api/h5/wechat-pay/notify")
            conn.commit()
        return JSONResponse({"code": "SUCCESS", "message": "成功"})
    except Exception as exc:
        safe_log_exception(LOGGER, "wechat_pay_notify_failed", exc, event_type="transaction.paid")
        return JSONResponse({"code": "FAIL", "message": str(exc)}, status_code=401)
