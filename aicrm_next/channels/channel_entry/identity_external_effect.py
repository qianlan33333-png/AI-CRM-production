from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import text

from .crm_port import (
    CompleteIdentityResolutionRequest,
    build_identity_resolution_queue_port,
)
from aicrm_next.platform.platform_foundation.command_bus.models import CommandContext
from aicrm_next.platform.platform_foundation.external_effects import (
    WECOM_EXTERNAL_CONTACT_DETAIL_FETCH,
    WECOM_PROFILE_UPDATE,
    ExternalEffectService,
)
from aicrm_next.platform.platform_foundation.external_effects.continuations import ExternalEffectContinuation
from aicrm_next.platform.platform_foundation.external_effects.realtime import wake_external_effect_job
from aicrm_next.platform.platform_foundation.external_effects.repo import build_external_effect_repository
from aicrm_next.platform.platform_foundation.internal_events import InternalEventService
from aicrm_next.platform.shared.db_session import get_session_factory

from .identity_bridge_service import build_identity_bridge_service
from .profile_description_backfill import (
    PROFILE_DESCRIPTION_BACKFILL_DETAIL_BUSINESS_TYPE,
    PROFILE_DESCRIPTION_BACKFILL_UPDATE_BUSINESS_TYPE,
    is_profile_description_backfill_detail,
    is_profile_description_backfill_settlement,
    run_profile_description_backfill_detail,
    settle_profile_description_backfill,
)


IDENTITY_RESOLVED_EVENT_TYPE = "identity.resolved"
IDENTITY_RESOLUTION_BUSINESS_TYPE = "identity_resolution_queue"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _identity_resolution_queue_id(job) -> int:
    payload = dict(job.payload_json or {})
    payload_queue_id = _positive_int(payload.get("queue_id"))
    business_queue_id = _positive_int(job.business_id)
    if payload_queue_id and business_queue_id and payload_queue_id != business_queue_id:
        return 0
    return payload_queue_id or business_queue_id


def _owner_description(provider_detail: dict[str, Any], owner_userid: str) -> tuple[str, str, bool]:
    owner = _text(owner_userid)
    follow_users = [dict(item or {}) for item in list(provider_detail.get("follow_user") or [])]
    if not owner:
        owner = next((_text(item.get("userid")) for item in follow_users if _text(item.get("userid"))), "")
    for item in follow_users:
        if _text(item.get("userid")) == owner:
            return owner, _text(item.get("description")), True
    return owner, "", False


def plan_profile_description_update(
    *,
    parent_job,
    queue_id: int,
    event_log_id: int | None,
    provider_detail: dict[str, Any],
    external_userid: str,
    owner_userid: str,
    service: ExternalEffectService | None = None,
) -> dict[str, Any]:
    external = _text(external_userid)
    if not external:
        return {"status": "skipped", "reason": "external_userid_missing", "description_source": "external_userid"}
    owner, current_description, owner_relationship_present = _owner_description(provider_detail, owner_userid)
    if not owner:
        return {"status": "skipped", "reason": "owner_userid_missing", "description_source": "external_userid"}
    if not owner_relationship_present:
        return {"status": "skipped", "reason": "owner_relationship_missing", "description_source": "external_userid"}
    if external in current_description:
        return {
            "status": "skipped",
            "reason": "external_userid_already_present",
            "description_source": "external_userid",
        }

    desired_description = f"{current_description}\n{external}" if current_description else external
    planned = (service or ExternalEffectService()).plan_effect(
        effect_type=WECOM_PROFILE_UPDATE,
        adapter_name="wecom_profile",
        operation="update_description",
        target_type="external_user",
        target_id=external,
        payload={
            "external_userid": external,
            "follow_user_userid": owner,
            "description": desired_description,
        },
        payload_summary={
            "external_userid_present": True,
            "owner_userid_present": True,
            "existing_description_present": bool(current_description),
            "description_appended": bool(current_description),
            "description_source": "external_userid",
            "real_external_call_executed": False,
        },
        context=CommandContext(
            actor_id="identity_resolution_continuation",
            actor_type="system",
            request_id=str(event_log_id or queue_id),
            trace_id=_text(getattr(parent_job, "trace_id", "")) or f"identity-resolution-{queue_id}",
            source_route="external_effect.completed/identity_profile_description",
        ),
        business_type="identity_resolution_profile_description",
        business_id=str(queue_id),
        source_module="aicrm_next.channels.channel_entry.identity_external_effect",
        source_event_id=str(event_log_id or ""),
        source_command_id=str(int(getattr(parent_job, "id", 0) or 0)),
        risk_level="medium",
        execution_mode="execute",
        status="queued",
        idempotency_key=f"identity-resolution:queue:{queue_id}:profile-description:v1",
        execution_id=f"exe_identity_profile_{uuid4().hex}",
        parent_execution_id=_text(getattr(parent_job, "execution_id", "")),
        lane="wecom_interactive",
        ordering_key=f"external_user:{external}",
    )
    job_id = _positive_int(planned.get("id"))
    wake_scheduled = wake_external_effect_job(
        job_id,
        reason="identity_profile_description",
        effect_type=WECOM_PROFILE_UPDATE,
    )
    return {
        "status": "queued",
        "description_source": "external_userid",
        "external_effect_job_id": job_id,
        "created": bool(planned.get("created_on_plan")),
        "description_appended": bool(current_description),
        "immediate_dispatch_scheduled": wake_scheduled,
        "real_external_call_executed": False,
    }


def _matches(job, _result) -> bool:
    if is_profile_description_backfill_detail(job):
        return True
    return (
        job.effect_type == WECOM_EXTERNAL_CONTACT_DETAIL_FETCH
        and _text(job.business_type) == IDENTITY_RESOLUTION_BUSINESS_TYPE
        and _identity_resolution_queue_id(job) > 0
    )


def _matches_terminal_identity(job, result) -> bool:
    if is_profile_description_backfill_settlement(job):
        return True
    return (
        job.effect_type == WECOM_EXTERNAL_CONTACT_DETAIL_FETCH
        and _text(job.business_type) == IDENTITY_RESOLUTION_BUSINESS_TYPE
        and _identity_resolution_queue_id(job) > 0
        and job.status != "succeeded"
    )


def _settle_terminal_identity(job, _dispatch_result) -> dict[str, Any]:
    if _text(job.business_type) == PROFILE_DESCRIPTION_BACKFILL_UPDATE_BUSINESS_TYPE:
        return settle_profile_description_backfill(job, _dispatch_result)
    queue_id = _identity_resolution_queue_id(job)
    if queue_id <= 0:
        return {"ok": False, "error": "identity_resolution_queue_id_missing"}
    target_status = {
        "unknown_after_dispatch": "held",
        "blocked": "held",
        "failed_terminal": "failed",
        "cancelled": "ignored",
        "simulated": "ignored",
    }.get(_text(job.status))
    if not target_status:
        return {"ok": True, "projected": False, "reason": "identity_effect_status_not_terminal"}
    error_code = _text(job.last_error_code) or f"external_effect_{_text(job.status)}"
    with get_session_factory()() as session:
        queue_updated = build_identity_resolution_queue_port().settle_terminal_sqlalchemy(
            session,
            queue_id=queue_id,
            external_effect_job_id=int(job.id),
            status=target_status,
            error_code=error_code,
        )
        runtime_count = session.execute(
            text(
                """
                UPDATE automation_channel_entry_runtime
                SET identity_status = :status,
                    identity_hold_reason = CASE WHEN :status = 'held' THEN :last_error ELSE '' END,
                    identity_held_at = CASE WHEN :status = 'held' THEN COALESCE(identity_held_at, CURRENT_TIMESTAMP) ELSE identity_held_at END,
                    identity_next_attempt_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE identity_external_effect_job_id = :job_id
                  AND identity_status NOT IN ('resolved', 'conflict')
                """
            ),
            {
                "status": target_status,
                "last_error": error_code,
                "job_id": int(job.id),
            },
        ).rowcount
        session.commit()
    return {
        "ok": True,
        "projected": bool(queue_updated or runtime_count),
        "queue_id": queue_id,
        "queue_status": target_status,
        "runtime_updated_count": int(runtime_count or 0),
    }


def _run(job, dispatch_result) -> dict[str, Any]:
    try:
        if _text(job.business_type) == PROFILE_DESCRIPTION_BACKFILL_DETAIL_BUSINESS_TYPE:
            return run_profile_description_backfill_detail(job, dispatch_result)
        return _run_private(job, dispatch_result)
    except Exception:
        # The raw provider detail is deliberately in scope here. Never allow an
        # exception string to flow into public continuation summaries.
        return {"ok": False, "error": "identity_local_continuation_failed"}


def _run_private(job, dispatch_result) -> dict[str, Any]:
    payload = dict(job.payload_json or {})
    queue_id = _identity_resolution_queue_id(job)
    attempt_id = _text(job.last_attempt_id)
    if queue_id <= 0 or not attempt_id:
        return {"ok": False, "error": "identity_resolution_completion_identifiers_missing"}

    queue_port = build_identity_resolution_queue_port()
    with get_session_factory()() as session:
        existing = queue_port.get_completion_receipt_sqlalchemy(
            session,
            external_effect_job_id=int(job.id),
        )
        if existing:
            _consume_provider_result(attempt_id, job_id=int(job.id))
            return {
                "ok": True,
                "deduplicated": True,
                "queue_id": queue_id,
                "result_status": _text(existing.get("result_status")),
                "provider_result_consumed": True,
            }
        queue_row = queue_port.get_queue_sqlalchemy(session, queue_id=queue_id)
    if not queue_row:
        return {"ok": False, "error": "identity_resolution_queue_not_found"}
    if int(queue_row.get("external_effect_job_id") or 0) != int(job.id):
        return {"ok": False, "error": "identity_resolution_effect_link_mismatch"}

    provider_detail = dict(dispatch_result.provider_result or {})
    if not provider_detail.get("external_contact"):
        return {"ok": False, "error": "identity_provider_result_missing"}
    external_userid = _text(payload.get("external_userid") or queue_row.get("external_userid"))
    result = build_identity_bridge_service().apply_external_contact_detail(
        detail_payload=provider_detail,
        external_userid=external_userid,
        owner_userid=_text(payload.get("owner_userid")),
        corp_id=_text(payload.get("corp_id") or queue_row.get("corp_id")),
    )
    result_status = _text(result.get("status"))
    if result_status not in {"success", "pending_identity", "conflict"}:
        return {
            "ok": False,
            "error": _text(result.get("reason")) or "identity_provider_result_apply_failed",
        }

    event_log_id = int(payload.get("event_log_id") or 0) or None
    profile_description = plan_profile_description_update(
        parent_job=job,
        queue_id=queue_id,
        event_log_id=event_log_id,
        provider_detail=provider_detail,
        external_userid=external_userid,
        owner_userid=_text(payload.get("owner_userid")),
    )
    result["profile_description"] = profile_description
    queue_payload = dict(queue_row.get("payload_json") or {})
    from . import application

    diagnostic = application._record_identity_sync_result(event_log_id, result)
    canonical: dict[str, Any] = {"status": "skipped", "reason": "unionid_missing"}
    if result_status == "success":
        canonical = application._canonicalize_channel_entry_after_identity(
            queue_payload,
            corp_id=_text(payload.get("corp_id") or queue_row.get("corp_id")),
            event_log_id=event_log_id,
            identity_sync=result,
        )
        emitted = _emit_identity_resolved(
            job=job,
            queue_id=queue_id,
            unionid=_text(result.get("unionid")),
            identity_map_id=int(result.get("identity_map_id") or 0),
            source_type=_text(queue_row.get("source_type")),
        )
        if not emitted.get("ok"):
            return {"ok": False, "error": "identity_resolved_event_emit_failed"}
    else:
        application._mark_runtime_identity_from_sync(
            queue_payload,
            corp_id=_text(payload.get("corp_id") or queue_row.get("corp_id")),
            event_log_id=event_log_id,
            identity_sync=result,
        )

    completion_status = {
        "success": "resolved",
        "pending_identity": "pending",
        "conflict": "conflict",
    }[result_status]
    if _text(result.get("unionid_status")) == "not_applicable":
        completion_status = "ignored"
    persisted = _persist_completion_receipt(
        job=job,
        attempt_id=attempt_id,
        queue_id=queue_id,
        result_status=completion_status,
        result=result,
    )
    consumed = _consume_provider_result(attempt_id, job_id=int(job.id))
    return {
        "ok": True,
        "deduplicated": not persisted,
        "queue_id": queue_id,
        "result_status": completion_status,
        "diagnostic_ok": bool(diagnostic.get("ok")),
        "canonical_status": _text(canonical.get("status")),
        "profile_description_status": _text(profile_description.get("status")),
        "profile_description_job_id": _positive_int(profile_description.get("external_effect_job_id")) or None,
        "provider_result_consumed": consumed,
        "real_external_call_executed": False,
    }


def _emit_identity_resolved(
    *,
    job,
    queue_id: int,
    unionid: str,
    identity_map_id: int,
    source_type: str,
) -> dict[str, Any]:
    if not unionid:
        return {"ok": False, "reason": "unionid_missing"}
    emitted = InternalEventService().emit_event(
        event_type=IDENTITY_RESOLVED_EVENT_TYPE,
        aggregate_type="crm_user_identity_resolution_queue",
        aggregate_id=str(queue_id),
        subject_type="unionid",
        subject_id=unionid,
        idempotency_key=f"identity.resolved:queue:{queue_id}:effect:{int(job.id)}",
        source_module="aicrm_next.channels.channel_entry.identity_external_effect",
        source_command_id=str(int(job.id)),
        payload={
            "queue_id": queue_id,
            "identity_map_id": identity_map_id,
            "unionid": unionid,
            "source_type": source_type,
        },
        payload_summary={
            "queue_id": queue_id,
            "identity_map_id": identity_map_id,
            "unionid_present": True,
            "source_type": source_type,
        },
        context=CommandContext(
            actor_id="identity_resolution_continuation",
            actor_type="system",
            source_route="external_effect.completed/identity_resolution",
            request_id=str(int(job.id)),
            trace_id=_text(job.trace_id),
        ),
        execution_id=f"exe_identity_resolved_{uuid4().hex}",
        parent_execution_id=_text(job.execution_id),
    )
    return {"ok": True, "event": emitted.get("event") or {}}


def _persist_completion_receipt(
    *,
    job,
    attempt_id: str,
    queue_id: int,
    result_status: str,
    result: dict[str, Any],
) -> bool:
    with get_session_factory()() as session:
        receipt_created = build_identity_resolution_queue_port().complete_sqlalchemy(
            session,
            CompleteIdentityResolutionRequest(
                job_id=int(job.id),
                attempt_id=attempt_id,
                queue_id=queue_id,
                result_status=result_status,
                result_summary_json=_json_summary(result),
                execution_id=f"exe_identity_completion_{uuid4().hex}",
                parent_execution_id=_text(job.execution_id),
                resolved_unionid=_text(result.get("unionid")),
                conflict_reason=(
                    _text(result.get("reason")) or "identity_conflict" if result_status == "conflict" else ""
                ),
            ),
        )
        session.commit()
        return receipt_created


def _consume_provider_result(attempt_id: str, *, job_id: int) -> bool:
    repository = build_external_effect_repository()
    consume = getattr(repository, "consume_attempt_provider_result", None)
    return bool(consume(attempt_id, job_id=job_id)) if callable(consume) else False


def _json_summary(result: dict[str, Any]) -> str:
    import json

    return json.dumps(
        {
            "status": _text(result.get("status")),
            "identity_map_id": int(result.get("identity_map_id") or 0),
            "unionid_present": bool(_text(result.get("unionid"))),
            "openid_present": bool(result.get("openid_present")),
            "provider_result_applied": bool(result.get("provider_result_applied")),
            "profile_description_status": _text((result.get("profile_description") or {}).get("status")),
            "profile_description_job_present": _positive_int(
                (result.get("profile_description") or {}).get("external_effect_job_id")
            )
            > 0,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


IDENTITY_EXTERNAL_CONTACT_DETAIL_CONTINUATION = ExternalEffectContinuation(
    name="identity_external_contact_detail_continuation",
    matches=_matches,
    run=_run,
    requires_provider_result=True,
)

IDENTITY_EXTERNAL_EFFECT_SETTLEMENT_CONTINUATION = ExternalEffectContinuation(
    name="identity_external_effect_settlement",
    matches=_matches_terminal_identity,
    run=_settle_terminal_identity,
)


__all__ = [
    "IDENTITY_EXTERNAL_CONTACT_DETAIL_CONTINUATION",
    "IDENTITY_EXTERNAL_EFFECT_SETTLEMENT_CONTINUATION",
    "IDENTITY_RESOLVED_EVENT_TYPE",
    "plan_profile_description_update",
]
