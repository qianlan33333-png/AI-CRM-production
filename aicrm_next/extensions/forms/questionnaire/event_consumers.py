from __future__ import annotations

from typing import Any

from aicrm_next.crm.customer_tags.local_projection import validate_questionnaire_tag_ids
from aicrm_next.platform.platform_foundation.command_bus import CommandContext
from aicrm_next.platform.platform_foundation.external_effects import (
    WEBHOOK_QUESTIONNAIRE_SUBMISSION_PUSH,
    WECOM_CONTACT_TAG_MARK,
    ExternalEffectService,
)
from aicrm_next.platform.platform_foundation.internal_events.models import (
    InternalEvent,
    InternalEventConsumerResult,
    InternalEventConsumerRun,
)

from .external_push import (
    build_questionnaire_external_effect_payload,
    build_questionnaire_external_push_payload,
)
from .repo import build_questionnaire_repository


def _text(value: Any) -> str:
    return str(value or "").strip()


def _event_submission(event: InternalEvent) -> dict[str, Any]:
    payload = dict(event.payload_json or {})
    raw = payload.get("submission")
    return dict(raw or {}) if isinstance(raw, dict) else {}


def _event_questionnaire(event: InternalEvent) -> dict[str, Any]:
    payload = dict(event.payload_json or {})
    raw = payload.get("questionnaire")
    return dict(raw or {}) if isinstance(raw, dict) else {}


def _event_answer_snapshots(event: InternalEvent) -> list[dict[str, Any]]:
    payload = dict(event.payload_json or {})
    raw = payload.get("answer_snapshots")
    return [dict(item) for item in list(raw or []) if isinstance(item, dict)]


def _load_authoritative_context(
    event: InternalEvent,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], str]:
    submission_id = _text(event.aggregate_id or _event_submission(event).get("submission_id"))
    if submission_id and not submission_id.isdigit():
        return (
            _event_questionnaire(event),
            _event_submission(event),
            _event_answer_snapshots(event),
            "",
        )
    try:
        repository = build_questionnaire_repository()
        submission = repository.get_submission_by_record_id(submission_id)
        if submission:
            questionnaire = repository.get_questionnaire(int(submission.get("questionnaire_id") or 0))
            if questionnaire:
                return (
                    dict(questionnaire),
                    dict(submission),
                    [dict(item) for item in list(submission.get("answer_snapshots") or [])],
                    "",
                )
            return {}, {}, [], "questionnaire_not_found"
        if getattr(repository, "read_model_status", "") != "fixture":
            return {}, {}, [], "submission_not_found"
    except Exception as exc:
        return {}, {}, [], f"questionnaire_reload_failed:{exc.__class__.__name__}"

    questionnaire = _event_questionnaire(event)
    submission = _event_submission(event)
    return questionnaire, submission, _event_answer_snapshots(event), ""


def _retryable_context_failure(
    event: InternalEvent,
    run: InternalEventConsumerRun,
    error: str,
) -> InternalEventConsumerResult:
    return InternalEventConsumerResult(
        status="failed_retryable",
        request_summary={
            "event_id": event.event_id,
            "consumer_name": run.consumer_name,
            "submission_id": _text(event.aggregate_id),
        },
        response_summary={"authoritative_context_loaded": False},
        error_code=error.split(":", 1)[0] or "questionnaire_reload_failed",
        error_message=error[:500] or "questionnaire authoritative context is unavailable",
        retry_after_seconds=300,
    )


def _consumer_context(event: InternalEvent, consumer_name: str) -> CommandContext:
    return CommandContext(
        actor_id="internal_event_consumer",
        actor_type="system",
        request_id=event.request_id,
        trace_id=event.trace_id,
        source_route=f"/internal-events/questionnaire.submitted/{consumer_name}",
    )


def questionnaire_projection_consumer(
    event: InternalEvent,
    run: InternalEventConsumerRun,
) -> InternalEventConsumerResult:
    _questionnaire, submission, _answers, error = _load_authoritative_context(event)
    if error:
        return _retryable_context_failure(event, run, error)
    submission_id = _text(submission.get("submission_id") or event.aggregate_id)
    if not submission_id:
        return _retryable_context_failure(event, run, "submission_id_missing")
    return InternalEventConsumerResult(
        status="succeeded",
        request_summary={"event_id": event.event_id, "submission_id": submission_id},
        response_summary={"submission_found": True, "authoritative_context_loaded": True},
        result_summary={"submission_id": submission_id, "questionnaire_projection": "submitted_confirmed"},
    )


def _external_push_config(questionnaire: dict[str, Any]) -> dict[str, Any]:
    raw = questionnaire.get("external_push_config")
    return dict(raw or {}) if isinstance(raw, dict) else {}


def _external_push_enabled(questionnaire: dict[str, Any]) -> bool:
    config = _external_push_config(questionnaire)
    return bool(config.get("enabled") or questionnaire.get("external_push_enabled"))


def _external_push_url(questionnaire: dict[str, Any]) -> str:
    config = _external_push_config(questionnaire)
    return _text(config.get("webhook_url") or questionnaire.get("external_push_url"))


def questionnaire_webhook_consumer(
    event: InternalEvent,
    run: InternalEventConsumerRun,
) -> InternalEventConsumerResult:
    questionnaire, submission, answers, error = _load_authoritative_context(event)
    if error:
        return _retryable_context_failure(event, run, error)
    submission_id = _text(submission.get("submission_id") or event.aggregate_id)
    questionnaire_id = _text(questionnaire.get("id") or submission.get("questionnaire_id"))
    if not submission_id or not questionnaire_id:
        return _retryable_context_failure(event, run, "questionnaire_identity_missing")
    target_url = _external_push_url(questionnaire)
    if not _external_push_enabled(questionnaire) or not target_url:
        return InternalEventConsumerResult(
            status="skipped",
            request_summary={"event_id": event.event_id, "submission_id": submission_id},
            response_summary={
                "skipped": True,
                "reason": "questionnaire_external_push_not_configured",
                "external_effect_job_created": False,
            },
            result_summary={"reason": "questionnaire_external_push_not_configured"},
        )
    body = build_questionnaire_external_push_payload(
        questionnaire=questionnaire,
        submission=submission,
        computed_result={"answer_snapshots": answers},
    )
    effect_payload = build_questionnaire_external_effect_payload(
        questionnaire=questionnaire,
        submission=submission,
        computed_result={"answer_snapshots": answers},
        target_url=target_url,
        body=body,
    )
    try:
        effects = ExternalEffectService()
        existing = effects.find_existing_job(
            effect_type=WEBHOOK_QUESTIONNAIRE_SUBMISSION_PUSH,
            target_type="questionnaire_submission",
            target_id=submission_id,
            business_type="questionnaire",
            business_id=questionnaire_id,
        )
        if existing is not None:
            return _effect_result(event, submission_id, existing.id, existing.status, created=False)
        job = effects.plan_effect(
            effect_type=WEBHOOK_QUESTIONNAIRE_SUBMISSION_PUSH,
            adapter_name="outbound_webhook",
            operation="post",
            target_type="questionnaire_submission",
            target_id=submission_id,
            business_type="questionnaire",
            business_id=questionnaire_id,
            payload={**effect_payload, "internal_event_id": event.event_id},
            payload_summary={
                "questionnaire_id": int(questionnaire_id or 0),
                "submission_id": submission_id,
                "answer_count": len(body.get("answers") or []),
                "target_url_present": True,
                "internal_event_id": event.event_id,
            },
            context=_consumer_context(event, run.consumer_name),
            source_module="questionnaire.event_consumers",
            source_event_id=event.event_id,
            source_command_id=event.source_command_id,
            risk_level="medium",
            requires_approval=False,
            execution_mode="execute",
            status="queued",
            idempotency_key=(f"questionnaire.submitted:{submission_id}:external-effect:{WEBHOOK_QUESTIONNAIRE_SUBMISSION_PUSH}"),
        )
    except Exception as exc:
        return InternalEventConsumerResult(
            status="failed_retryable",
            request_summary={"event_id": event.event_id, "submission_id": submission_id},
            response_summary={"external_effect_job_created": False},
            error_code="external_push_plan_failed",
            error_message=str(exc)[:500],
            retry_after_seconds=300,
        )
    return _effect_result(
        event,
        submission_id,
        int(job.get("id") or 0),
        _text(job.get("status")),
        created=True,
    )


def _effect_result(
    event: InternalEvent,
    submission_id: str,
    job_id: int,
    job_status: str,
    *,
    created: bool,
) -> InternalEventConsumerResult:
    return InternalEventConsumerResult(
        status="succeeded",
        request_summary={"event_id": event.event_id, "submission_id": submission_id},
        response_summary={
            "external_effect_job_created": created,
            "external_effect_job_reused": not created,
            "external_effect_job_id": job_id,
            "effect_type": WEBHOOK_QUESTIONNAIRE_SUBMISSION_PUSH,
            "status": job_status,
            "real_external_call_executed": False,
        },
        result_summary={
            "external_effect_job_id": job_id,
            "external_effect_job_reused": not created,
            "effect_type": WEBHOOK_QUESTIONNAIRE_SUBMISSION_PUSH,
        },
    )


def questionnaire_tag_consumer(
    event: InternalEvent,
    run: InternalEventConsumerRun,
) -> InternalEventConsumerResult:
    questionnaire, submission, _answers, error = _load_authoritative_context(event)
    if error:
        return _retryable_context_failure(event, run, error)
    submission_id = _text(submission.get("submission_id") or event.aggregate_id)
    questionnaire_id = _text(questionnaire.get("id") or submission.get("questionnaire_id"))
    if not submission_id or not questionnaire_id:
        return _retryable_context_failure(event, run, "questionnaire_identity_missing")
    try:
        validation = validate_questionnaire_tag_ids(list(submission.get("final_tags") or []))
    except Exception as exc:
        return _retryable_context_failure(event, run, f"tag_validation_failed:{exc.__class__.__name__}")
    tag_ids = [_text(tag_id) for tag_id in list(validation.get("tag_ids") or []) if _text(tag_id)]
    if tag_ids and validation.get("ok") is False:
        return InternalEventConsumerResult(
            status="failed_terminal",
            request_summary={
                "event_id": event.event_id,
                "submission_id": submission_id,
                "tag_count": len(tag_ids),
            },
            response_summary={
                "external_effect_job_created": False,
                "reason": "questionnaire_tag_config_invalid",
                "invalid_tag_count": len(validation.get("invalid_tag_ids") or []),
            },
            error_code="questionnaire_tag_config_invalid",
            error_message="questionnaire final tags are not present in the authoritative tag catalog",
        )
    if not tag_ids:
        return InternalEventConsumerResult(
            status="skipped",
            request_summary={"event_id": event.event_id, "submission_id": submission_id},
            response_summary={"skipped": True, "reason": "questionnaire_tags_not_configured"},
            result_summary={"reason": "questionnaire_tags_not_configured"},
        )
    unionid = _text(submission.get("unionid"))
    customer_id = _text(submission.get("customer_id"))
    external_userid = _text(submission.get("external_userid"))
    owner_userid = _text(submission.get("follow_user_userid"))
    missing = [
        name
        for name, value in (
            ("customer_id", customer_id),
            ("external_userid", external_userid),
            ("follow_user_userid", owner_userid),
        )
        if not value
    ]
    if missing:
        return InternalEventConsumerResult(
            status="failed_retryable",
            request_summary={"event_id": event.event_id, "submission_id": submission_id},
            response_summary={
                "external_effect_job_created": False,
                "missing_identity_fields": missing,
                "unionid_present": bool(unionid),
                "external_userid_present": bool(external_userid),
                "follow_user_userid_present": bool(owner_userid),
            },
            error_code="identity_pending" if "customer_id" in missing else "tag_identity_incomplete",
            error_message="questionnaire tag execution is waiting for canonical identity",
            retry_after_seconds=300,
        )
    try:
        effects = ExternalEffectService()
        existing = effects.find_existing_job(
            effect_type=WECOM_CONTACT_TAG_MARK,
            target_type="external_userid",
            target_id=external_userid,
            business_type="questionnaire_submission",
            business_id=submission_id,
        )
        if existing is not None:
            return _tag_effect_result(event, submission_id, existing.id, existing.status, created=False)
        job = effects.plan_effect(
            effect_type=WECOM_CONTACT_TAG_MARK,
            adapter_name="wecom_tag",
            operation="tag_mark",
            target_type="external_userid",
            target_id=external_userid,
            business_type="questionnaire_submission",
            business_id=submission_id,
            payload={
                "target_unionid": unionid,
                "target_customer_id": int(customer_id),
                "external_userid": external_userid,
                "follow_user_userid": owner_userid,
                "tag_ids": tag_ids,
                "questionnaire_id": int(questionnaire_id or 0),
                "submission_id": submission_id,
                "projection": {
                    "type": "questionnaire_contact_tags",
                    "source": "questionnaire_internal_event",
                },
            },
            payload_summary={
                "questionnaire_id": int(questionnaire_id or 0),
                "submission_id": submission_id,
                "tag_count": len(tag_ids),
                "unionid_present": bool(unionid),
                "customer_id_present": True,
                "external_userid_present": True,
                "follow_user_userid_present": True,
            },
            context=_consumer_context(event, run.consumer_name),
            source_module="questionnaire.event_consumers",
            source_event_id=event.event_id,
            source_command_id=event.source_command_id,
            risk_level="high",
            requires_approval=False,
            execution_mode="execute",
            status="queued",
            idempotency_key=(f"questionnaire.submitted:{submission_id}:external-effect:{WECOM_CONTACT_TAG_MARK}"),
        )
    except Exception as exc:
        return InternalEventConsumerResult(
            status="failed_retryable",
            request_summary={"event_id": event.event_id, "submission_id": submission_id},
            response_summary={"external_effect_job_created": False},
            error_code="tag_effect_plan_failed",
            error_message=str(exc)[:500],
            retry_after_seconds=300,
        )
    return _tag_effect_result(
        event,
        submission_id,
        int(job.get("id") or 0),
        _text(job.get("status")),
        created=True,
    )


def _tag_effect_result(
    event: InternalEvent,
    submission_id: str,
    job_id: int,
    job_status: str,
    *,
    created: bool,
) -> InternalEventConsumerResult:
    return InternalEventConsumerResult(
        status="succeeded",
        request_summary={"event_id": event.event_id, "submission_id": submission_id},
        response_summary={
            "external_effect_job_created": created,
            "external_effect_job_reused": not created,
            "external_effect_job_id": job_id,
            "effect_type": WECOM_CONTACT_TAG_MARK,
            "status": job_status,
            "wecom_api_called": False,
            "real_external_call_executed": False,
        },
        result_summary={
            "external_effect_job_id": job_id,
            "external_effect_job_reused": not created,
            "effect_type": WECOM_CONTACT_TAG_MARK,
        },
    )


def automation_questionnaire_consumer(
    event: InternalEvent,
    run: InternalEventConsumerRun,
) -> InternalEventConsumerResult:
    return InternalEventConsumerResult(
        status="skipped",
        request_summary={"event_id": event.event_id, "consumer_name": run.consumer_name},
        response_summary={"skipped": True, "reason": "automation_questionnaire_not_configured"},
        result_summary={"reason": "automation_questionnaire_not_configured"},
    )


def customer_summary_consumer(
    event: InternalEvent,
    run: InternalEventConsumerRun,
) -> InternalEventConsumerResult:
    return InternalEventConsumerResult(
        status="skipped",
        request_summary={"event_id": event.event_id, "consumer_name": run.consumer_name},
        response_summary={"skipped": True, "reason": "customer_summary_not_configured"},
        result_summary={"reason": "customer_summary_not_configured"},
    )


__all__ = [
    "automation_questionnaire_consumer",
    "customer_summary_consumer",
    "questionnaire_projection_consumer",
    "questionnaire_tag_consumer",
    "questionnaire_webhook_consumer",
]
