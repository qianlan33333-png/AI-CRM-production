from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel


class ResolvePersonIdentityRequest(BaseModel):
    """Channel-owned identity query contract.

    The CRM implementation accepts this structural request without requiring
    the channel domain to import CRM-owned DTOs.
    """

    external_userid: str | None = None
    mobile: str | None = None
    openid: str | None = None
    unionid: str | None = None


class ResolvedIdentity(Protocol):
    unionid: str | None
    customer_name: str | None
    remark: str | None


class IdentityResolveResult(Protocol):
    status: str
    identity: ResolvedIdentity | None
    reason: str


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
    def enqueue_dbapi(self, connection: Any, request: EnqueueIdentityResolutionRequest) -> dict[str, Any]: ...

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

    def complete_sqlalchemy(self, session: Any, request: CompleteIdentityResolutionRequest) -> bool: ...


class IdentityEventLogPort(Protocol):
    def log_external_contact_event(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_external_contact_event_log(self, event_log_id: int) -> dict[str, Any] | None: ...

    def mark_event_status(self, event_log_id: int, status: str, error_message: str = "") -> None: ...

    def record_identity_sync_result(self, event_log_id: int, **kwargs: Any) -> None: ...

    def list_recent_events(self, scene_value: str, limit: int = 20) -> list[dict[str, Any]]: ...


class CustomerTagProjectionPort(Protocol):
    def save_channel_snapshot_dbapi(self, executor: Any, **kwargs: Any) -> dict[str, int]: ...


@dataclass(frozen=True)
class ChannelCrmDependencies:
    resolve_identity_with_dbapi: Callable[..., IdentityResolveResult]
    resolve_external_userid_with_dbapi: Callable[..., str]
    identity_resolution_queue_factory: Callable[[], IdentityResolutionQueuePort]
    identity_event_log_factory: Callable[..., IdentityEventLogPort]
    customer_tag_projection_factory: Callable[[], CustomerTagProjectionPort]
    plan_identity_resolution_effect: Callable[..., dict[str, Any]]
    enqueue_channel_entry_identity_resolution_in_connection: Callable[..., dict[str, Any]]
    ensure_verified_wecom_customer: Callable[..., dict[str, Any]]
    apply_verified_wecom_detail: Callable[..., dict[str, Any]]
    close_verified_wecom_relationship: Callable[..., dict[str, Any]]


_DEPENDENCIES: ChannelCrmDependencies | None = None


def configure_channel_crm_port(dependencies: ChannelCrmDependencies) -> None:
    global _DEPENDENCIES
    _DEPENDENCIES = dependencies


def _dependencies() -> ChannelCrmDependencies:
    if _DEPENDENCIES is None:
        raise RuntimeError("channel_crm_port_not_configured")
    return _DEPENDENCIES


def resolve_identity_with_dbapi(
    executor: Any,
    request: ResolvePersonIdentityRequest,
    *,
    placeholder: str = "%s",
    for_update: bool = False,
) -> IdentityResolveResult:
    return _dependencies().resolve_identity_with_dbapi(
        executor,
        request,
        placeholder=placeholder,
        for_update=for_update,
    )


def resolve_external_userid_with_dbapi(
    executor: Any,
    external_userid: str,
    *,
    placeholder: str = "%s",
    for_update: bool = False,
) -> str:
    return _dependencies().resolve_external_userid_with_dbapi(
        executor,
        external_userid,
        placeholder=placeholder,
        for_update=for_update,
    )


def resolved_unionid(result: IdentityResolveResult) -> str:
    if getattr(result, "status", "") != "resolved":
        return ""
    identity = getattr(result, "identity", None)
    return str(getattr(identity, "unionid", "") or "").strip() if identity is not None else ""


def build_identity_resolution_queue_port() -> IdentityResolutionQueuePort:
    return _dependencies().identity_resolution_queue_factory()


def build_identity_event_log_port(
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> IdentityEventLogPort:
    return _dependencies().identity_event_log_factory(connection_factory=connection_factory)


def build_customer_tag_projection_port() -> CustomerTagProjectionPort:
    return _dependencies().customer_tag_projection_factory()


def plan_identity_resolution_effect(
    connection: Any,
    queue_row: dict[str, Any],
    *,
    parent_execution_id: str = "",
    source_route: str = "identity_resolution.queue.enqueue",
) -> dict[str, Any]:
    return _dependencies().plan_identity_resolution_effect(
        connection,
        queue_row,
        parent_execution_id=parent_execution_id,
        source_route=source_route,
    )


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
    return _dependencies().enqueue_channel_entry_identity_resolution_in_connection(
        connection,
        corp_id=corp_id,
        external_userid=external_userid,
        follow_user_userid=follow_user_userid,
        payload_json=payload_json,
        reason=reason,
        parent_execution_id=parent_execution_id,
        event_log_id=event_log_id,
    )


def ensure_verified_wecom_customer(**kwargs: Any) -> dict[str, Any]:
    return _dependencies().ensure_verified_wecom_customer(**kwargs)


def apply_verified_wecom_detail(**kwargs: Any) -> dict[str, Any]:
    return _dependencies().apply_verified_wecom_detail(**kwargs)


def close_verified_wecom_relationship(**kwargs: Any) -> dict[str, Any]:
    return _dependencies().close_verified_wecom_relationship(**kwargs)


__all__ = [
    "ChannelCrmDependencies",
    "CompleteIdentityResolutionRequest",
    "EnqueueIdentityResolutionRequest",
    "IdentityEventLogPort",
    "IdentityResolutionQueuePort",
    "IdentityResolveResult",
    "ResolvePersonIdentityRequest",
    "build_customer_tag_projection_port",
    "build_identity_event_log_port",
    "build_identity_resolution_queue_port",
    "configure_channel_crm_port",
    "enqueue_channel_entry_identity_resolution_in_connection",
    "ensure_verified_wecom_customer",
    "apply_verified_wecom_detail",
    "close_verified_wecom_relationship",
    "plan_identity_resolution_effect",
    "resolve_external_userid_with_dbapi",
    "resolve_identity_with_dbapi",
    "resolved_unionid",
]
