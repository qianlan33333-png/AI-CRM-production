from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from aicrm_next.platform.platform_foundation.command_bus.models import CommandContext
from aicrm_next.platform.platform_foundation.external_effects import (
    ExternalEffectService,
    WECOM_EXTERNAL_CONTACT_DETAIL_FETCH,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def plan_identity_resolution_effect(
    connection: Any,
    queue_row: dict[str, Any],
    *,
    parent_execution_id: str = "",
    source_route: str = "identity_resolution.queue.enqueue",
) -> dict[str, Any]:
    """Attach one canonical provider-read effect to a caller-owned queue transaction."""

    row = dict(queue_row or {})
    queue_id = int(row.get("id") or 0)
    if queue_id <= 0:
        raise ValueError("identity_resolution_queue_id_required")
    if _text(row.get("status")) == "held":
        return {
            "ok": True,
            "planned": False,
            "held": True,
            "queue_id": queue_id,
            "reason": _text(row.get("hold_reason")) or "historical_identity_resolution_hold",
            "real_external_call_executed": False,
        }
    existing_job_id = int(row.get("external_effect_job_id") or 0)
    if existing_job_id > 0:
        return {
            "ok": True,
            "planned": False,
            "deduplicated": True,
            "queue_id": queue_id,
            "external_effect_job_id": existing_job_id,
            "execution_id": _text(row.get("execution_id")),
            "real_external_call_executed": False,
        }

    external_userid = _text(row.get("external_userid"))
    intent_execution_id = _text(row.get("execution_id")) or f"exe_identity_resolution_{uuid4().hex}"
    source_parent_execution_id = _text(row.get("parent_execution_id")) or _text(parent_execution_id)
    if not external_userid:
        _mark_queue_without_provider_target(
            connection,
            queue_id=queue_id,
            execution_id=intent_execution_id,
            parent_execution_id=source_parent_execution_id,
        )
        return {
            "ok": True,
            "planned": False,
            "held": True,
            "queue_id": queue_id,
            "execution_id": intent_execution_id,
            "reason": "external_userid_required_for_wecom_detail",
            "real_external_call_executed": False,
        }

    payload = _json_object(row.get("payload_json"))
    corp_id = _text(row.get("corp_id") or payload.get("corp_id") or payload.get("ToUserName"))
    owner_userid = _text(payload.get("follow_user_userid") or payload.get("UserID") or payload.get("owner_userid"))
    event_log_id = int(payload.get("event_log_id") or row.get("event_log_id") or 0)
    effect_execution_id = f"exe_identity_provider_{uuid4().hex}"
    job = ExternalEffectService().plan_effect(
        effect_type=WECOM_EXTERNAL_CONTACT_DETAIL_FETCH,
        adapter_name="wecom_external_contact_detail",
        operation="get_external_contact_detail",
        target_type="external_user",
        target_id=external_userid,
        payload={
            "queue_id": queue_id,
            "external_userid": external_userid,
            "corp_id": corp_id,
            "owner_userid": owner_userid,
            "event_log_id": event_log_id,
            "source_type": _text(row.get("source_type")),
        },
        payload_summary={
            "queue_id": queue_id,
            "external_userid_present": True,
            "corp_id_present": bool(corp_id),
            "owner_userid_present": bool(owner_userid),
            "event_log_id": event_log_id,
            "source_type": _text(row.get("source_type")),
            "real_external_call_executed": False,
        },
        context=CommandContext(
            actor_id="identity_resolution_queue",
            actor_type="system",
            source_route=source_route,
            request_id=str(event_log_id or queue_id),
            trace_id=f"identity-resolution-{queue_id}",
        ),
        business_type="identity_resolution_queue",
        business_id=str(queue_id),
        source_module="aicrm_next.crm.identity_contact.resolution_effects",
        source_event_id=str(event_log_id or ""),
        idempotency_key=f"identity-resolution:queue:{queue_id}:provider-detail:v1",
        execution_id=effect_execution_id,
        parent_execution_id=intent_execution_id,
        lane="wecom_interactive",
        ordering_key=f"external_user:{external_userid}",
        fairness_key=corp_id or "wecom_default",
        rate_scope_key=f"wecom:{corp_id or 'default'}:external_contact_detail",
        status="queued",
        execution_mode="execute",
        max_attempts=5,
        connection=connection,
    )
    _link_queue_effect(
        connection,
        queue_id=queue_id,
        external_effect_job_id=int(job.get("id") or 0),
        execution_id=intent_execution_id,
        parent_execution_id=source_parent_execution_id,
    )
    return {
        "ok": True,
        "planned": bool(job.get("created_on_plan")),
        "deduplicated": not bool(job.get("created_on_plan")),
        "queue_id": queue_id,
        "external_effect_job_id": int(job.get("id") or 0),
        "execution_id": intent_execution_id,
        "effect_execution_id": _text(job.get("execution_id")),
        "parent_execution_id": source_parent_execution_id,
        "status": _text(job.get("status")),
        "real_external_call_executed": False,
    }


def enqueue_channel_entry_identity_resolution_in_connection(
    connection: Any,
    *,
    corp_id: str,
    external_userid: str,
    follow_user_userid: str = "",
    payload_json: dict[str, Any] | None = None,
    reason: str = "identity_pending_unionid",
    parent_execution_id: str = "",
    event_log_id: int | None = None,
) -> dict[str, Any]:
    external = _text(external_userid)
    if not external:
        return {"ok": False, "reason": "external_userid_missing", "real_external_call_executed": False}
    event_id = int(event_log_id or 0)
    relationship_key = f"{_text(corp_id)}:{external}:{_text(follow_user_userid)}"
    source_key = f"{relationship_key}:event:{event_id}" if event_id > 0 else relationship_key
    if event_id > 0:
        existing = connection.execute(
            """
            SELECT *
            FROM crm_user_identity_resolution_queue
            WHERE source_type = 'channel_entry'
              AND source_key = %s
            ORDER BY id ASC
            LIMIT 1
            FOR UPDATE
            """,
            (source_key,),
        ).fetchone()
        if existing:
            return plan_identity_resolution_effect(
                connection,
                dict(existing),
                parent_execution_id=parent_execution_id,
                source_route="channel_entry.identity_resolution",
            )
    row = connection.execute(
        """
        INSERT INTO crm_user_identity_resolution_queue (
            source_type, source_key, corp_id, external_userid, payload_json,
            reason, status, first_seen_at, last_seen_at, created_at, updated_at
        ) VALUES (
            'channel_entry', %s, %s, %s, %s, %s, 'pending',
            NOW(), NOW(), NOW(), NOW()
        )
        ON CONFLICT (source_type, source_key)
        WHERE status = 'pending' AND source_type <> '' AND source_key <> ''
        DO UPDATE SET
            corp_id = COALESCE(NULLIF(EXCLUDED.corp_id, ''), crm_user_identity_resolution_queue.corp_id),
            external_userid = COALESCE(NULLIF(EXCLUDED.external_userid, ''), crm_user_identity_resolution_queue.external_userid),
            payload_json = crm_user_identity_resolution_queue.payload_json || EXCLUDED.payload_json,
            reason = EXCLUDED.reason,
            last_seen_at = NOW(),
            updated_at = NOW()
        RETURNING *
        """,
        (
            source_key,
            _text(corp_id),
            external,
            json.dumps(
                {**dict(payload_json or {}), "event_log_id": int(event_log_id or 0)},
                ensure_ascii=False,
                default=str,
                separators=(",", ":"),
            ),
            _text(reason) or "identity_pending_unionid",
        ),
    ).fetchone()
    if row:
        row = connection.execute(
            """
            UPDATE crm_user_identity_resolution_queue queue
            SET customer_id = identity_map.customer_id,
                identity_id = identity_map.identity_id,
                enrichment_status = CASE
                    WHEN queue.status = 'resolved' THEN 'resolved'
                    WHEN queue.status = 'conflict' THEN 'conflict'
                    ELSE 'pending'
                END,
                updated_at = NOW()
            FROM wecom_external_contact_identity_map identity_map
            WHERE queue.id = %s
              AND identity_map.corp_id = queue.corp_id
              AND identity_map.external_userid = queue.external_userid
            RETURNING queue.*
            """,
            (int(row.get("id") or 0),),
        ).fetchone() or row
    return plan_identity_resolution_effect(
        connection,
        dict(row or {}),
        parent_execution_id=parent_execution_id,
        source_route="channel_entry.identity_resolution.enqueue",
    )


def enqueue_sidebar_identity_verification(
    *,
    corp_id: str,
    external_userid: str,
    owner_userid: str,
) -> dict[str, Any]:
    """Queue provider verification for an OAuth/JSSDK-derived sidebar context."""

    from .repo import open_identity_connection

    with open_identity_connection() as connection:
        planned = enqueue_channel_entry_identity_resolution_in_connection(
            connection,
            corp_id=corp_id,
            external_userid=external_userid,
            follow_user_userid=owner_userid,
            payload_json={
                "Event": "change_external_contact",
                "ChangeType": "edit_external_contact",
                "ExternalUserID": external_userid,
                "UserID": owner_userid,
                "source": "sidebar_trusted_context_verification",
            },
            reason="sidebar_relationship_verification_required",
        )
        connection.commit()
        return planned


def _link_queue_effect(
    connection: Any,
    *,
    queue_id: int,
    external_effect_job_id: int,
    execution_id: str,
    parent_execution_id: str,
) -> None:
    if isinstance(connection, Session):
        connection.execute(
            sql_text(
                """
                UPDATE crm_user_identity_resolution_queue
                SET external_effect_job_id = :job_id,
                    execution_id = :execution_id,
                    parent_execution_id = :parent_execution_id,
                    lane = 'wecom_interactive',
                    hold_reason = '',
                    held_at = NULL,
                    row_version = row_version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :queue_id
                  AND status = 'pending'
                  AND external_effect_job_id IS NULL
                """
            ),
            {
                "queue_id": queue_id,
                "job_id": external_effect_job_id,
                "execution_id": execution_id,
                "parent_execution_id": parent_execution_id,
            },
        )
        return
    connection.execute(
        """
        UPDATE crm_user_identity_resolution_queue
        SET external_effect_job_id = %s,
            execution_id = %s,
            parent_execution_id = %s,
            lane = 'wecom_interactive',
            hold_reason = '',
            held_at = NULL,
            row_version = row_version + 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
          AND status = 'pending'
          AND external_effect_job_id IS NULL
        """,
        (external_effect_job_id, execution_id, parent_execution_id, queue_id),
    )


def _mark_queue_without_provider_target(
    connection: Any,
    *,
    queue_id: int,
    execution_id: str,
    parent_execution_id: str,
) -> None:
    if isinstance(connection, Session):
        connection.execute(
            sql_text(
                """
                UPDATE crm_user_identity_resolution_queue
                SET status = 'held',
                    execution_id = :execution_id,
                    parent_execution_id = :parent_execution_id,
                    hold_reason = 'external_userid_required_for_wecom_detail',
                    held_at = CURRENT_TIMESTAMP,
                    next_attempt_at = NULL,
                    row_version = row_version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :queue_id AND status = 'pending'
                """
            ),
            {
                "queue_id": queue_id,
                "execution_id": execution_id,
                "parent_execution_id": parent_execution_id,
            },
        )
        return
    connection.execute(
        """
        UPDATE crm_user_identity_resolution_queue
        SET status = 'held',
            execution_id = %s,
            parent_execution_id = %s,
            hold_reason = 'external_userid_required_for_wecom_detail',
            held_at = CURRENT_TIMESTAMP,
            next_attempt_at = NULL,
            row_version = row_version + 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND status = 'pending'
        """,
        (execution_id, parent_execution_id, queue_id),
    )


__all__ = [
    "enqueue_channel_entry_identity_resolution_in_connection",
    "plan_identity_resolution_effect",
]
