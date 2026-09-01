from __future__ import annotations

from typing import Any, Mapping
from uuid import uuid4

from aicrm_next.platform.shared.errors import NotFoundError

from .domain import normalize_questionnaire, public_projection
from .oauth import (
    COOKIE_NAME,
    build_questionnaire_h5_identity_cookie,
    questionnaire_h5_identity_from_cookies,
    resolve_adapter_mode,
)
from .operations import resolve_questionnaire_completion_action
from .repo import QuestionnaireRepository, build_questionnaire_repository


IDENTITY_FIELDS = ("respondent_key", "openid", "unionid", "external_userid")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _read_meta(repo: QuestionnaireRepository) -> dict[str, Any]:
    return {
        "route_owner": "ai_crm_next",
        "fallback_used": False,
        "source_status": getattr(repo, "source_status", "local_contract_probe"),
        "read_model_status": getattr(repo, "read_model_status", "fixture"),
        "degraded": False,
        "page_error": "",
    }


class QuestionnaireOAuthConfigReader:
    def adapter_mode(self) -> str:
        return resolve_adapter_mode()

    def is_configured(self) -> bool:
        return bool(self.adapter_mode())

    def read(self) -> dict[str, Any]:
        mode = self.adapter_mode()
        return {
            "ok": True,
            "configured": bool(mode),
            "adapter_mode": mode,
            "source_status": "next_runtime_config",
            "route_owner": "ai_crm_next",
            "fallback_used": False,
        }


class QuestionnaireRespondentIdentityService:
    def resolve(
        self,
        *,
        cookies: Mapping[str, str] | None = None,
        request_identity: Mapping[str, Any] | None = None,
        slug: str = "",
    ) -> dict[str, Any]:
        raw_cookie_identity = questionnaire_h5_identity_from_cookies(cookies or {})
        cookie_identity = self._clean_identity(raw_cookie_identity)
        query_identity = self._clean_identity(request_identity or {})
        cookie_is_anonymous = bool(raw_cookie_identity.get("anonymous"))
        query_has_identity = any(query_identity.get(field) for field in IDENTITY_FIELDS)
        if cookie_is_anonymous and query_has_identity:
            cookie_identity = {}
        identity: dict[str, str] = {}
        for field in IDENTITY_FIELDS:
            if cookie_identity.get(field):
                identity[field] = cookie_identity[field]
        for field in IDENTITY_FIELDS:
            if not identity.get(field) and query_identity.get(field):
                identity[field] = query_identity[field]
        for field in ("app_id", "customer_id", "respondent_identity_id"):
            value = _text(raw_cookie_identity.get(field))
            if value:
                identity[field] = value
        anonymous = False
        cookie_value = ""
        cookie_name = COOKIE_NAME
        if not any(identity.get(field) for field in IDENTITY_FIELDS):
            anonymous = True
            identity["respondent_key"] = f"anon_{uuid4().hex}"
            cookie_value = build_questionnaire_h5_identity_cookie({"respondent_key": identity["respondent_key"], "slug": slug, "anonymous": True})
        elif not cookie_identity and query_has_identity:
            anonymous = _text(identity.get("respondent_key")).startswith("anon_")
            cookie_value = build_questionnaire_h5_identity_cookie({**identity, "slug": slug, "anonymous": anonymous})
        elif (cookie_is_anonymous and identity.get("respondent_key") == cookie_identity.get("respondent_key")) or _text(
            identity.get("respondent_key")
        ).startswith("anon_"):
            anonymous = True
        return {
            "ok": True,
            "identity": identity,
            "anonymous": anonymous,
            "cookie_name": cookie_name,
            "cookie_value": cookie_value,
            "source_status": "next_identity",
            "route_owner": "ai_crm_next",
            "fallback_used": False,
        }

    def _clean_identity(self, value: Mapping[str, Any]) -> dict[str, str]:
        return {field: _text(value.get(field)) for field in IDENTITY_FIELDS}


class QuestionnairePublicReadService:
    def __init__(self, repo: QuestionnaireRepository | None = None) -> None:
        self._repo = repo or build_questionnaire_repository()

    def get_public_questionnaire(self, slug: str) -> dict[str, Any]:
        item = self._repo.get_questionnaire_by_slug(slug)
        if not item:
            raise NotFoundError("questionnaire not found")
        if not bool(item.get("enabled", True)):
            raise NotFoundError("questionnaire disabled")
        return {"ok": True, **_read_meta(self._repo), **public_projection(item)}


class QuestionnaireSubmissionStatusService:
    def __init__(self, repo: QuestionnaireRepository | None = None) -> None:
        self._repo = repo or build_questionnaire_repository()

    def get_submission_status(self, slug: str, *, identity: Mapping[str, Any] | None = None) -> dict[str, Any]:
        item = self._repo.get_questionnaire_by_slug(slug)
        if not item:
            raise NotFoundError("questionnaire not found")
        if not bool(item.get("enabled", True)):
            raise NotFoundError("questionnaire disabled")
        submission = self._repo.find_submission_for_identity(int(item["id"]), dict(identity or {}))
        normalized_slug = _text(item.get("slug")) or _text(slug)
        redirect_url = _text(item.get("redirect_url"))
        questionnaire = normalize_questionnaire(item)
        completion_projection = (
            resolve_questionnaire_completion_action(item) if submission else {"completion_action": {"type": "default", "redirect_url": ""}, "lead_qr": None}
        )
        return {
            "ok": True,
            "submitted": bool(submission),
            "questionnaire_id": int(item["id"]),
            "slug": normalized_slug,
            "identity_key": self._identity_key(identity or {}),
            "submission": submission,
            "redirect_url": redirect_url,
            "completion_target": questionnaire["completion_target"],
            "completion_target_enabled": questionnaire["completion_target_enabled"],
            "completion_target_type": questionnaire["completion_target_type"],
            "completion_action": completion_projection["completion_action"],
            "lead_qr": completion_projection["lead_qr"],
            "submitted_url": f"/s/{normalized_slug}/submitted",
            **_read_meta(self._repo),
        }

    def _identity_key(self, identity: Mapping[str, Any]) -> str:
        for field in ("external_userid", "unionid", "openid", "respondent_key"):
            value = _text(identity.get(field))
            if value:
                return f"{field}:{value}"
        return ""
