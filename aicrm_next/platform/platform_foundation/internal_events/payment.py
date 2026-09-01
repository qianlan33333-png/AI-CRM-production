from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Any

from aicrm_next.platform.platform_foundation.external_effects import ExternalEffectJob, ExternalEffectService, WEBHOOK_ORDER_PAID_PUSH
from aicrm_next.platform.platform_foundation.command_bus import CommandContext

from .consumer_registry import (
    InternalEventConsumerHandler,
    InternalEventConsumerRegistry,
    current_internal_event_consumer_registry,
)
from .models import InternalEvent, InternalEventConsumerResult, InternalEventConsumerRun, InternalEventCreateRequest
from .repository import read_wechat_pay_order_for_payment_event

PAYMENT_SUCCEEDED_EVENT_TYPE = "payment.succeeded"
TRANSACTION_PAID_EVENT_ALIAS = "transaction.paid"
PAYMENT_SUCCEEDED_EVENT_ALIAS = "payment_succeeded"
PAYMENT_SUCCEEDED_EVENT_TYPES = (
    PAYMENT_SUCCEEDED_EVENT_TYPE,
    TRANSACTION_PAID_EVENT_ALIAS,
    PAYMENT_SUCCEEDED_EVENT_ALIAS,
)
PAYMENT_SUCCEEDED_CORE_CONSUMERS = (
    "order_projection_consumer",
    "service_period_entitlement_consumer",
    "product_paid_wecom_tag_consumer",
    "webhook_order_paid_consumer",
    "customer_business_summary_consumer",
    "dnd_policy_consumer",
    "ai_assist_notify_consumer",
)
PAYMENT_SUCCEEDED_PRODUCTION_EVENT_CONSUMERS = tuple(f"{PAYMENT_SUCCEEDED_EVENT_TYPE}:{consumer_name}" for consumer_name in PAYMENT_SUCCEEDED_CORE_CONSUMERS)
PaymentIdentityProjector = Callable[..., dict[str, Any]]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _masked_mobile(value: Any) -> str:
    digits = "".join(char for char in _text(value) if char.isdigit())
    return f"{digits[:3]}****{digits[-4:]}" if len(digits) >= 7 else ""


def build_payment_succeeded_event_request(
    *,
    order: dict[str, Any],
    transaction: dict[str, Any],
    domain_event_outbox_id: Any = None,
    source_route: str = "",
) -> InternalEventCreateRequest | None:
    out_trade_no = _text(order.get("out_trade_no") or transaction.get("out_trade_no"))
    aggregate_id = _text(order.get("id") or out_trade_no)
    if not out_trade_no or not aggregate_id:
        return None
    subject_id = (
        _text(order.get("customer_id"))
        or _text(order.get("unionid"))
        or _text(order.get("external_userid"))
        or _text(order.get("userid_snapshot"))
        or _text(order.get("respondent_key"))
    )
    return InternalEventCreateRequest(
        event_type=PAYMENT_SUCCEEDED_EVENT_TYPE,
        event_version=1,
        aggregate_type="wechat_pay_order",
        aggregate_id=aggregate_id,
        subject_type="customer",
        subject_id=subject_id,
        idempotency_key=f"payment.succeeded:{out_trade_no}",
        source_module="public_product.h5_wechat_pay",
        source_command_id=out_trade_no,
        correlation_id=out_trade_no,
        context=CommandContext(
            actor_id="wechat_pay_notify",
            actor_type="system",
            trace_id=out_trade_no,
            request_id=_text(transaction.get("transaction_id")),
            source_route=source_route or "/api/h5/wechat-pay/notify",
        ),
        payload={
            "order": dict(order),
            "transaction": dict(transaction or {}),
            "domain_event_outbox_id": domain_event_outbox_id,
            "legacy_event_aliases": [TRANSACTION_PAID_EVENT_ALIAS, PAYMENT_SUCCEEDED_EVENT_ALIAS],
        },
        payload_summary={
            "out_trade_no": out_trade_no,
            "order_id": order.get("id"),
            "aggregate_id": aggregate_id,
            "subject_type": "customer",
            "subject_id": subject_id,
            "customer_id": order.get("customer_id"),
            "product_code": order.get("product_code"),
            "amount_total": int(order.get("amount_total") or order.get("payer_total") or 0),
            "status": order.get("status"),
            "trade_state": order.get("trade_state"),
            "paid_at": str(order.get("paid_at") or ""),
            "mobile_masked": _masked_mobile(order.get("mobile_snapshot")),
            "domain_event_outbox_id": domain_event_outbox_id,
        },
    )


def _order_from_event(event: InternalEvent) -> dict[str, Any]:
    payload = dict(event.payload_json or {})
    order = payload.get("order") if isinstance(payload.get("order"), dict) else {}
    return dict(order or {})


def _read_order_from_db(event: InternalEvent) -> dict[str, Any]:
    lookup = _text((event.payload_json or {}).get("order", {}).get("out_trade_no") if isinstance((event.payload_json or {}).get("order"), dict) else "")
    aggregate_id = _text(event.aggregate_id)
    return read_wechat_pay_order_for_payment_event(lookup=lookup, aggregate_id=aggregate_id)


def _transaction_from_event(event: InternalEvent) -> dict[str, Any]:
    payload = dict(event.payload_json or {})
    transaction = payload.get("transaction") if isinstance(payload.get("transaction"), dict) else {}
    return dict(transaction or {})


def _is_order_paid(order: dict[str, Any]) -> bool:
    return _text(order.get("status")) == "paid" or _text(order.get("trade_state")) == "SUCCESS"


def _configured_order_paid_job(external_effects: ExternalEffectService, *, out_trade_no: str, order_id: str) -> ExternalEffectJob | None:
    business_ids = [value for value in (out_trade_no, order_id) if value]
    seen: set[int] = set()
    for business_id in business_ids:
        jobs, _ = external_effects.list_jobs(
            {"effect_type": WEBHOOK_ORDER_PAID_PUSH, "business_type": "commerce_order", "business_id": business_id},
            limit=20,
        )
        for job in jobs:
            if job.id in seen:
                continue
            seen.add(job.id)
            payload = job.payload_json or {}
            if payload.get("webhook_url"):
                return job
    return None


def _plan_order_paid_external_push_from_db(
    *,
    order: dict[str, Any],
    transaction: dict[str, Any],
    event: InternalEvent,
    planner: Callable[..., dict[str, Any] | None] | None,
) -> dict[str, Any] | None:
    if planner is None:
        raise RuntimeError("order-paid external push planner composition is required")
    return planner(
        order=order,
        transaction=transaction,
        domain_event_outbox_id=(event.payload_json or {}).get("domain_event_outbox_id"),
    )


def order_projection_consumer(
    event: InternalEvent,
    run: InternalEventConsumerRun,
    *,
    identity_projector: PaymentIdentityProjector | None = None,
) -> InternalEventConsumerResult:
    order = _read_order_from_db(event) or _order_from_event(event)
    out_trade_no = _text(order.get("out_trade_no") or event.aggregate_id)
    if not order:
        return InternalEventConsumerResult(
            status="failed_retryable",
            request_summary={"event_id": event.event_id, "aggregate_id": event.aggregate_id},
            response_summary={"order_found": False},
            error_code="order_payload_missing",
            error_message="payment.succeeded event is missing the order payload",
        )
    if not _is_order_paid(order):
        return InternalEventConsumerResult(
            status="failed_retryable",
            request_summary={"event_id": event.event_id, "out_trade_no": out_trade_no},
            response_summary={"order_found": True, "paid": False, "status": order.get("status"), "trade_state": order.get("trade_state")},
            error_code="order_not_paid",
            error_message="order is not paid yet",
        )
    mobile_projection = {"ok": True, "projected": False, "reason": "identity_projector_not_configured"}
    if identity_projector is not None:
        try:
            mobile_projection = dict(
                identity_projector(
                    order=order,
                    source_route="/internal-events/payment.succeeded/order_projection_consumer",
                )
                or {}
            )
        except Exception:
            return InternalEventConsumerResult(
                status="failed_retryable",
                request_summary={"event_id": event.event_id, "out_trade_no": out_trade_no},
                response_summary={"order_found": True, "paid": True, "mobile_projected": False},
                error_code="payment_identity_projection_failed",
                error_message="canonical payment identity projection failed",
                retry_after_seconds=60,
            )
    projection_reason = _text(mobile_projection.get("reason"))
    if not mobile_projection.get("ok", True) and projection_reason == "projection_error":
        return InternalEventConsumerResult(
            status="failed_retryable",
            request_summary={"event_id": event.event_id, "out_trade_no": out_trade_no},
            response_summary={"order_found": True, "paid": True, "mobile_projected": False},
            error_code="payment_identity_projection_failed",
            error_message="canonical payment identity projection failed",
            retry_after_seconds=60,
        )
    return InternalEventConsumerResult(
        status="succeeded",
        request_summary={"event_id": event.event_id, "out_trade_no": out_trade_no},
        response_summary={
            "order_found": True,
            "paid": True,
            "mobile_projection_attempted": identity_projector is not None,
            "mobile_projected": bool(mobile_projection.get("projected")),
            "mobile_projection_reason": projection_reason,
        },
        result_summary={
            "order_projection": "paid_confirmed",
            "out_trade_no": out_trade_no,
            "mobile_projected": bool(mobile_projection.get("projected")),
            "mobile_projection_reason": projection_reason,
        },
    )


def webhook_order_paid_consumer(
    event: InternalEvent,
    run: InternalEventConsumerRun,
    *,
    external_push_planner: Callable[..., dict[str, Any] | None] | None = None,
) -> InternalEventConsumerResult:
    order = _order_from_event(event)
    transaction = _transaction_from_event(event)
    out_trade_no = _text(order.get("out_trade_no") or transaction.get("out_trade_no") or event.aggregate_id)
    target_id = _text(order.get("id") or out_trade_no)
    if not target_id or not out_trade_no:
        return InternalEventConsumerResult(
            status="failed_retryable",
            request_summary={"event_id": event.event_id, "aggregate_id": event.aggregate_id},
            response_summary={"external_effect_job_created": False},
            error_code="order_identity_missing",
            error_message="order id or out_trade_no is required",
        )
    external_effects = ExternalEffectService()
    existing_job = _configured_order_paid_job(external_effects, out_trade_no=out_trade_no, order_id=_text(order.get("id")))
    if existing_job is not None:
        return InternalEventConsumerResult(
            status="succeeded",
            request_summary={"event_id": event.event_id, "out_trade_no": out_trade_no},
            response_summary={
                "external_effect_job_created": False,
                "external_effect_job_reused": True,
                "external_effect_job_id": existing_job.id,
                "effect_type": WEBHOOK_ORDER_PAID_PUSH,
                "execution_mode": existing_job.execution_mode,
                "status": existing_job.status,
            },
            result_summary={
                "external_effect_job_id": existing_job.id,
                "external_effect_job_reused": True,
                "effect_type": WEBHOOK_ORDER_PAID_PUSH,
            },
        )
    try:
        planned = _plan_order_paid_external_push_from_db(
            order=order,
            transaction=transaction,
            event=event,
            planner=external_push_planner,
        )
    except Exception as exc:
        return InternalEventConsumerResult(
            status="failed_retryable",
            request_summary={"event_id": event.event_id, "out_trade_no": out_trade_no},
            response_summary={"external_effect_job_created": False, "effect_type": WEBHOOK_ORDER_PAID_PUSH},
            error_code="external_push_plan_failed",
            error_message=str(exc)[:500],
            retry_after_seconds=300,
        )
    if planned and not planned.get("ok", True):
        return InternalEventConsumerResult(
            status="failed_retryable",
            request_summary={"event_id": event.event_id, "out_trade_no": out_trade_no},
            response_summary={"external_effect_job_created": False, "effect_type": WEBHOOK_ORDER_PAID_PUSH},
            error_code="external_push_plan_failed",
            error_message=_text(planned.get("reason") or "external push planner returned a failed result"),
            retry_after_seconds=300,
        )
    if not planned or planned.get("skipped"):
        return InternalEventConsumerResult(
            status="succeeded",
            request_summary={"event_id": event.event_id, "out_trade_no": out_trade_no},
            response_summary={
                "external_effect_job_created": False,
                "external_effect_job_reused": False,
                "effect_type": WEBHOOK_ORDER_PAID_PUSH,
                "skipped": True,
                "reason": (planned or {}).get("reason") or "external_push_config_unavailable",
            },
            result_summary={"external_effect_job_created": False, "effect_type": WEBHOOK_ORDER_PAID_PUSH},
        )
    job_id = planned.get("external_effect_job_id")
    return InternalEventConsumerResult(
        status="succeeded",
        request_summary={"event_id": event.event_id, "out_trade_no": out_trade_no},
        response_summary={
            "external_effect_job_created": True,
            "external_effect_job_reused": False,
            "external_effect_job_id": job_id,
            "effect_type": WEBHOOK_ORDER_PAID_PUSH,
            "execution_mode": "execute",
            "status": "queued",
        },
        result_summary={"external_effect_job_id": job_id, "external_effect_job_reused": False, "effect_type": WEBHOOK_ORDER_PAID_PUSH},
    )


def customer_business_summary_consumer(event: InternalEvent, run: InternalEventConsumerRun) -> InternalEventConsumerResult:
    return InternalEventConsumerResult(
        status="skipped",
        request_summary={"event_id": event.event_id},
        response_summary={"skipped": True, "reason": "summary_refresh_not_configured"},
        result_summary={"reason": "summary_refresh_not_configured"},
    )


def dnd_policy_consumer(event: InternalEvent, run: InternalEventConsumerRun) -> InternalEventConsumerResult:
    return InternalEventConsumerResult(
        status="skipped",
        request_summary={"event_id": event.event_id},
        response_summary={"skipped": True, "reason": "dnd_policy_not_configured"},
        result_summary={"reason": "dnd_policy_not_configured"},
    )


def ai_assist_notify_consumer(event: InternalEvent, run: InternalEventConsumerRun) -> InternalEventConsumerResult:
    return InternalEventConsumerResult(
        status="skipped",
        request_summary={"event_id": event.event_id},
        response_summary={"skipped": True, "reason": "ai_assist_notify_not_configured"},
        result_summary={"reason": "ai_assist_notify_not_configured"},
    )


def register_payment_succeeded_consumers(
    registry: InternalEventConsumerRegistry | None = None,
    *,
    payment_identity_projector: PaymentIdentityProjector | None = None,
    service_period_consumer: InternalEventConsumerHandler | None = None,
    product_paid_tag_consumer: InternalEventConsumerHandler | None = None,
    webhook_order_paid_handler: InternalEventConsumerHandler | None = None,
) -> None:
    registry = registry or current_internal_event_consumer_registry()

    for event_type in PAYMENT_SUCCEEDED_EVENT_TYPES:
        registry.register(
            event_type,
            "order_projection_consumer",
            (
                partial(order_projection_consumer, identity_projector=payment_identity_projector)
                if payment_identity_projector is not None
                else order_projection_consumer
            ),
            consumer_type="projection",
        )
        if service_period_consumer is not None:
            registry.register(
                event_type,
                "service_period_entitlement_consumer",
                service_period_consumer,
                consumer_type="projection",
            )
        if product_paid_tag_consumer is not None:
            registry.register(
                event_type,
                "product_paid_wecom_tag_consumer",
                product_paid_tag_consumer,
                consumer_type="external_effect_planner",
            )
        registry.register(
            event_type,
            "webhook_order_paid_consumer",
            webhook_order_paid_handler or webhook_order_paid_consumer,
            consumer_type="external_effect_planner",
        )
        registry.register(event_type, "customer_business_summary_consumer", customer_business_summary_consumer, consumer_type="projection")
        registry.register(event_type, "dnd_policy_consumer", dnd_policy_consumer, consumer_type="orchestration")
        registry.register(event_type, "ai_assist_notify_consumer", ai_assist_notify_consumer, consumer_type="orchestration")
