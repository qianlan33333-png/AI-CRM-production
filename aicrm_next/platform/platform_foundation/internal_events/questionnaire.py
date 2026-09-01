from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aicrm_next.platform.platform_foundation.command_bus import CommandContext

from .consumer_registry import (
    InternalEventConsumerHandler,
    InternalEventConsumerRegistry,
    current_internal_event_consumer_registry,
)
from .models import InternalEventCreateRequest

QUESTIONNAIRE_SUBMITTED_EVENT_TYPE = "questionnaire.submitted"


def build_questionnaire_submitted_event_request(
    *,
    questionnaire: dict[str, Any],
    submission: dict[str, Any],
    answer_snapshots: list[dict[str, Any]],
    context: CommandContext,
    source_command_id: str = "",
) -> InternalEventCreateRequest | None:
    submission_id = _text(submission.get("submission_id") or submission.get("id"))
    questionnaire_id = _text(questionnaire.get("id") or submission.get("questionnaire_id"))
    if not submission_id or not questionnaire_id:
        return None
    unionid = _text(submission.get("unionid"))
    customer_id = _text(submission.get("customer_id"))
    external_push_config = _external_push_config(questionnaire)
    questionnaire_payload = {
        "id": int(questionnaire_id),
        "slug": _text(questionnaire.get("slug") or submission.get("slug")),
        "title": _text(questionnaire.get("title") or questionnaire.get("name")),
        "external_push_enabled": _external_push_enabled(questionnaire),
        "external_push_config": external_push_config,
    }
    submission_payload = {
        "submission_id": submission_id,
        "customer_id": customer_id,
        "respondent_identity_id": _text(submission.get("respondent_identity_id")),
        "questionnaire_id": int(questionnaire_id),
        "slug": _text(submission.get("slug") or questionnaire.get("slug")),
        "respondent_key": _text(submission.get("respondent_key")),
        "external_userid": _text(submission.get("external_userid")),
        "follow_user_userid": _text(submission.get("follow_user_userid")),
        "openid": _text(submission.get("openid")),
        "unionid": unionid,
        "unionid_present": bool(unionid),
        "mobile": _text(submission.get("mobile") or submission.get("mobile_snapshot")),
        "submitted_at": _text(submission.get("submitted_at") or submission.get("created_at")),
        "final_tags": [_text(tag_id) for tag_id in list(submission.get("final_tags") or []) if _text(tag_id)],
    }
    snapshots = [dict(item) for item in answer_snapshots if isinstance(item, dict)]
    return InternalEventCreateRequest(
        event_type=QUESTIONNAIRE_SUBMITTED_EVENT_TYPE,
        event_version=1,
        aggregate_type="questionnaire_submission",
        aggregate_id=submission_id,
        subject_type="customer" if customer_id else "unionid" if unionid else "questionnaire_submission",
        subject_id=customer_id or unionid or submission_id,
        idempotency_key=f"questionnaire.submitted:{submission_id}",
        source_module="questionnaire.h5_write",
        source_command_id=_text(source_command_id) or submission_id,
        correlation_id=_text(context.trace_id) or submission_id,
        context=context,
        payload={
            "questionnaire": questionnaire_payload,
            "submission": submission_payload,
            "answer_snapshots": snapshots,
            "source": {"command_id": _text(source_command_id)},
        },
        payload_summary={
            "submission_id": submission_id,
            "questionnaire_id": int(questionnaire_id),
            "slug": questionnaire_payload["slug"],
            "answer_count": len(snapshots),
            "final_tag_count": len(submission_payload["final_tags"]),
            "unionid_present": bool(unionid),
            "customer_id_present": bool(customer_id),
            "external_push_configured": bool(questionnaire_payload["external_push_enabled"] and _target_url(questionnaire_payload)),
        },
    )


def _text(value: Any) -> str:
    return str(value or "").strip()


def _external_push_config(questionnaire: dict[str, Any]) -> dict[str, Any]:
    config = questionnaire.get("external_push_config")
    return dict(config or {}) if isinstance(config, dict) else {}


def _external_push_enabled(questionnaire: dict[str, Any]) -> bool:
    config = _external_push_config(questionnaire)
    return _bool(config.get("enabled") or questionnaire.get("external_push_enabled"))


def _target_url(questionnaire: dict[str, Any]) -> str:
    config = _external_push_config(questionnaire)
    return _text(config.get("webhook_url") or questionnaire.get("external_push_url"))


def _bool(value: Any) -> bool:
    return str(value if value is not None else "").strip().lower() in {"1", "true", "yes", "on", "t"}


def register_questionnaire_event_consumers(
    registry: InternalEventConsumerRegistry | None = None,
    *,
    handlers: Mapping[str, InternalEventConsumerHandler] | None = None,
) -> None:
    registry = registry or current_internal_event_consumer_registry()
    handlers = dict(handlers or {})
    required = {
        "questionnaire_projection_consumer",
        "questionnaire_webhook_consumer",
        "questionnaire_tag_consumer",
        "automation_questionnaire_consumer",
        "customer_summary_consumer",
    }
    missing = sorted(required - set(handlers))
    if missing:
        raise ValueError(f"questionnaire consumer handlers are required: {', '.join(missing)}")
    registry.register(
        QUESTIONNAIRE_SUBMITTED_EVENT_TYPE,
        "questionnaire_projection_consumer",
        handlers["questionnaire_projection_consumer"],
        consumer_type="projection",
    )
    registry.register(
        QUESTIONNAIRE_SUBMITTED_EVENT_TYPE,
        "questionnaire_webhook_consumer",
        handlers["questionnaire_webhook_consumer"],
        consumer_type="external_effect_planner",
    )
    registry.register(
        QUESTIONNAIRE_SUBMITTED_EVENT_TYPE,
        "questionnaire_tag_consumer",
        handlers["questionnaire_tag_consumer"],
        consumer_type="external_effect_planner",
    )
    registry.register(
        QUESTIONNAIRE_SUBMITTED_EVENT_TYPE,
        "automation_questionnaire_consumer",
        handlers["automation_questionnaire_consumer"],
        consumer_type="orchestration",
    )
    registry.register(
        QUESTIONNAIRE_SUBMITTED_EVENT_TYPE,
        "customer_summary_consumer",
        handlers["customer_summary_consumer"],
        consumer_type="projection",
    )


__all__ = [
    "QUESTIONNAIRE_SUBMITTED_EVENT_TYPE",
    "build_questionnaire_submitted_event_request",
    "register_questionnaire_event_consumers",
]
