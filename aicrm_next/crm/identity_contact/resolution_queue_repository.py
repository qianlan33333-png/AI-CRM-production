from __future__ import annotations

import json
from typing import Any

from psycopg.types.json import Jsonb
from sqlalchemy import text

from .resolution_queue_port import (
    CompleteIdentityResolutionRequest,
    EnqueueIdentityResolutionRequest,
    IdentityResolutionBackfillOutcome,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _payload(value: dict[str, Any]) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str, sort_keys=True)


def _json_safe(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value or {}, ensure_ascii=False, default=str))


def _result_error(result: dict[str, Any]) -> str:
    return (
        _text(result.get("reason"))
        or _text(result.get("message"))
        or _text(result.get("status"))
        or "identity_resolution_failed"
    )[:500]


class PostgresIdentityResolutionQueueRepository:
    def enqueue_dbapi(
        self,
        connection: Any,
        request: EnqueueIdentityResolutionRequest,
    ) -> dict[str, Any]:
        self._validate(request)
        row = connection.execute(
            """
            INSERT INTO crm_user_identity_resolution_queue (
                source_type,
                source_key,
                corp_id,
                external_userid,
                openid,
                mobile,
                payload_json,
                reason,
                status,
                first_seen_at,
                last_seen_at,
                created_at,
                updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s::jsonb, %s, 'pending',
                NOW(), NOW(), NOW(), NOW()
            )
            ON CONFLICT (source_type, source_key)
            WHERE status = 'pending' AND source_type <> '' AND source_key <> ''
            DO UPDATE SET
                corp_id = COALESCE(NULLIF(EXCLUDED.corp_id, ''), crm_user_identity_resolution_queue.corp_id),
                external_userid = COALESCE(NULLIF(EXCLUDED.external_userid, ''), crm_user_identity_resolution_queue.external_userid),
                openid = COALESCE(NULLIF(EXCLUDED.openid, ''), crm_user_identity_resolution_queue.openid),
                mobile = COALESCE(NULLIF(EXCLUDED.mobile, ''), crm_user_identity_resolution_queue.mobile),
                payload_json = crm_user_identity_resolution_queue.payload_json || EXCLUDED.payload_json,
                reason = EXCLUDED.reason,
                last_seen_at = NOW(),
                updated_at = NOW()
            RETURNING *
            """,
            (
                _text(request.source_type),
                _text(request.source_key),
                _text(request.corp_id),
                _text(request.external_userid),
                _text(request.openid),
                _text(request.mobile),
                _payload(request.payload_json),
                _text(request.reason) or "identity_unresolved",
            ),
        ).fetchone()
        return self._plan(connection, row, request)

    def enqueue_sqlalchemy(
        self,
        session: Any,
        request: EnqueueIdentityResolutionRequest,
    ) -> dict[str, Any]:
        self._validate(request)
        row = session.execute(
            text(
                """
                INSERT INTO crm_user_identity_resolution_queue (
                    source_type,
                    source_key,
                    corp_id,
                    external_userid,
                    openid,
                    mobile,
                    payload_json,
                    reason,
                    status,
                    first_seen_at,
                    last_seen_at,
                    created_at,
                    updated_at
                ) VALUES (
                    :source_type, :source_key, :corp_id, :external_userid, :openid, :mobile,
                    CAST(:payload_json AS jsonb), :reason, 'pending',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (source_type, source_key)
                WHERE status = 'pending' AND source_type <> '' AND source_key <> ''
                DO UPDATE SET
                    corp_id = COALESCE(NULLIF(EXCLUDED.corp_id, ''), crm_user_identity_resolution_queue.corp_id),
                    external_userid = COALESCE(NULLIF(EXCLUDED.external_userid, ''), crm_user_identity_resolution_queue.external_userid),
                    openid = COALESCE(NULLIF(EXCLUDED.openid, ''), crm_user_identity_resolution_queue.openid),
                    mobile = COALESCE(NULLIF(EXCLUDED.mobile, ''), crm_user_identity_resolution_queue.mobile),
                    payload_json = crm_user_identity_resolution_queue.payload_json || EXCLUDED.payload_json,
                    reason = EXCLUDED.reason,
                    last_seen_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING *
                """
            ),
            {
                "source_type": _text(request.source_type),
                "source_key": _text(request.source_key),
                "corp_id": _text(request.corp_id),
                "external_userid": _text(request.external_userid),
                "openid": _text(request.openid),
                "mobile": _text(request.mobile),
                "payload_json": _payload(request.payload_json),
                "reason": _text(request.reason) or "identity_unresolved",
            },
        ).mappings().one()
        return self._plan(session, row, request)

    def claim_due_dbapi(
        self,
        connection: Any,
        *,
        limit: int,
        locked_by: str,
        lease_seconds: int,
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit or 100), 500))
        bounded_lease_seconds = max(30, min(int(lease_seconds or 600), 3600))
        rows = connection.execute(
            """
            WITH due AS (
                SELECT id
                FROM crm_user_identity_resolution_queue
                WHERE external_effect_job_id IS NULL
                  AND (
                      (
                          status = 'pending'
                          AND (next_attempt_at IS NULL OR next_attempt_at <= CURRENT_TIMESTAMP)
                      )
                      OR (
                          status = 'failed'
                          AND enrichment_status = 'pending'
                          AND last_enrichment_attempt_at <= CURRENT_TIMESTAMP - INTERVAL '24 hours'
                      )
                  )
                ORDER BY COALESCE(next_attempt_at, first_seen_at, created_at) ASC, id ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            UPDATE crm_user_identity_resolution_queue q
            SET status = 'pending',
                completed_at = NULL,
                attempts = COALESCE(attempts, 0) + 1,
                attempt_count = COALESCE(attempt_count, 0) + 1,
                last_seen_at = CURRENT_TIMESTAMP,
                next_attempt_at = CURRENT_TIMESTAMP + make_interval(secs => %s),
                updated_at = CURRENT_TIMESTAMP,
                payload_json = payload_json || %s
            FROM due
            WHERE q.id = due.id
            RETURNING q.*
            """,
            (
                bounded_limit,
                bounded_lease_seconds,
                Jsonb(
                    {
                        "identity_backfill_claim": {
                            "locked_by": _text(locked_by) or "identity-resolution-worker",
                            "lease_seconds": bounded_lease_seconds,
                        }
                    }
                ),
            ),
        ).fetchall()
        return [dict(row) for row in rows or []]

    def record_backfill_result_dbapi(
        self,
        connection: Any,
        *,
        queue_id: int,
        outcome: IdentityResolutionBackfillOutcome,
        result: dict[str, Any],
    ) -> None:
        normalized_queue_id = int(queue_id or 0)
        if normalized_queue_id <= 0:
            raise ValueError("identity resolution queue_id is required")
        summary = Jsonb({"identity_backfill_result": _json_safe(result)})
        if outcome == "resolved":
            connection.execute(
                """
                UPDATE crm_user_identity_resolution_queue
                SET status = 'resolved',
                    enrichment_status = 'resolved',
                    resolved_unionid = %s,
                    resolved_at = CURRENT_TIMESTAMP,
                    last_error = '',
                    payload_json = payload_json || %s,
                    next_attempt_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (_text(result.get("unionid")), summary, normalized_queue_id),
            )
            return
        if outcome == "retryable":
            connection.execute(
                """
                UPDATE crm_user_identity_resolution_queue
                SET status = 'pending',
                    enrichment_status = 'pending',
                    last_enrichment_attempt_at = CURRENT_TIMESTAMP,
                    last_error = %s,
                    payload_json = payload_json || %s,
                    next_attempt_at = CURRENT_TIMESTAMP + make_interval(mins => CASE COALESCE(attempts, 1)
                        WHEN 1 THEN 1
                        WHEN 2 THEN 5
                        WHEN 3 THEN 30
                        WHEN 4 THEN 120
                        ELSE 1440
                    END),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (_result_error(result), summary, normalized_queue_id),
            )
            return
        if outcome == "failed":
            connection.execute(
                """
                UPDATE crm_user_identity_resolution_queue
                SET status = 'failed',
                    enrichment_status = 'pending',
                    last_enrichment_attempt_at = CURRENT_TIMESTAMP,
                    last_error = %s,
                    payload_json = payload_json || %s,
                    next_attempt_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (_result_error(result), summary, normalized_queue_id),
            )
            return
        raise ValueError(f"unsupported identity resolution backfill outcome: {outcome}")

    def get_queue_sqlalchemy(self, session: Any, *, queue_id: int) -> dict[str, Any] | None:
        row = session.execute(
            text("SELECT * FROM crm_user_identity_resolution_queue WHERE id = :queue_id"),
            {"queue_id": int(queue_id or 0)},
        ).mappings().fetchone()
        return dict(row) if row else None

    def get_completion_receipt_sqlalchemy(
        self,
        session: Any,
        *,
        external_effect_job_id: int,
    ) -> dict[str, Any] | None:
        row = session.execute(
            text(
                """
                SELECT *
                FROM identity_resolution_completion_receipt
                WHERE external_effect_job_id = :job_id
                LIMIT 1
                """
            ),
            {"job_id": int(external_effect_job_id or 0)},
        ).mappings().fetchone()
        return dict(row) if row else None

    def settle_terminal_sqlalchemy(
        self,
        session: Any,
        *,
        queue_id: int,
        external_effect_job_id: int,
        status: str,
        error_code: str,
    ) -> bool:
        normalized_status = _text(status)
        if normalized_status not in {"held", "failed", "ignored"}:
            raise ValueError("unsupported identity resolution terminal status")
        row_id = session.execute(
            text(
                """
                UPDATE crm_user_identity_resolution_queue
                SET status = :status,
                    enrichment_status = CASE
                        WHEN :status = 'held' AND :last_error = 'config_missing'
                            THEN 'capability_unavailable'
                        ELSE enrichment_status
                    END,
                    last_error = :last_error,
                    next_attempt_at = NULL,
                    hold_reason = CASE WHEN :status = 'held' THEN :last_error ELSE '' END,
                    held_at = CASE WHEN :status = 'held' THEN COALESCE(held_at, CURRENT_TIMESTAMP) ELSE held_at END,
                    completed_at = CASE
                        WHEN :status IN ('failed', 'ignored') THEN COALESCE(completed_at, CURRENT_TIMESTAMP)
                        ELSE completed_at
                    END,
                    row_version = row_version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :queue_id
                  AND external_effect_job_id = :job_id
                  AND status IN ('pending', 'polling')
                RETURNING id
                """
            ),
            {
                "status": normalized_status,
                "last_error": _text(error_code) or "external_effect_terminal",
                "queue_id": int(queue_id or 0),
                "job_id": int(external_effect_job_id or 0),
            },
        ).scalar_one_or_none()
        return bool(row_id)

    def complete_sqlalchemy(
        self,
        session: Any,
        request: CompleteIdentityResolutionRequest,
    ) -> bool:
        if request.result_status not in {"resolved", "conflict", "pending", "ignored"}:
            raise ValueError("unsupported identity resolution completion status")
        receipt = session.execute(
            text(
                """
                INSERT INTO identity_resolution_completion_receipt (
                    external_effect_job_id, attempt_id, queue_id, result_status,
                    result_summary_json, execution_id, parent_execution_id, created_at
                ) VALUES (
                    :job_id, :attempt_id, :queue_id, :result_status,
                    CAST(:result_summary AS jsonb), :execution_id, :parent_execution_id,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT (external_effect_job_id) DO NOTHING
                RETURNING id
                """
            ),
            {
                "job_id": int(request.job_id or 0),
                "attempt_id": _text(request.attempt_id),
                "queue_id": int(request.queue_id or 0),
                "result_status": request.result_status,
                "result_summary": _text(request.result_summary_json) or "{}",
                "execution_id": _text(request.execution_id),
                "parent_execution_id": _text(request.parent_execution_id),
            },
        ).fetchone()
        if not receipt:
            return False
        if request.result_status == "pending":
            session.execute(
                text(
                    """
                    UPDATE crm_user_identity_resolution_queue
                    SET status = CASE WHEN COALESCE(attempts, 0) + 1 >= 5 THEN 'failed' ELSE 'pending' END,
                        enrichment_status = 'pending',
                        attempts = COALESCE(attempts, 0) + 1,
                        attempt_count = COALESCE(attempt_count, 0) + 1,
                        external_effect_job_id = NULL,
                        last_enrichment_attempt_at = CURRENT_TIMESTAMP,
                        last_error = 'missing_unionid',
                        next_attempt_at = CASE
                            WHEN COALESCE(attempts, 0) + 1 >= 5 THEN NULL
                            ELSE CURRENT_TIMESTAMP + make_interval(mins => CASE COALESCE(attempts, 0) + 1
                                WHEN 1 THEN 1
                                WHEN 2 THEN 5
                                WHEN 3 THEN 30
                                WHEN 4 THEN 120
                                ELSE 1440
                            END)
                        END,
                        completed_at = CASE
                            WHEN COALESCE(attempts, 0) + 1 >= 5 THEN CURRENT_TIMESTAMP
                            ELSE completed_at
                        END,
                        row_version = row_version + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :queue_id
                      AND external_effect_job_id = :job_id
                      AND status = 'pending'
                    """
                ),
                {
                    "queue_id": int(request.queue_id or 0),
                    "job_id": int(request.job_id or 0),
                },
            )
            return True
        session.execute(
            text(
                """
                UPDATE crm_user_identity_resolution_queue
                SET status = :status,
                    enrichment_status = CASE
                        WHEN :status = 'ignored' THEN 'not_applicable'
                        ELSE :status
                    END,
                    resolved_unionid = :resolved_unionid,
                    conflict_reason = :conflict_reason,
                    completed_at = CURRENT_TIMESTAMP,
                    resolved_at = CASE WHEN :status = 'resolved' THEN CURRENT_TIMESTAMP ELSE resolved_at END,
                    last_error = '',
                    row_version = row_version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :queue_id
                  AND external_effect_job_id = :job_id
                  AND status = 'pending'
                """
            ),
            {
                "queue_id": int(request.queue_id or 0),
                "job_id": int(request.job_id or 0),
                "status": request.result_status,
                "resolved_unionid": _text(request.resolved_unionid),
                "conflict_reason": _text(request.conflict_reason),
            },
        )
        return True

    def reopen_pre_provider_dbapi(
        self,
        connection: Any,
        *,
        queue_ids: list[int],
        external_effect_job_ids: list[int],
    ) -> list[int]:
        normalized_queue_ids = [int(value) for value in queue_ids]
        normalized_job_ids = [int(value) for value in external_effect_job_ids]
        if len(normalized_queue_ids) != len(normalized_job_ids):
            raise ValueError("identity resolution queue/job link counts must match")
        if not normalized_queue_ids:
            return []
        rows = connection.execute(
            """
            WITH expected AS (
                SELECT queue_id, job_id
                FROM UNNEST(%s::BIGINT[], %s::BIGINT[]) AS links(queue_id, job_id)
            )
            UPDATE crm_user_identity_resolution_queue queue
            SET status = 'pending',
                hold_reason = '',
                held_at = NULL,
                next_attempt_at = CURRENT_TIMESTAMP,
                last_error = '',
                completed_at = NULL,
                row_version = row_version + 1,
                updated_at = CURRENT_TIMESTAMP
            FROM expected
            WHERE queue.id = expected.queue_id
              AND queue.external_effect_job_id = expected.job_id
              AND queue.status IN ('pending', 'held')
            RETURNING queue.id
            """,
            (normalized_queue_ids, normalized_job_ids),
        ).fetchall()
        return [int(row["id"]) for row in rows or []]

    @staticmethod
    def _validate(request: EnqueueIdentityResolutionRequest) -> None:
        if not _text(request.source_type):
            raise ValueError("identity resolution source_type is required")
        if not _text(request.source_key):
            raise ValueError("identity resolution source_key is required")
        if not _text(request.source_route):
            raise ValueError("identity resolution source_route is required")

    @staticmethod
    def _plan(
        connection: Any,
        row: Any,
        request: EnqueueIdentityResolutionRequest,
    ) -> dict[str, Any]:
        if not row:
            return {}
        from .resolution_effects import plan_identity_resolution_effect

        return plan_identity_resolution_effect(
            connection,
            dict(row),
            parent_execution_id=_text(request.parent_execution_id),
            source_route=_text(request.source_route),
        )


__all__ = ["PostgresIdentityResolutionQueueRepository"]
