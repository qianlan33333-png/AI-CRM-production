from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any, Callable, ContextManager

from aicrm_next.platform.shared.postgres_connection import db_session
from aicrm_next.platform.shared.runtime_settings import managed_runtime_setting

from .domain import ENTRY_CHANGE_TYPES, text
from .identity_bridge_repo import IdentityBridgeRepository, PostgresIdentityBridgeRepository, build_identity_bridge_repository
from .crm_port import apply_verified_wecom_detail
from .wecom_adapter import WeComAdapterBlocked, WeComApiError, get_wecom_adapter

SIDEBAR_IDENTITY_REFRESH_INTERVAL_SECONDS = 60


def _single_corp_id(requested_corp_id: str) -> tuple[str, str]:
    configured = text(managed_runtime_setting("WECOM_CORP_ID"))
    requested = text(requested_corp_id)
    if configured and requested and requested != configured:
        return "", "corp_id_mismatch"
    return configured or requested, ""


def _adapter_failure(exc: Exception) -> tuple[str, dict[str, Any]]:
    if isinstance(exc, WeComAdapterBlocked):
        payload: dict[str, Any] = {"reason": exc.reason}
        if exc.missing_config:
            payload["missing_config"] = exc.missing_config
        return exc.reason, payload
    if isinstance(exc, WeComApiError):
        payload = {"reason": "wecom_api_error", "message": exc.message}
        if exc.payload:
            payload["wecom_result"] = exc.payload
        return "wecom_api_error", payload
    return "wecom_api_error", {"reason": "wecom_api_error", "message": str(exc)}


def _preferred_owner_userid(owner_userid: str, detail: dict[str, Any]) -> str:
    owner = text(owner_userid)
    if owner:
        return owner
    for item in list((detail or {}).get("follow_user") or []):
        userid = text((item or {}).get("userid"))
        if userid:
            return userid
    return ""


def _sync_profile_description_with_external_userid(
    adapter: Any,
    *,
    external_userid: str,
    owner_userid: str,
) -> dict[str, Any]:
    external = text(external_userid)
    owner = text(owner_userid)
    if not external:
        return {"status": "skipped", "reason": "external_userid_missing"}
    if not owner:
        return {"status": "skipped", "reason": "owner_userid_missing", "description_source": "external_userid"}
    updater = getattr(adapter, "update_external_contact_remark", None)
    if not callable(updater):
        return {
            "status": "skipped",
            "reason": "adapter_missing_update_external_contact_remark",
            "description_source": "external_userid",
        }
    payload = {"userid": owner, "external_userid": external, "description": external}
    try:
        result = updater(payload)
    except Exception as exc:
        reason, failure = _adapter_failure(exc)
        return {"status": "failed", "reason": reason, **failure, "description_source": "external_userid"}
    if int((result or {}).get("errcode") or 0) != 0:
        return {
            "status": "failed",
            "reason": "wecom_api_error",
            "wecom_result": dict(result or {}),
            "description_source": "external_userid",
        }
    return {
        "status": "success",
        "description_source": "external_userid",
        "description": external,
        "real_external_call_executed": True,
    }


def _age_seconds(value: Any) -> int | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        now = datetime.now(timezone.utc)
    else:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    return max(0, int((now - value).total_seconds()))


def _refresh_reason(state: dict[str, Any], *, min_interval_seconds: int) -> str:
    if not state.get("exists"):
        return "identity_missing"
    age = state.get("age_seconds")
    if isinstance(age, int) and age < min_interval_seconds:
        return ""
    if not state.get("unionid_present") and not state.get("openid_present"):
        return "identity_missing_unionid_openid"
    if not state.get("mobile_bound"):
        return "mobile_not_bound"
    return ""


class IdentityBridgeService:
    def __init__(
        self,
        *,
        repository: IdentityBridgeRepository | None = None,
        adapter_factory: Callable[[], Any] = get_wecom_adapter,
    ) -> None:
        self.repository = repository or build_identity_bridge_repository()
        self._adapter_factory = adapter_factory

    def _repository_session(self) -> ContextManager[Any]:
        if isinstance(self.repository, PostgresIdentityBridgeRepository):
            return db_session()
        return nullcontext()

    def identity_bridge_state(self, external_userid: str) -> dict[str, Any]:
        state = dict(self.repository.identity_bridge_state(text(external_userid)) or {})
        state["age_seconds"] = _age_seconds(state.get("updated_at"))
        return state

    def bind_mobile_from_identity_sources(
        self,
        external_userid: str,
        owner_userid: str = "",
        bind_by_userid: str = "",
        force_rebind: bool = False,
    ) -> dict[str, Any]:
        external = text(external_userid)
        if not external:
            return {"status": "skipped", "reason": "external_userid_missing"}

        with self._repository_session():
            existing = dict(self.repository.get_contact_binding_status(external, owner_userid) or {})
            candidate = self.repository.get_unique_mobile_candidate_from_identity_sources(external)
            if existing.get("is_bound"):
                if candidate and text(candidate.get("mobile")) and text(candidate.get("mobile")) != text(existing.get("mobile")) and not force_rebind:
                    return {
                        "status": "conflict",
                        "reason": "external_userid already bound to another mobile",
                        "mobile": text(candidate.get("mobile")),
                        "binding": existing,
                    }
                return {"status": "already_bound", "mobile": text(existing.get("mobile")), "binding": existing}

            if not candidate:
                return {"status": "skipped", "reason": "no_single_candidate"}

            mobile = text(candidate.get("mobile"))
            owner = self.repository.resolve_binding_owner_userid(external, owner_userid)
            binding = self.repository.upsert_external_contact_binding_record(
                external_userid=external,
                person_id=0,
                mobile=mobile,
                bind_by_userid=text(bind_by_userid) or owner or "wecom_external_contact_callback",
                owner_userid=owner,
                force_rebind=force_rebind,
            )
            lead_pool_merge = self.repository.merge_lead_pool_after_mobile_bind(
                external_userid=external,
                mobile=mobile,
                owner_userid=owner,
            )
            return {
                "status": "bound",
                "mobile": mobile,
                "candidate": dict(candidate),
                "binding": binding,
                "lead_pool_merge": lead_pool_merge,
            }

    def _sync_external_contact_identity_for_detail(
        self,
        *,
        detail_payload: dict[str, Any],
        external_userid: str,
        owner_userid: str,
        corp_id: str,
    ) -> dict[str, Any]:
        detail_external = text(((detail_payload or {}).get("external_contact") or {}).get("external_userid"))
        if detail_external != text(external_userid):
            return {"status": "failed", "reason": "provider_target_mismatch", "real_external_call_executed": False}
        follow_userids = {
            text((item or {}).get("userid"))
            for item in list((detail_payload or {}).get("follow_user") or [])
            if text((item or {}).get("userid"))
        }
        if not text(owner_userid) or text(owner_userid) not in follow_userids:
            return {
                "status": "failed",
                "reason": "provider_relationship_mismatch",
                "real_external_call_executed": False,
            }
        with self._repository_session():
            record = self.repository.normalize_external_contact_identity(
                corp_id,
                detail_payload,
                owner_userid,
                status="active",
            )
            if not text(record.get("external_userid")):
                return {"status": "skipped", "reason": "contact_detail_missing_external_userid"}

            oneid_result: dict[str, Any] = {}
            if isinstance(self.repository, PostgresIdentityBridgeRepository):
                oneid_result = apply_verified_wecom_detail(
                    corp_id=corp_id,
                    external_userid=external_userid,
                    owner_userid=owner_userid,
                    detail_payload=detail_payload,
                )
                if text(oneid_result.get("status")) == "conflict":
                    return {
                        **oneid_result,
                        "reason": text(oneid_result.get("reason")) or "identity_conflict",
                        "unionid_present": bool(text(record.get("unionid"))),
                        "openid_present": bool(text(record.get("openid"))),
                    }

            identity_map_id = self.repository.upsert_external_contact_identity(record)
            if not text(record.get("unionid")):
                return {
                    "status": "pending_identity",
                    "reason": "missing_unionid",
                    "identity_map_id": int(identity_map_id or 0),
                    "customer_id": int(oneid_result.get("customer_id") or 0),
                    "customer_status": "active",
                    "unionid_status": text(oneid_result.get("unionid_status")) or "pending",
                    "unionid_present": False,
                    "openid_present": bool(text(record.get("openid"))),
                    "mobile_binding": {"status": "skipped", "reason": "identity_pending_unionid"},
                    "questionnaire_backfill": {"status": "skipped", "reason": "identity_pending_unionid"},
                }
            self.repository.replace_external_contact_follow_users(
                corp_id,
                external_userid,
                list((detail_payload or {}).get("follow_user") or []),
                preferred_userid=owner_userid,
            )
            self.repository.refresh_external_contact_identity_owner(corp_id, external_userid)
            mobile_binding = self.bind_mobile_from_identity_sources(
                external_userid,
                owner_userid=owner_userid,
                bind_by_userid=owner_userid or "wecom_external_contact_callback",
            )
            questionnaire_backfill: dict[str, Any] = {"status": "skipped", "reason": "mobile_not_bound"}
            if text((mobile_binding or {}).get("mobile")) and text((mobile_binding or {}).get("status")) in {"bound", "already_bound"}:
                questionnaire_backfill = self.repository.backfill_questionnaire_submissions_for_mobile_binding(
                    external_userid=external_userid,
                    mobile=text(mobile_binding.get("mobile")),
                    follow_user_userid=owner_userid,
                )
            return {
                "status": "success",
                "identity_map_id": int(identity_map_id or 0),
                "customer_id": int(oneid_result.get("customer_id") or 0),
                "merge_action": text(oneid_result.get("action")),
                "unionid": text(record.get("unionid")),
                "unionid_present": bool(text(record.get("unionid"))),
                "openid_present": bool(text(record.get("openid"))),
                "mobile_binding": mobile_binding,
                "questionnaire_backfill": questionnaire_backfill,
            }

    def sync_external_contact_identity_for_event(self, event: dict[str, Any], *, corp_id: str = "") -> dict[str, Any]:
        if text(event.get("Event")) != "change_external_contact" or text(event.get("ChangeType")) not in ENTRY_CHANGE_TYPES:
            return {"status": "skipped", "reason": "unsupported_event"}
        external_userid = text(event.get("ExternalUserID"))
        owner_userid = text(event.get("UserID"))
        if not external_userid:
            return {"status": "skipped", "reason": "external_userid_missing"}
        effective_corp_id, corp_error = _single_corp_id(corp_id)
        if corp_error:
            return {
                "status": "failed",
                "reason": corp_error,
                "real_external_call_executed": False,
            }

        try:
            adapter = self._adapter_factory()
            detail_loader = getattr(adapter, "get_external_contact_detail", None)
            if not callable(detail_loader):
                return {"status": "skipped", "reason": "adapter_missing_get_external_contact_detail"}
            detail = detail_loader(external_userid)
            if int((detail or {}).get("errcode") or 0) != 0:
                return {"status": "failed", "reason": "wecom_api_error", "wecom_result": dict(detail or {})}
            detail_payload = dict(detail or {})
            owner_userid = _preferred_owner_userid(owner_userid, detail_payload)
            profile_description = _sync_profile_description_with_external_userid(
                adapter,
                external_userid=external_userid,
                owner_userid=owner_userid,
            )
            result = self._sync_external_contact_identity_for_detail(
                detail_payload=detail_payload,
                external_userid=external_userid,
                owner_userid=owner_userid,
                corp_id=effective_corp_id,
            )
            result["profile_description"] = profile_description
            return result
        except Exception as exc:
            reason, failure = _adapter_failure(exc)
            return {"status": "failed", "reason": reason, **failure}

    def apply_external_contact_detail(
        self,
        *,
        detail_payload: dict[str, Any],
        external_userid: str,
        owner_userid: str = "",
        corp_id: str = "",
    ) -> dict[str, Any]:
        """Apply an already-persisted provider result without any provider call."""

        external = text(external_userid)
        if not external:
            return {"status": "failed", "reason": "external_userid_missing", "real_external_call_executed": False}
        effective_corp_id, corp_error = _single_corp_id(corp_id)
        if corp_error:
            return {"status": "failed", "reason": corp_error, "real_external_call_executed": False}
        detail = dict(detail_payload or {})
        detail_external = text((detail.get("external_contact") or {}).get("external_userid"))
        if detail_external and detail_external != external:
            return {"status": "failed", "reason": "provider_target_mismatch", "real_external_call_executed": False}
        preferred_owner = _preferred_owner_userid(owner_userid, detail)
        result = self._sync_external_contact_identity_for_detail(
            detail_payload=detail,
            external_userid=external,
            owner_userid=preferred_owner,
            corp_id=effective_corp_id,
        )
        result["real_external_call_executed"] = False
        result["provider_result_applied"] = True
        return result

    def ensure_external_contact_identity_for_sidebar(
        self,
        *,
        external_userid: str,
        owner_userid: str = "",
        corp_id: str = "",
        min_interval_seconds: int = SIDEBAR_IDENTITY_REFRESH_INTERVAL_SECONDS,
    ) -> dict[str, Any]:
        normalized_external_userid = text(external_userid)
        if not normalized_external_userid:
            return {"status": "skipped", "reason": "external_userid_missing"}
        _, corp_error = _single_corp_id(corp_id)
        if corp_error:
            return {
                "status": "failed",
                "reason": corp_error,
                "real_external_call_executed": False,
            }
        state = self.identity_bridge_state(normalized_external_userid)
        reason = _refresh_reason(state, min_interval_seconds=max(0, int(min_interval_seconds)))
        if not reason:
            return {
                "status": "skipped",
                "reason": "identity_fresh",
                "unionid_present": bool(state.get("unionid_present")),
                "openid_present": bool(state.get("openid_present")),
                "mobile_bound": bool(state.get("mobile_bound")),
                "age_seconds": state.get("age_seconds"),
            }
        from . import repo as channel_repo

        planned = channel_repo.enqueue_channel_entry_identity_resolution(
            corp_id=text(corp_id),
            external_userid=normalized_external_userid,
            follow_user_userid=text(owner_userid),
            payload_json={
                "Event": "change_external_contact",
                "ChangeType": "edit_external_contact",
                "ExternalUserID": normalized_external_userid,
                "UserID": text(owner_userid),
                "source": "sidebar_identity_refresh",
            },
            reason=reason,
        )
        return {
            "status": "queued" if planned.get("external_effect_job_id") else "held",
            "reason": reason,
            "external_effect_job_id": int(planned.get("external_effect_job_id") or 0) or None,
            "execution_id": text(planned.get("execution_id")),
            "unionid_present": bool(state.get("unionid_present")),
            "openid_present": bool(state.get("openid_present")),
            "mobile_bound": bool(state.get("mobile_bound")),
            "real_external_call_executed": False,
        }


def build_identity_bridge_service(
    *,
    repository: IdentityBridgeRepository | None = None,
    adapter_factory: Callable[[], Any] = get_wecom_adapter,
) -> IdentityBridgeService:
    return IdentityBridgeService(repository=repository, adapter_factory=adapter_factory)
