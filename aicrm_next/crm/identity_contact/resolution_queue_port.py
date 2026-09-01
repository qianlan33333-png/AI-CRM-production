from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


IdentityResolutionBackfillOutcome = Literal["resolved", "retryable", "failed"]
IdentityResolutionCompletionStatus = Literal["resolved", "conflict", "pending", "ignored"]


@dataclass(frozen=True)
class CompleteIdentityResolutionRequest:
    job_id: int
    attempt_id: str
    queue_id: int
    result_status: IdentityResolutionCompletionStatus
    result_summary_json: str
    execution_id: str
    parent_execution_id: str = ""
    resolved_unionid: str = ""
    conflict_reason: str = ""


@dataclass(frozen=True)
class EnqueueIdentityResolutionRequest:
    source_type: str
    source_key: str
    reason: str
    source_route: str
    corp_id: str = ""
    external_userid: str = ""
    openid: str = ""
    mobile: str = ""
    payload_json: dict[str, Any] = field(default_factory=dict)
    parent_execution_id: str = ""


class IdentityResolutionQueuePort(Protocol):
    """Public owner port for durable unresolved-identity intents."""

    def enqueue_dbapi(
        self,
        connection: Any,
        request: EnqueueIdentityResolutionRequest,
    ) -> dict[str, Any]: ...

    def enqueue_sqlalchemy(
        self,
        session: Any,
        request: EnqueueIdentityResolutionRequest,
    ) -> dict[str, Any]: ...

    def claim_due_dbapi(
        self,
        connection: Any,
        *,
        limit: int,
        locked_by: str,
        lease_seconds: int,
    ) -> list[dict[str, Any]]: ...

    def record_backfill_result_dbapi(
        self,
        connection: Any,
        *,
        queue_id: int,
        outcome: IdentityResolutionBackfillOutcome,
        result: dict[str, Any],
    ) -> None: ...

    def get_queue_sqlalchemy(self, session: Any, *, queue_id: int) -> dict[str, Any] | None: ...

    def get_completion_receipt_sqlalchemy(
        self,
        session: Any,
        *,
        external_effect_job_id: int,
    ) -> dict[str, Any] | None: ...

    def settle_terminal_sqlalchemy(
        self,
        session: Any,
        *,
        queue_id: int,
        external_effect_job_id: int,
        status: str,
        error_code: str,
    ) -> bool: ...

    def complete_sqlalchemy(
        self,
        session: Any,
        request: CompleteIdentityResolutionRequest,
    ) -> bool: ...

    def reopen_pre_provider_dbapi(
        self,
        connection: Any,
        *,
        queue_ids: list[int],
        external_effect_job_ids: list[int],
    ) -> list[int]: ...


def build_identity_resolution_queue_port() -> IdentityResolutionQueuePort:
    from .resolution_queue_repository import PostgresIdentityResolutionQueueRepository

    return PostgresIdentityResolutionQueueRepository()


__all__ = [
    "CompleteIdentityResolutionRequest",
    "EnqueueIdentityResolutionRequest",
    "IdentityResolutionBackfillOutcome",
    "IdentityResolutionCompletionStatus",
    "IdentityResolutionQueuePort",
    "build_identity_resolution_queue_port",
]
