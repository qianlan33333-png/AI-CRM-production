from __future__ import annotations

import hashlib
from typing import Any, Callable
from urllib.parse import quote, urlencode, urlparse

from aicrm_next.platform.shared.runtime import production_environment
from aicrm_next.platform.shared.runtime_settings import (
    environment_fallback,
    managed_runtime_setting,
    runtime_bool,
)

from .audit import record_audit_event
from .idempotency import get_or_create, make_idempotency_key
from .questionnaire_contracts import AdapterMode, Json
from .wechat_oauth_client import WeChatOAuthClientError, build_wechat_oauth_client


VALID_MODES = {"fake", "disabled", "staging", "production"}
RUNTIME_SETTING_KEYS = frozenset(
    {
        "AICRM_NEXT_ENABLE_REAL_QUESTIONNAIRE_WEBHOOK",
        "AICRM_NEXT_ENABLE_REAL_WECHAT_OAUTH",
        "AICRM_NEXT_ENABLE_REAL_WECOM_TAG",
        "AICRM_NEXT_QUESTIONNAIRE_WEBHOOK_MODE",
        "AICRM_NEXT_WECHAT_OAUTH_MODE",
        "AICRM_NEXT_WECOM_TAG_MODE",
        "AICRM_PUBLIC_BASE_URL",
        "APP_EXTERNAL_BASE_URL",
        "EXTERNAL_BASE_URL",
        "NEXT_PUBLIC_BASE_URL",
        "PUBLIC_BASE_URL",
        "WECHAT_MP_APP_ID",
        "WECHAT_MP_APP_SECRET",
        "WECHAT_MP_OAUTH_SCOPE",
        "WECHAT_PAY_NOTIFY_URL",
    }
)


def _normalise_mode(value: str | None, *, default: AdapterMode = "fake") -> AdapterMode:
    mode = (value or default).strip().lower()
    if mode not in VALID_MODES:
        return default
    return mode  # type: ignore[return-value]


def _env_true(name: str) -> bool:
    return runtime_bool(name)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_target(target: dict[str, Any]) -> dict[str, Any]:
    forbidden = {"secret", "token", "access_token", "client_secret", "app_secret"}
    return {key: value for key, value in target.items() if key.lower() not in forbidden}


def _base_result(
    *,
    ok: bool,
    adapter: str,
    mode: AdapterMode,
    operation: str,
    idempotency_key: str,
    target: dict[str, Any],
    result: dict[str, Any] | None,
    audit_id: str,
    side_effect_executed: bool = False,
    error_code: str = "",
    error_message: str = "",
) -> Json:
    return {
        "ok": ok,
        "adapter": adapter,
        "mode": mode,
        "operation": operation,
        "idempotency_key": idempotency_key,
        "target": _safe_target(target),
        "result": result or {},
        "audit_id": audit_id,
        "side_effect_executed": side_effect_executed,
        "error_code": error_code,
        "error_message": error_message,
    }


class _GuardedQuestionnaireAdapter:
    adapter_name = "QuestionnaireAdapter"
    production_flag = ""

    def __init__(self, mode: AdapterMode | str = "fake") -> None:
        self.mode = _normalise_mode(str(mode), default="fake")

    def _guarded_result(self, *, operation: str, idempotency_key: str, target: dict[str, Any]) -> Json | None:
        if self.mode == "disabled":
            audit = record_audit_event(
                adapter=self.adapter_name,
                operation=operation,
                mode=self.mode,
                idempotency_key=idempotency_key,
                side_effect_executed=False,
                status="blocked",
                error_code="adapter_disabled",
            )
            return _base_result(
                ok=False,
                adapter=self.adapter_name,
                mode=self.mode,
                operation=operation,
                idempotency_key=idempotency_key,
                target=target,
                result={},
                audit_id=audit["audit_id"],
                error_code="adapter_disabled",
                error_message=f"{self.adapter_name} is disabled",
            )
        if self.mode == "production":
            error_code = "production_guard_failed" if not _env_true(self.production_flag) else "production_not_implemented"
            audit = record_audit_event(
                adapter=self.adapter_name,
                operation=operation,
                mode=self.mode,
                idempotency_key=idempotency_key,
                side_effect_executed=False,
                status="blocked",
                error_code=error_code,
            )
            return _base_result(
                ok=False,
                adapter=self.adapter_name,
                mode=self.mode,
                operation=operation,
                idempotency_key=idempotency_key,
                target=target,
                result={},
                audit_id=audit["audit_id"],
                error_code=error_code,
                error_message=f"{self.adapter_name} production mode is not enabled for real outbound calls",
            )
        return None

    def _successful_result(self, *, operation: str, idempotency_key: str, target: dict[str, Any], factory) -> Json:
        cached = get_or_create(idempotency_key, factory)
        audit = record_audit_event(
            adapter=self.adapter_name,
            operation=operation,
            mode=self.mode,
            idempotency_key=idempotency_key,
            side_effect_executed=False,
            status="ok",
        )
        return _base_result(
            ok=True,
            adapter=self.adapter_name,
            mode=self.mode,
            operation=operation,
            idempotency_key=idempotency_key,
            target=target,
            result=cached,
            audit_id=audit["audit_id"],
        )


class WeChatOAuthAdapter(_GuardedQuestionnaireAdapter):
    adapter_name = "WeChatOAuthAdapter"
    production_flag = "AICRM_NEXT_ENABLE_REAL_WECHAT_OAUTH"

    def __init__(self, mode: AdapterMode | str = "fake", oauth_client_factory: Callable[[], Any] | None = None) -> None:
        super().__init__(mode)
        self._oauth_client_factory = oauth_client_factory

    def build_authorize_url(
        self,
        *,
        slug: str | None = None,
        state: str | None = None,
        redirect: str | None = None,
        openid: str | None = None,
        unionid: str | None = None,
        external_userid: str | None = None,
        idempotency_key: str | None = None,
    ) -> Json:
        operation = "build_authorize_url"
        resolved_state = (state or slug or "questionnaire_fake_state").strip()
        target = {"state": resolved_state, "slug": slug or "", "openid": openid or "", "unionid": unionid or "", "external_userid": external_userid or ""}
        key = idempotency_key or make_idempotency_key(operation=operation, payload=target)
        if self.mode == "production":
            guarded = self._production_guarded_result(operation=operation, idempotency_key=key, target=target)
            if guarded:
                return guarded
            return self._real_authorize_url_result(operation=operation, idempotency_key=key, target=target, state=resolved_state, redirect=redirect)
        guarded = self._guarded_result(operation=operation, idempotency_key=key, target=target)
        if guarded:
            return guarded
        return self._successful_result(operation=operation, idempotency_key=key, target=target, factory=lambda: self._fake_authorize_url(resolved_state, redirect, openid, unionid, external_userid))

    def exchange_code(self, *, code: str, state: str | None = None, redirect: str | None = None, idempotency_key: str | None = None) -> Json:
        operation = "exchange_code"
        target = {"state": state or "", "code_hash": _digest(code or "")[:12], "redirect": redirect or ""}
        key = idempotency_key or make_idempotency_key(operation=operation, payload=target)
        if self.mode == "production":
            guarded = self._production_guarded_result(operation=operation, idempotency_key=key, target=target)
            if guarded:
                return guarded
            return self._real_identity_result(operation=operation, idempotency_key=key, target=target, state=state, redirect=redirect, openid=None, unionid=None, external_userid=None, code=code)
        guarded = self._guarded_result(operation=operation, idempotency_key=key, target=target)
        if guarded:
            return guarded
        digest = _digest(key)[:16]
        return self._successful_result(
            operation=operation,
            idempotency_key=key,
            target=target,
            factory=lambda: {"openid": f"openid_fake_{digest}", "unionid": f"unionid_fake_{digest}", "source_status": "fake", "redirect_url": redirect or (f"/s/{state}" if state else "/")},
        )

    def fetch_userinfo(self, *, openid: str, unionid: str | None = None, idempotency_key: str | None = None) -> Json:
        operation = "fetch_userinfo"
        target = {"openid": openid, "unionid": unionid or ""}
        key = idempotency_key or make_idempotency_key(operation=operation, payload=target)
        if self.mode == "production":
            guarded = self._production_guarded_result(operation=operation, idempotency_key=key, target=target)
            if guarded:
                return guarded
            return self._adapter_result(
                ok=False,
                operation=operation,
                idempotency_key=key,
                target=target,
                error_code="access_token_required",
                error_message="WeChat userinfo requires an OAuth access token in production mode",
            )
        guarded = self._guarded_result(operation=operation, idempotency_key=key, target=target)
        if guarded:
            return guarded
        digest = _digest(key)[:12]
        return self._successful_result(operation=operation, idempotency_key=key, target=target, factory=lambda: {"openid": openid, "unionid": unionid or f"unionid_fake_{digest}", "nickname": f"fake_user_{digest}", "source_status": "fake"})

    def resolve_oauth_identity(
        self,
        *,
        state: str | None = None,
        redirect: str | None = None,
        openid: str | None = None,
        unionid: str | None = None,
        external_userid: str | None = None,
        code: str | None = None,
        idempotency_key: str | None = None,
    ) -> Json:
        operation = "resolve_oauth_identity"
        target = {"state": state or "", "openid": openid or "", "unionid": unionid or "", "external_userid": external_userid or "", "code_hash": _digest(code or "")[:12] if code else ""}
        key = idempotency_key or make_idempotency_key(operation=operation, payload=target)
        if self.mode == "production":
            guarded = self._production_guarded_result(operation=operation, idempotency_key=key, target=target)
            if guarded:
                return guarded
            return self._real_identity_result(operation=operation, idempotency_key=key, target=target, state=state, redirect=redirect, openid=openid, unionid=unionid, external_userid=external_userid, code=code)
        guarded = self._guarded_result(operation=operation, idempotency_key=key, target=target)
        if guarded:
            return guarded
        return self._successful_result(operation=operation, idempotency_key=key, target=target, factory=lambda: self._fake_identity(state, redirect, openid, unionid, external_userid, code))

    def _production_guarded_result(self, *, operation: str, idempotency_key: str, target: dict[str, Any]) -> Json | None:
        if _env_true(self.production_flag):
            return None
        return self._adapter_result(
            ok=False,
            operation=operation,
            idempotency_key=idempotency_key,
            target=target,
            error_code="production_guard_failed",
            error_message=f"{self.adapter_name} production mode is not enabled for real outbound calls",
        )

    def _real_authorize_url_result(self, *, operation: str, idempotency_key: str, target: dict[str, Any], state: str, redirect: str | None) -> Json:
        app_id = self._wechat_app_id()
        if not app_id:
            return self._adapter_result(
                ok=False,
                operation=operation,
                idempotency_key=idempotency_key,
                target=target,
                error_code="wechat_oauth_not_configured",
                error_message="WECHAT_MP_APP_ID is required for real WeChat OAuth",
            )
        redirect_uri = self._absolute_redirect_uri(redirect)
        query = urlencode(
            {
                "appid": app_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": self._oauth_scope(),
                "state": state,
            }
        )
        result = {
            "redirect_url": f"https://open.weixin.qq.com/connect/oauth2/authorize?{query}#wechat_redirect",
            "state": state,
            "source_status": "production",
            "oauth_provider": "wechat_mp",
        }
        return self._adapter_result(ok=True, operation=operation, idempotency_key=idempotency_key, target=target, result=result)

    def _oauth_client(self) -> Any:
        return self._oauth_client_factory() if self._oauth_client_factory else build_wechat_oauth_client()

    def _real_identity_result(
        self,
        *,
        operation: str,
        idempotency_key: str,
        target: dict[str, Any],
        state: str | None,
        redirect: str | None,
        openid: str | None,
        unionid: str | None,
        external_userid: str | None,
        code: str | None,
    ) -> Json:
        if not code:
            return self._adapter_result(
                ok=False,
                operation=operation,
                idempotency_key=idempotency_key,
                target=target,
                error_code="oauth_code_required",
                error_message="WeChat OAuth callback code is required in production mode",
            )
        app_id = self._wechat_app_id()
        app_secret = self._wechat_app_secret()
        if not app_id or not app_secret:
            return self._adapter_result(
                ok=False,
                operation=operation,
                idempotency_key=idempotency_key,
                target=target,
                error_code="wechat_oauth_not_configured",
                error_message="WECHAT_MP_APP_ID and WECHAT_MP_APP_SECRET are required for real WeChat OAuth",
            )
        try:
            client = self._oauth_client()
            exchange_payload = client.exchange_code(app_id=app_id, app_secret=app_secret, code=code)
            if self._wechat_error_code(exchange_payload):
                return self._wechat_payload_error(operation=operation, idempotency_key=idempotency_key, target=target, payload=exchange_payload)
            # Production identity is provider evidence only. Callback query
            # parameters are hints for fixture modes and must never fill a
            # missing OpenID/UnionID in a real OAuth exchange.
            resolved_openid = str(exchange_payload.get("openid") or "").strip()
            resolved_unionid = str(exchange_payload.get("unionid") or "").strip()
            access_token = str(exchange_payload.get("access_token") or "").strip()
            if not resolved_openid:
                return self._wechat_payload_error(operation=operation, idempotency_key=idempotency_key, target=target, payload=exchange_payload)
            if not resolved_unionid and access_token and self._oauth_scope() == "snsapi_userinfo":
                userinfo_payload = client.fetch_userinfo(access_token=access_token, openid=resolved_openid)
                if self._wechat_error_code(userinfo_payload):
                    return self._wechat_payload_error(operation=operation, idempotency_key=idempotency_key, target=target, payload=userinfo_payload)
                resolved_unionid = str(userinfo_payload.get("unionid") or "").strip()
        except Exception as exc:
            error_message = exc.message if isinstance(exc, WeChatOAuthClientError) else "WeChat OAuth exchange failed"
            sensitive_values = (code, app_secret, locals().get("access_token", ""))
            if any(value and str(value) in error_message for value in sensitive_values):
                error_message = "WeChat OAuth exchange failed"
            return self._adapter_result(
                ok=False,
                operation=operation,
                idempotency_key=idempotency_key,
                target=target,
                side_effect_executed=True,
                error_code="wechat_oauth_exchange_failed",
                error_message=error_message,
            )
        result = {
            "openid": resolved_openid,
            "unionid": resolved_unionid,
            "external_userid": "",
            "redirect_url": redirect or (f"/s/{state}" if state else "/"),
            "state": (state or "").strip(),
            "source_status": "production",
            "oauth_provider": "wechat_mp",
        }
        return self._adapter_result(ok=True, operation=operation, idempotency_key=idempotency_key, target=target, result=result, side_effect_executed=True)

    def _wechat_payload_error(self, *, operation: str, idempotency_key: str, target: dict[str, Any], payload: dict[str, Any]) -> Json:
        return self._adapter_result(
            ok=False,
            operation=operation,
            idempotency_key=idempotency_key,
            target=target,
            side_effect_executed=True,
            error_code="wechat_oauth_exchange_failed",
            error_message=str(payload.get("errmsg") or payload.get("errcode") or "invalid WeChat OAuth payload"),
        )

    def _adapter_result(
        self,
        *,
        ok: bool,
        operation: str,
        idempotency_key: str,
        target: dict[str, Any],
        result: dict[str, Any] | None = None,
        side_effect_executed: bool = False,
        error_code: str = "",
        error_message: str = "",
    ) -> Json:
        audit = record_audit_event(
            adapter=self.adapter_name,
            operation=operation,
            mode=self.mode,
            idempotency_key=idempotency_key,
            side_effect_executed=side_effect_executed,
            status="ok" if ok else "blocked",
            error_code=error_code,
        )
        return _base_result(
            ok=ok,
            adapter=self.adapter_name,
            mode=self.mode,
            operation=operation,
            idempotency_key=idempotency_key,
            target=target,
            result=result or {},
            audit_id=audit["audit_id"],
            side_effect_executed=side_effect_executed,
            error_code=error_code,
            error_message=error_message,
        )

    @staticmethod
    def _wechat_error_code(payload: dict[str, Any]) -> bool:
        errcode = payload.get("errcode")
        if errcode in (None, "", 0, "0"):
            return False
        return True

    @staticmethod
    def _wechat_app_id() -> str:
        return managed_runtime_setting("WECHAT_MP_APP_ID")

    @staticmethod
    def _wechat_app_secret() -> str:
        return managed_runtime_setting("WECHAT_MP_APP_SECRET")

    @staticmethod
    def _oauth_scope() -> str:
        return managed_runtime_setting("WECHAT_MP_OAUTH_SCOPE", "snsapi_userinfo").strip() or "snsapi_userinfo"

    @classmethod
    def _absolute_redirect_uri(cls, redirect: str | None) -> str:
        value = str(redirect or "").strip()
        if value.startswith(("http://", "https://")):
            return value
        if not value.startswith("/"):
            value = "/" + value
        return cls._public_base_url() + value

    @staticmethod
    def _public_base_url() -> str:
        production = production_environment()
        for key in (
            "AICRM_PUBLIC_BASE_URL",
            "PUBLIC_BASE_URL",
            "EXTERNAL_BASE_URL",
            "APP_EXTERNAL_BASE_URL",
            "NEXT_PUBLIC_BASE_URL",
        ):
            value = environment_fallback(key).strip().rstrip("/")
            if value:
                if production and "localhost" in value:
                    continue
                return value
        notify_url = managed_runtime_setting("WECHAT_PAY_NOTIFY_URL").strip()
        parsed = urlparse(notify_url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            candidate = f"{parsed.scheme}://{parsed.netloc}"
            if not production or "localhost" not in candidate:
                return candidate
        if production:
            return "https://www.youcangogogo.com"
        return "http://localhost"

    def _fake_authorize_url(self, state: str, redirect: str | None, openid: str | None, unionid: str | None, external_userid: str | None) -> Json:
        redirect_url = redirect or f"/api/h5/wechat/oauth/callback?state={quote(state)}"
        query = []
        if openid:
            query.append(f"openid={quote(openid)}")
        if unionid:
            query.append(f"unionid={quote(unionid)}")
        if external_userid:
            query.append(f"external_userid={quote(external_userid)}")
        fake_redirect_url = redirect_url + (("&" if "?" in redirect_url else "?") + "&".join(query) if query else "")
        source_status = "staging_fake" if self.mode == "staging" else "fake"
        return {"redirect_url": fake_redirect_url, "state": state, "source_status": source_status, "oauth_provider": "wechat_mp"}

    def _fake_identity(self, state: str | None, redirect: str | None, openid: str | None, unionid: str | None, external_userid: str | None, code: str | None) -> Json:
        digest = _digest(code or state or "questionnaire")[:12]
        resolved_state = (state or "").strip()
        return {
            "openid": openid or f"openid_fake_{digest}",
            "unionid": unionid or f"unionid_fake_{digest}",
            "external_userid": external_userid or "",
            "redirect_url": redirect or (f"/s/{resolved_state}" if resolved_state else "/"),
            "state": resolved_state,
            "source_status": ("staging_fake" if self.mode == "staging" else "fake") if resolved_state else "missing_config",
        }


class WeComTagAdapter(_GuardedQuestionnaireAdapter):
    adapter_name = "WeComTagAdapter"
    production_flag = "AICRM_NEXT_ENABLE_REAL_WECOM_TAG"

    def mark_external_contact_tags(self, *, external_userid: str, tag_ids: list[str], questionnaire_id: int | str | None = None, submission_id: str | None = None, idempotency_key: str | None = None) -> Json:
        return self._tag_operation("mark_external_contact_tags", external_userid=external_userid, tag_ids=tag_ids, questionnaire_id=questionnaire_id, submission_id=submission_id, idempotency_key=idempotency_key)

    def unmark_external_contact_tags(self, *, external_userid: str, tag_ids: list[str], questionnaire_id: int | str | None = None, submission_id: str | None = None, idempotency_key: str | None = None) -> Json:
        return self._tag_operation("unmark_external_contact_tags", external_userid=external_userid, tag_ids=tag_ids, questionnaire_id=questionnaire_id, submission_id=submission_id, idempotency_key=idempotency_key)

    def validate_tag_ids(self, *, tag_ids: list[str], idempotency_key: str | None = None) -> Json:
        operation = "validate_tag_ids"
        target = {"tag_ids": sorted(tag_ids)}
        key = idempotency_key or make_idempotency_key(operation=operation, payload=target)
        guarded = self._guarded_result(operation=operation, idempotency_key=key, target=target)
        if guarded:
            return guarded
        source_status = "staging_fake" if self.mode == "staging" else "fake"
        return self._successful_result(operation=operation, idempotency_key=key, target=target, factory=lambda: {"valid": True, "invalid_tag_ids": [], "tag_ids": sorted(tag_ids), "source_status": source_status})

    def build_tag_operation_preview(self, *, external_userid: str, tag_ids: list[str], operation: str = "mark", idempotency_key: str | None = None) -> Json:
        op = "build_tag_operation_preview"
        target = {"external_userid": external_userid, "tag_ids": sorted(tag_ids), "operation": operation}
        key = idempotency_key or make_idempotency_key(operation=op, payload=target)
        guarded = self._guarded_result(operation=op, idempotency_key=key, target=target)
        if guarded:
            return guarded
        source_status = "staging_fake_preview" if self.mode == "staging" else "fake_preview"
        return self._successful_result(operation=op, idempotency_key=key, target=target, factory=lambda: {"operation": operation, "tag_ids": sorted(tag_ids), "source_status": source_status})

    def _tag_operation(self, operation: str, *, external_userid: str, tag_ids: list[str], questionnaire_id: int | str | None, submission_id: str | None, idempotency_key: str | None) -> Json:
        target = {"external_userid": external_userid, "tag_ids": sorted(tag_ids), "questionnaire_id": questionnaire_id or "", "submission_id": submission_id or ""}
        key = idempotency_key or make_idempotency_key(operation=operation, payload=target)
        guarded = self._guarded_result(operation=operation, idempotency_key=key, target=target)
        if guarded:
            return guarded
        digest = _digest(key)[:16]
        mode_prefix = "staging" if self.mode == "staging" else "fake"
        return self._successful_result(operation=operation, idempotency_key=key, target=target, factory=lambda: {"operation_id": f"{mode_prefix}_tag_op_{digest}", "tag_ids": sorted(tag_ids), "source_status": f"{mode_prefix}", "applied": False})


class QuestionnaireExternalPushAdapter(_GuardedQuestionnaireAdapter):
    adapter_name = "QuestionnaireExternalPushAdapter"
    production_flag = "AICRM_NEXT_ENABLE_REAL_QUESTIONNAIRE_WEBHOOK"

    def push_submission_event(self, *, questionnaire_id: int | str, submission_id: str, webhook_url: str | None = None, payload_summary: dict[str, Any] | None = None, idempotency_key: str | None = None) -> Json:
        return self._push_operation("push_submission_event", questionnaire_id=questionnaire_id, submission_id=submission_id, webhook_url=webhook_url, payload_summary=payload_summary, idempotency_key=idempotency_key)

    def push_score_result_event(self, *, questionnaire_id: int | str, submission_id: str, score: int, webhook_url: str | None = None, payload_summary: dict[str, Any] | None = None, idempotency_key: str | None = None) -> Json:
        merged = {**(payload_summary or {}), "score": score}
        return self._push_operation("push_score_result_event", questionnaire_id=questionnaire_id, submission_id=submission_id, webhook_url=webhook_url, payload_summary=merged, idempotency_key=idempotency_key)

    def retry_push_event(self, *, event_id: str, webhook_url: str | None = None, idempotency_key: str | None = None) -> Json:
        operation = "retry_push_event"
        target = {"event_id": event_id, "webhook_url": webhook_url or ""}
        key = idempotency_key or make_idempotency_key(operation=operation, payload=target)
        guarded = self._guarded_result(operation=operation, idempotency_key=key, target=target)
        if guarded:
            return guarded
        mode_prefix = "staging" if self.mode == "staging" else "fake"
        return self._successful_result(operation=operation, idempotency_key=key, target=target, factory=lambda: {"push_id": f"{mode_prefix}_retry_{_digest(key)[:16]}", "source_status": mode_prefix, "delivered": False})

    def build_push_preview(self, *, questionnaire_id: int | str, webhook_url: str | None = None, payload_summary: dict[str, Any] | None = None, idempotency_key: str | None = None) -> Json:
        operation = "build_push_preview"
        target = {"questionnaire_id": questionnaire_id, "webhook_url": webhook_url or "", "payload_summary": payload_summary or {}}
        key = idempotency_key or make_idempotency_key(operation=operation, payload=target)
        guarded = self._guarded_result(operation=operation, idempotency_key=key, target=target)
        if guarded:
            return guarded
        mode_prefix = "staging" if self.mode == "staging" else "fake"
        return self._successful_result(operation=operation, idempotency_key=key, target=target, factory=lambda: {"preview_id": f"{mode_prefix}_push_preview_{_digest(key)[:16]}", "source_status": f"{mode_prefix}_preview"})

    def _push_operation(self, operation: str, *, questionnaire_id: int | str, submission_id: str, webhook_url: str | None, payload_summary: dict[str, Any] | None, idempotency_key: str | None) -> Json:
        target = {"questionnaire_id": questionnaire_id, "submission_id": submission_id, "webhook_url": webhook_url or "", "payload_summary": payload_summary or {}}
        key = idempotency_key or make_idempotency_key(operation=operation, payload=target)
        guarded = self._guarded_result(operation=operation, idempotency_key=key, target=target)
        if guarded:
            return guarded
        mode_prefix = "staging" if self.mode == "staging" else "fake"
        return self._successful_result(operation=operation, idempotency_key=key, target=target, factory=lambda: {"push_id": f"{mode_prefix}_push_{_digest(key)[:16]}", "source_status": mode_prefix, "delivered": False})


class QuestionnaireSubmitSideEffectGateway:
    adapter_name = "QuestionnaireSubmitSideEffectGateway"

    def __init__(
        self,
        *,
        tag_adapter: WeComTagAdapter | None = None,
        push_adapter: QuestionnaireExternalPushAdapter | None = None,
        local_tag_projector: Callable[..., Json] | None = None,
        tag_mutation_executor: Callable[..., Json] | None = None,
        mobile_binder: Callable[..., Json] | None = None,
    ) -> None:
        self._tag_adapter = tag_adapter or build_wecom_tag_adapter()
        self._push_adapter = push_adapter or build_questionnaire_external_push_adapter()
        self._local_tag_projector = local_tag_projector
        self._tag_mutation_executor = tag_mutation_executor
        self._mobile_binder = mobile_binder

    def apply_tags(
        self,
        *,
        questionnaire_id: int | str,
        submission_id: str,
        external_userid: str,
        tag_ids: list[str],
        unionid: str = "",
        follow_user_userid: str = "",
    ) -> Json:
        projection_key = make_idempotency_key(
            operation="questionnaire.tag.local_projection",
            payload={
                "questionnaire_id": questionnaire_id,
                "submission_id": submission_id,
                "unionid": unionid,
                "external_userid": external_userid,
                "tag_ids": sorted(tag_ids),
            },
        )
        local_projection = (
            self._local_tag_projector(
                unionid=unionid,
                external_userid=external_userid,
                owner_userid=follow_user_userid,
                tag_ids=tag_ids,
                source="questionnaire_submit_pipeline",
                questionnaire_id=questionnaire_id,
                submission_id=submission_id,
                idempotency_key=projection_key,
            )
            if self._local_tag_projector
            else {
                "ok": False,
                "local_projection_updated": False,
                "source_status": "composition_unavailable",
            }
        )
        if not tag_ids:
            return self.record_side_effect_audit(
                operation="apply_tags",
                target={"questionnaire_id": questionnaire_id, "submission_id": submission_id, "external_userid": external_userid, "tag_ids": tag_ids},
                result={
                    "skipped": True,
                    "reason": "missing_tags",
                    "local_projection": local_projection,
                    "local_projection_updated": bool(local_projection.get("local_projection_updated")),
                },
            )
        if not external_userid:
            return self.record_side_effect_audit(
                operation="apply_tags",
                target={"questionnaire_id": questionnaire_id, "submission_id": submission_id, "external_userid": external_userid, "tag_ids": tag_ids},
                result={
                    "skipped": False,
                    "reason": "identity_external_userid_missing",
                    "external_effect_status": "blocked",
                    "local_projection": local_projection,
                    "local_projection_updated": bool(local_projection.get("local_projection_updated")),
                },
            )
        if self._tag_mutation_executor is None:
            return self.record_side_effect_audit(
                operation="apply_tags",
                target={
                    "questionnaire_id": questionnaire_id,
                    "submission_id": submission_id,
                    "external_userid": external_userid,
                    "tag_ids": tag_ids,
                },
                result={
                    "skipped": False,
                    "reason": "tag_mutation_composition_unavailable",
                    "external_effect_status": "blocked",
                    "local_projection": local_projection,
                    "local_projection_updated": bool(local_projection.get("local_projection_updated")),
                },
                error_code="tag_mutation_composition_unavailable",
            )
        return self._tag_mutation_executor(
            questionnaire_id=questionnaire_id,
            submission_id=submission_id,
            external_userid=external_userid,
            tag_ids=tag_ids,
            unionid=unionid,
            follow_user_userid=follow_user_userid,
            local_projection=local_projection,
        )

    def emit_external_push(self, *, questionnaire_id: int | str, submission_id: str, webhook_url: str | None, payload_summary: dict[str, Any]) -> Json:
        if not webhook_url:
            return self.record_side_effect_audit(
                operation="emit_external_push",
                target={"questionnaire_id": questionnaire_id, "submission_id": submission_id, "webhook_url": ""},
                result={"skipped": True, "reason": "missing_webhook_url"},
            )
        return self._push_adapter.push_submission_event(questionnaire_id=questionnaire_id, submission_id=submission_id, webhook_url=webhook_url, payload_summary=payload_summary)

    def bind_mobile(self, *, submission: dict[str, Any], questionnaire: dict[str, Any]) -> Json:
        external_userid = str(submission.get("external_userid") or "").strip()
        mobile = str(submission.get("mobile") or "").strip()
        if not external_userid or not mobile:
            return self.record_side_effect_audit(
                operation="bind_mobile",
                target={
                    "questionnaire_id": questionnaire.get("id"),
                    "submission_id": submission.get("submission_id"),
                    "external_userid": external_userid,
                    "mobile_present": bool(mobile),
                },
                result={"skipped": True, "reason": "missing_external_userid_or_mobile"},
            )
        target = {
            "questionnaire_id": questionnaire.get("id"),
            "submission_id": submission.get("submission_id"),
            "external_userid": external_userid,
            "mobile_present": True,
        }
        if self._mobile_binder is None:
            return self.record_side_effect_audit(
                operation="bind_mobile",
                target=target,
                result={"skipped": False, "reason": "mobile_binding_composition_unavailable"},
                error_code="mobile_binding_composition_unavailable",
            )
        try:
            result = self._mobile_binder(
                external_userid=external_userid,
                mobile=mobile,
                owner_userid=str(submission.get("owner_userid") or submission.get("follow_user_userid") or "").strip(),
                bind_by_userid="questionnaire_submit",
                customer_name=str(submission.get("customer_name") or "问卷提交用户").strip(),
                force_rebind=True,
            )
        except Exception as exc:
            return self.record_side_effect_audit(
                operation="bind_mobile",
                target=target,
                result={"skipped": False, "reason": "bind_failed", "error": str(exc)},
                error_code="mobile_bind_failed",
            )
        idempotency_key = make_idempotency_key(operation="bind_mobile", payload={"target": target, "result": result})
        audit = record_audit_event(
            adapter=self.adapter_name,
            operation="bind_mobile",
            mode="fake",
            idempotency_key=idempotency_key,
            side_effect_executed=bool(result.get("side_effect_executed")),
            status="ok",
        )
        payload = _base_result(
            ok=True,
            adapter=self.adapter_name,
            mode="fake",
            operation="bind_mobile",
            idempotency_key=idempotency_key,
            target=target,
            result=result,
            audit_id=audit["audit_id"],
        )
        payload["side_effect_executed"] = bool(result.get("side_effect_executed"))
        return payload

    def emit_automation_questionnaire_result(self, *, questionnaire: dict[str, Any], submission: dict[str, Any], final_tags: list[str]) -> Json:
        followup_type = "priority" if "tag_interest_ai_tools" in final_tags else "normal"
        return self.record_side_effect_audit(
            operation="emit_automation_questionnaire_result",
            target={"questionnaire_id": questionnaire.get("id"), "submission_id": submission.get("submission_id"), "external_userid": submission.get("external_userid") or ""},
            result={
                "ok": False,
                "source_status": "retired_automation_member_noop",
                "retired": True,
                "member_id": "",
                "followup_type": followup_type,
                "current_pool": "",
                "message": "旧 automation_member 问卷分流已退场；请使用 AI Audience source-poke / refresh 链路。",
            },
        )

    def record_side_effect_audit(self, *, operation: str, target: dict[str, Any], result: dict[str, Any] | None = None, error_code: str = "") -> Json:
        idempotency_key = make_idempotency_key(operation=operation, payload={"target": _safe_target(target), "result": result or {}})
        audit = record_audit_event(
            adapter=self.adapter_name,
            operation=operation,
            mode="fake",
            idempotency_key=idempotency_key,
            side_effect_executed=False,
            status="blocked" if error_code else "ok",
            error_code=error_code,
        )
        return _base_result(
            ok=not error_code,
            adapter=self.adapter_name,
            mode="fake",
            operation=operation,
            idempotency_key=idempotency_key,
            target=target,
            result=result or {},
            audit_id=audit["audit_id"],
            error_code=error_code,
            error_message="" if not error_code else "side effect audit recorded as blocked",
        )

    def side_effect_safety(self) -> dict[str, Any]:
        return {
            "wechat_oauth_mode": build_wechat_oauth_adapter().mode,
            "wecom_tag_mode": "next_plan",
            "questionnaire_external_push_mode": self._push_adapter.mode,
            "real_oauth_executed": False,
            "real_wecom_tag_executed": False,
            "real_external_webhook_executed": False,
            "real_mobile_binding_executed": False,
            "side_effect_executed": False,
        }


def build_wechat_oauth_adapter() -> WeChatOAuthAdapter:
    return WeChatOAuthAdapter(managed_runtime_setting("AICRM_NEXT_WECHAT_OAUTH_MODE", "fake"))


def build_wecom_tag_adapter() -> WeComTagAdapter:
    return WeComTagAdapter(managed_runtime_setting("AICRM_NEXT_WECOM_TAG_MODE", "fake"))


def build_questionnaire_external_push_adapter() -> QuestionnaireExternalPushAdapter:
    return QuestionnaireExternalPushAdapter(
        managed_runtime_setting("AICRM_NEXT_QUESTIONNAIRE_WEBHOOK_MODE", "fake")
    )
