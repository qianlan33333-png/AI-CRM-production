from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from aicrm_next.integration_ports import WeChatOAuthAdapter, build_wechat_oauth_adapter
from aicrm_next.platform.platform_foundation.audit_ledger import InMemoryAuditLedger
from aicrm_next.platform.shared.runtime import production_data_ready, production_environment
from aicrm_next.platform.shared.runtime_settings import runtime_bool, runtime_setting

from .dto import OAuthCallbackRequest, OAuthStartRequest


Json = dict[str, Any]
ADAPTER_MODES = {"fake", "sandbox", "real_blocked", "real_enabled"}
COOKIE_NAME = "questionnaire_h5_identity"
SOURCE_STATUS = "next_oauth_adapter"
STATE_TTL_SECONDS = 600
SOURCE_PARAM_FIELDS = ("source_channel", "campaign_id", "staff_id")

_AUDIT_LEDGER = InMemoryAuditLedger()
_DIAGNOSTICS: list[Json] = []
_USED_NONCES: set[str] = set()
RUNTIME_SETTING_KEYS = frozenset(
    {
        "AICRM_QUESTIONNAIRE_OAUTH_STATE_SECRET",
        "AICRM_NEXT_ACTION_TOKEN_SECRET",
        "SECRET_KEY",
        "AICRM_QUESTIONNAIRE_OAUTH_ADAPTER_MODE",
        "AICRM_QUESTIONNAIRE_OAUTH_ENABLE_REAL",
        "AICRM_QUESTIONNAIRE_OAUTH_REDIRECT_ALLOWLIST",
        "WECHAT_MP_APP_ID",
    }
)


def _now() -> int:
    return int(time.time())


def _b64encode(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _json_dumps(payload: Json) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _secret() -> str:
    return (
        runtime_setting("AICRM_QUESTIONNAIRE_OAUTH_STATE_SECRET")
        or runtime_setting("AICRM_NEXT_ACTION_TOKEN_SECRET")
        or runtime_setting("SECRET_KEY")
        or "aicrm-next-questionnaire-oauth-dev-secret"
    )


def _sign(message: str) -> str:
    return hmac.new(_secret().encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def _signed_blob(payload: Json) -> str:
    encoded = _b64encode(_json_dumps(payload).encode("utf-8"))
    return f"{encoded}.{_sign(encoded)}"


def _load_signed_blob(value: str) -> Json:
    try:
        encoded, signature = value.split(".", 1)
    except ValueError as exc:
        raise OAuthStateError("state_invalid", "state signature is missing") from exc
    if not hmac.compare_digest(_sign(encoded), signature):
        raise OAuthStateError("state_invalid", "state signature mismatch")
    try:
        payload = json.loads(_b64decode(encoded).decode("utf-8"))
    except Exception as exc:
        raise OAuthStateError("state_invalid", "state payload is invalid") from exc
    if not isinstance(payload, dict):
        raise OAuthStateError("state_invalid", "state payload is invalid")
    return payload


def _env_true(name: str) -> bool:
    return runtime_bool(name, False)


def resolve_adapter_mode() -> str:
    explicit = (
        runtime_setting(
            "AICRM_QUESTIONNAIRE_OAUTH_ADAPTER_MODE",
            "",
        )
        .strip()
        .lower()
    )
    if explicit in ADAPTER_MODES:
        if explicit == "real_enabled" and not _env_true("AICRM_QUESTIONNAIRE_OAUTH_ENABLE_REAL"):
            return "real_blocked"
        return explicit
    if production_data_ready() or production_environment():
        return "real_blocked"
    return "fake"


def _normalize_redirect(redirect: str | None, slug: str | None) -> str:
    target = (redirect or "").strip() or (f"/s/{slug}" if slug else "/")
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        allowed = {
            item.strip().rstrip("/")
            for item in runtime_setting(
                "AICRM_QUESTIONNAIRE_OAUTH_REDIRECT_ALLOWLIST",
                "",
            ).split(",")
            if item.strip()
        }
        origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        if origin not in allowed:
            raise OAuthStateError("redirect_not_allowed", "redirect target is not allowlisted")
        return target
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        raise OAuthStateError("redirect_not_allowed", "redirect target is not allowlisted")
    return target


def _source_params(payload: Json) -> dict[str, str]:
    source = payload.get("source") if isinstance(payload.get("source"), dict) else payload
    result: dict[str, str] = {}
    if not isinstance(source, dict):
        return result
    for key in SOURCE_PARAM_FIELDS:
        value = str(source.get(key) or "").strip()
        if value:
            result[key] = value
    return result


def _redirect_with_source_params(redirect: str, source: Json) -> str:
    params = _source_params(source)
    if not params:
        return redirect
    parts = urlsplit(redirect)
    existing = dict(parse_qsl(parts.query, keep_blank_values=True))
    existing.update(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(existing), parts.fragment))


def _safe_state_context(state: str | None) -> Json:
    if not state:
        return {}
    try:
        payload = _load_signed_blob(state)
    except Exception:
        return {}
    slug = str(payload.get("slug") or "").strip()
    try:
        redirect = _normalize_redirect(str(payload.get("redirect") or ""), slug)
    except OAuthStateError:
        redirect = f"/s/{slug}" if slug else "/"
    redirect = _redirect_with_source_params(redirect, payload)
    return {
        "slug": slug,
        "redirect_url": redirect,
        "browser_redirect": bool(payload.get("browser_redirect")),
    }


def questionnaire_oauth_state_context(state: str | None) -> Json:
    return _safe_state_context(state)


def questionnaire_h5_identity_from_cookies(cookies: Mapping[str, str]) -> Json:
    raw_cookie = str(cookies.get(COOKIE_NAME) or "").strip()
    if not raw_cookie:
        return {}
    try:
        payload = _load_signed_blob(raw_cookie)
    except Exception:
        return {}
    return {
        "openid": str(payload.get("openid") or "").strip(),
        "app_id": str(payload.get("app_id") or "").strip(),
        "customer_id": int(payload.get("customer_id") or 0),
        "respondent_identity_id": int(payload.get("respondent_identity_id") or 0),
        "unionid": str(payload.get("unionid") or "").strip(),
        "respondent_key": str(payload.get("respondent_key") or "").strip(),
        "external_userid": str(payload.get("external_userid") or "").strip(),
        "slug": str(payload.get("slug") or "").strip(),
        "anonymous": bool(payload.get("anonymous")),
    }


def build_questionnaire_h5_identity_cookie(identity: Mapping[str, Any]) -> str:
    payload = {
        "respondent_key": str(identity.get("respondent_key") or "").strip(),
        "openid": str(identity.get("openid") or "").strip(),
        "app_id": str(identity.get("app_id") or "").strip(),
        "customer_id": int(identity.get("customer_id") or 0),
        "respondent_identity_id": int(identity.get("respondent_identity_id") or 0),
        "unionid": str(identity.get("unionid") or "").strip(),
        "external_userid": str(identity.get("external_userid") or "").strip(),
        "slug": str(identity.get("slug") or "").strip(),
        "anonymous": bool(identity.get("anonymous")),
        "iat": _now(),
    }
    return _signed_blob(payload)


@dataclass(frozen=True)
class OAuthStateError(Exception):
    code: str
    message: str


def reset_questionnaire_oauth_state() -> None:
    _USED_NONCES.clear()
    _DIAGNOSTICS.clear()
    global _AUDIT_LEDGER
    _AUDIT_LEDGER = InMemoryAuditLedger()


def get_questionnaire_oauth_audit_events() -> list[Json]:
    return [event.to_dict() for event in _AUDIT_LEDGER.list_events()]


def get_questionnaire_oauth_diagnostics() -> list[Json]:
    return list(_DIAGNOSTICS)


class QuestionnaireOAuthAdapter:
    def __init__(self, adapter: WeChatOAuthAdapter | None = None, *, mode: str | None = None) -> None:
        self.mode = mode if mode in ADAPTER_MODES else resolve_adapter_mode()
        self._adapter = adapter or build_wechat_oauth_adapter()
        if self.mode == "real_enabled" and isinstance(self._adapter, WeChatOAuthAdapter):
            self._adapter.mode = "production"
            self._adapter.production_flag = "AICRM_QUESTIONNAIRE_OAUTH_ENABLE_REAL"

    def build_authorize_url(self, request: OAuthStartRequest) -> Json:
        try:
            redirect = _normalize_redirect(request.redirect, request.slug)
            state_payload = self._state_payload(request, redirect)
            state = _signed_blob(state_payload)
            callback_query = {"code": "fake-code", "state": state}
            if request.browser_redirect:
                callback_query["response_mode"] = "redirect"
            callback_url = f"/api/h5/wechat/oauth/callback?{urlencode(callback_query)}"
            result: Json = {
                "ok": True,
                "redirect_url": callback_url,
                "callback_url": callback_url,
                "state": state,
                "state_expires_at": state_payload["exp"],
                "nonce": state_payload["nonce"],
                "oauth_provider": "wechat_mp",
                "adapter_mode": self.mode,
                "source_status": SOURCE_STATUS,
                "route_owner": "ai_crm_next",
                "fallback_used": False,
                "real_external_call_executed": False,
                "redirect_allowed": True,
                "redirect_target": redirect,
                "external_call_blocked": self.mode == "real_blocked",
                "redirect_prepared": self.mode == "real_blocked",
                "browser_redirect": bool(request.browser_redirect),
            }
            if self.mode == "real_enabled":
                result.update(self._real_authorize_response(request, state, redirect))
            self._audit("questionnaire.oauth.start", result, target_id=request.slug or "")
            return result
        except OAuthStateError as exc:
            return self._error(exc.code, exc.message, event_type="questionnaire.oauth.start.error", target_id=request.slug or "")

    def exchange_code(self, request: OAuthCallbackRequest, state_payload: Json) -> Json:
        if self.mode == "real_blocked":
            return {
                "ok": False,
                "error": "external_call_blocked",
                "source_status": SOURCE_STATUS,
                "adapter_mode": self.mode,
                "real_external_call_executed": False,
            }
        if self.mode != "real_enabled":
            digest = hashlib.sha256(f"{state_payload.get('nonce', '')}:{request.code or 'fake-code'}".encode("utf-8")).hexdigest()[:16]
            return {
                "ok": True,
                "openid": request.openid or f"openid_fake_{digest}",
                "unionid": request.unionid or f"unionid_fake_{digest}",
                "external_userid": request.external_userid or "",
                "real_external_call_executed": False,
            }
        adapter_result = self._adapter.resolve_oauth_identity(
            state=request.state,
            redirect=str(state_payload.get("redirect") or ""),
            openid=request.openid,
            unionid=request.unionid,
            external_userid=request.external_userid,
            code=request.code,
        )
        result = adapter_result.get("result") if isinstance(adapter_result.get("result"), dict) else {}
        return {
            "ok": bool(adapter_result.get("ok")),
            "openid": result.get("openid", ""),
            "unionid": result.get("unionid", ""),
            "external_userid": result.get("external_userid", ""),
            "error": adapter_result.get("error_code", ""),
            "real_external_call_executed": bool(adapter_result.get("side_effect_executed")),
        }

    def fetch_user_identity(self, request: OAuthCallbackRequest, state_payload: Json) -> Json:
        return self.exchange_code(request, state_payload)

    def validate_state(self, state: str | None) -> Json:
        if not state:
            raise OAuthStateError("state_missing", "state is required")
        payload = _load_signed_blob(state)
        exp = int(payload.get("exp") or 0)
        nonce = str(payload.get("nonce") or "")
        if exp < _now():
            raise OAuthStateError("state_expired", "state has expired")
        if not nonce:
            raise OAuthStateError("state_invalid", "state nonce is missing")
        if nonce in _USED_NONCES:
            raise OAuthStateError("state_replayed", "state nonce has already been used")
        redirect = _normalize_redirect(str(payload.get("redirect") or ""), str(payload.get("slug") or ""))
        payload["redirect"] = redirect
        return payload

    def create_identity_session(self, identity: Json, state_payload: Json) -> tuple[Json, str]:
        customer_id = int(identity.get("customer_id") or 0)
        respondent_key = (
            f"customer:{customer_id}" if customer_id else identity.get("unionid") or identity.get("openid") or identity.get("external_userid") or ""
        )
        session = {
            "respondent_key": respondent_key,
            "openid": identity.get("openid", ""),
            "app_id": identity.get("app_id", ""),
            "customer_id": customer_id,
            "respondent_identity_id": int(identity.get("respondent_identity_id") or 0),
            "unionid": identity.get("unionid", ""),
            "external_userid": identity.get("external_userid", ""),
            "slug": state_payload.get("slug", ""),
            "nonce": state_payload.get("nonce", ""),
            "iat": _now(),
        }
        return session, _signed_blob(session)

    def callback(self, request: OAuthCallbackRequest) -> Json:
        try:
            if request.error or request.errcode:
                raise OAuthStateError("oauth_provider_error", request.error or request.errcode or "oauth provider error")
            state_payload = self.validate_state(request.state)
            identity = self.fetch_user_identity(request, state_payload)
            if not identity.get("ok"):
                self._record_diagnostic("external_call_blocked", request, state_payload)
                return self._error(
                    str(identity.get("error") or "external_call_blocked"),
                    "OAuth external call is blocked by adapter mode",
                    event_type="questionnaire.oauth.callback.blocked",
                    target_id=str(state_payload.get("slug") or ""),
                    status_code=200,
                )
            from aicrm_next.crm.identity_contact.application import ResolvePersonIdentityQuery
            from aicrm_next.crm.identity_contact.wechat_unionid_guard import resolve_oauth_unionid

            canonical_unionid = resolve_oauth_unionid(
                identity,
                identity_query=ResolvePersonIdentityQuery(),
            )
            if not canonical_unionid:
                self._record_diagnostic("unionid_required", request, state_payload)
                return self._error(
                    "unionid_required",
                    "WeChat OAuth did not provide a canonical UnionID",
                    event_type="questionnaire.oauth.callback.identity_missing",
                    target_id=str(state_payload.get("slug") or ""),
                    status_code=409,
                )
            identity["unionid"] = canonical_unionid
            app_id = runtime_setting("WECHAT_MP_APP_ID").strip() or ("wechat_mp_test_app" if self.mode in {"fake", "sandbox"} else "")
            openid = str(identity.get("openid") or "").strip()
            if not app_id or not openid:
                self._record_diagnostic("openid_required", request, state_payload)
                return self._error(
                    "openid_required",
                    "WeChat OAuth did not provide an app-scoped OpenID",
                    event_type="questionnaire.oauth.callback.identity_missing",
                    target_id=str(state_payload.get("slug") or ""),
                    status_code=409,
                )
            identity["app_id"] = app_id
            if production_data_ready():
                from aicrm_next.crm.identity_contact.oneid_repository import PostgresOneIDService

                resolution = PostgresOneIDService().ensure_verified_wechat_identity(
                    app_id=app_id,
                    openid=openid,
                    unionid=str(identity.get("unionid") or "").strip(),
                    source_type="questionnaire_wechat_oauth",
                    source_event_id=str(request.code or "").strip(),
                )
                if resolution.get("status") == "conflict":
                    self._record_diagnostic("identity_conflict", request, state_payload)
                    return self._error(
                        "identity_conflict",
                        "WeChat identity conflicts with an existing customer",
                        event_type="questionnaire.oauth.callback.identity_conflict",
                        target_id=str(state_payload.get("slug") or ""),
                        status_code=409,
                    )
                identity["customer_id"] = int(resolution.get("customer_id") or 0)
                identity["respondent_identity_id"] = int(resolution.get("openid_identity_id") or resolution.get("identity_id") or 0)
            _USED_NONCES.add(str(state_payload["nonce"]))
            session_identity, signed_cookie = self.create_identity_session(identity, state_payload)
            result = {
                "ok": True,
                "redirect_url": _redirect_with_source_params(
                    str(state_payload.get("redirect") or f"/s/{state_payload.get('slug', '')}"),
                    state_payload,
                ),
                "slug": state_payload.get("slug", ""),
                "identity": session_identity,
                "session_cookie_name": COOKIE_NAME,
                "session_cookie": signed_cookie,
                "adapter_mode": self.mode,
                "source_status": SOURCE_STATUS,
                "route_owner": "ai_crm_next",
                "fallback_used": False,
                "real_external_call_executed": bool(identity.get("real_external_call_executed")),
                "audit_recorded": True,
                "browser_redirect": bool(state_payload.get("browser_redirect")),
            }
            self._audit("questionnaire.oauth.callback", result, target_id=str(state_payload.get("slug") or ""))
            return result
        except OAuthStateError as exc:
            self._record_diagnostic(exc.code, request, {})
            return self._error(exc.code, exc.message, event_type="questionnaire.oauth.callback.error")

    def _state_payload(self, request: OAuthStartRequest, redirect: str) -> Json:
        now = _now()
        return {
            "slug": (request.slug or request.state or "hxc-activation-v1").strip(),
            "redirect": redirect,
            "scene": request.scene or "",
            "browser_redirect": bool(request.browser_redirect),
            "source": _source_params(
                {
                    "source_channel": request.source_channel,
                    "campaign_id": request.campaign_id,
                    "staff_id": request.staff_id,
                }
            ),
            "nonce": secrets.token_urlsafe(16),
            "iat": now,
            "exp": now + STATE_TTL_SECONDS,
            "adapter_mode": self.mode,
        }

    def _real_authorize_response(self, request: OAuthStartRequest, state: str, redirect: str) -> Json:
        adapter_result = self._adapter.build_authorize_url(
            slug=request.slug,
            state=state,
            redirect="/api/h5/wechat/oauth/callback",
            openid=request.openid,
            unionid=request.unionid,
            external_userid=request.external_userid,
        )
        result = adapter_result.get("result") if isinstance(adapter_result.get("result"), dict) else {}
        return {
            "ok": bool(adapter_result.get("ok")),
            "redirect_url": result.get("redirect_url", ""),
            "real_external_call_executed": bool(adapter_result.get("side_effect_executed")),
            "external_call_blocked": not bool(adapter_result.get("ok")),
            "error": adapter_result.get("error_code", ""),
        }

    def _audit(self, event_type: str, payload: Json, *, target_id: str = "") -> None:
        safe_payload = {key: value for key, value in payload.items() if key not in {"session_cookie"}}
        _AUDIT_LEDGER.record_event(
            event_type=event_type,
            actor_id="questionnaire_h5_oauth",
            actor_type="system",
            target_type="questionnaire",
            target_id=target_id,
            source_route="/api/h5/wechat/oauth",
            payload=safe_payload,
        )

    def _record_diagnostic(self, code: str, request: OAuthCallbackRequest, state_payload: Json) -> None:
        _DIAGNOSTICS.append(
            {
                "diagnostic_type": "questionnaire.oauth.callback",
                "error_code": code,
                "slug": state_payload.get("slug", ""),
                "state_present": bool(request.state),
                "code_present": bool(request.code),
                "adapter_mode": self.mode,
                "source_status": SOURCE_STATUS,
            }
        )

    def _error(
        self,
        code: str,
        message: str,
        *,
        event_type: str,
        target_id: str = "",
        status_code: int = 400,
    ) -> Json:
        payload = {
            "ok": False,
            "error": code,
            "message": message,
            "source_status": "state_error" if code.startswith("state_") or code == "redirect_not_allowed" else SOURCE_STATUS,
            "adapter_mode": self.mode,
            "route_owner": "ai_crm_next",
            "fallback_used": False,
            "real_external_call_executed": False,
            "status_code": status_code,
            "audit_recorded": True,
        }
        self._audit(event_type, payload, target_id=target_id)
        return payload


def build_questionnaire_oauth_adapter(*, mode: str | None = None) -> QuestionnaireOAuthAdapter:
    return QuestionnaireOAuthAdapter(mode=mode)
