from __future__ import annotations

from typing import Any

from aicrm_next.platform.platform_foundation.internal_events.models import InternalEvent, InternalEventConsumerResult, InternalEventConsumerRun
from aicrm_next.platform.platform_foundation.internal_events.repository import read_wechat_pay_order_for_payment_event

from .application import GrantOrRenewEntitlementCommand


def _text(value: Any) -> str:
    return str(value or "").strip()


def _payload_order(event: InternalEvent) -> dict[str, Any]:
    payload = dict(event.payload_json or {})
    order = payload.get("order") if isinstance(payload.get("order"), dict) else {}
    return dict(order or {})


def _payload_transaction(event: InternalEvent) -> dict[str, Any]:
    payload = dict(event.payload_json or {})
    transaction = payload.get("transaction") if isinstance(payload.get("transaction"), dict) else {}
    return dict(transaction or {})


def service_period_entitlement_consumer(event: InternalEvent, run: InternalEventConsumerRun) -> InternalEventConsumerResult:
    payload_order = _payload_order(event)
    transaction = _payload_transaction(event)
    out_trade_no = _text(payload_order.get("out_trade_no") or transaction.get("out_trade_no") or event.aggregate_id)
    try:
        order = read_wechat_pay_order_for_payment_event(lookup=out_trade_no, aggregate_id=_text(event.aggregate_id)) or payload_order
    except Exception as exc:
        return InternalEventConsumerResult(
            status="failed_retryable",
            request_summary={"event_id": event.event_id, "out_trade_no": out_trade_no},
            response_summary={"authoritative_order_read": False},
            error_code="order_read_failed",
            error_message=str(exc)[:500],
            retry_after_seconds=300,
        )
    if not order:
        return InternalEventConsumerResult(
            status="failed_retryable",
            request_summary={"event_id": event.event_id, "aggregate_id": event.aggregate_id},
            response_summary={"order_found": False},
            error_code="order_payload_missing",
            error_message="payment.succeeded event is missing order payload",
            retry_after_seconds=300,
        )
    try:
        result = GrantOrRenewEntitlementCommand()(order=order, transaction=transaction)
    except Exception as exc:
        return InternalEventConsumerResult(
            status="failed_retryable",
            request_summary={"event_id": event.event_id, "out_trade_no": out_trade_no},
            response_summary={"entitlement_projection_applied": False},
            error_code="entitlement_projection_failed",
            error_message=str(exc)[:500],
            retry_after_seconds=300,
        )
    if result.get("reason") == "not_service_period_product":
        return InternalEventConsumerResult(
            status="skipped",
            request_summary={"event_id": event.event_id, "out_trade_no": out_trade_no},
            response_summary={"skipped": True, "reason": "not_service_period_product"},
            result_summary={"reason": "not_service_period_product"},
        )
    if result.get("reason") == "order_not_paid":
        return InternalEventConsumerResult(
            status="failed_retryable",
            request_summary={"event_id": event.event_id, "out_trade_no": out_trade_no},
            response_summary={"skipped": True, "reason": "order_not_paid"},
            error_code="order_not_paid",
            error_message="order is not paid yet",
            retry_after_seconds=300,
        )
    if result.get("reason") in {"missing_unionid", "missing_customer_id"}:
        return InternalEventConsumerResult(
            status="failed_retryable",
            request_summary={"event_id": event.event_id, "out_trade_no": out_trade_no},
            response_summary={"skipped": True, "reason": "missing_customer_id"},
            result_summary={"event_type": "grant_failed_missing_customer_id", "out_trade_no": out_trade_no},
            error_code="missing_customer_id",
            error_message="canonical customer_id is required before service entitlement can be granted",
            retry_after_seconds=900,
        )
    if not result.get("ok", False):
        return InternalEventConsumerResult(
            status="failed_retryable",
            request_summary={"event_id": event.event_id, "out_trade_no": out_trade_no},
            response_summary={"skipped": bool(result.get("skipped")), "reason": result.get("reason", "")},
            error_code=_text(result.get("reason")) or "entitlement_projection_failed",
            error_message="service-period entitlement projection failed",
            retry_after_seconds=300,
        )
    return InternalEventConsumerResult(
        status="succeeded",
        request_summary={"event_id": event.event_id, "out_trade_no": out_trade_no},
        response_summary={"skipped": bool(result.get("skipped")), "reason": result.get("reason", "")},
        result_summary={
            "event_type": result.get("event_type", ""),
            "out_trade_no": out_trade_no,
            "entitlement_id": (result.get("entitlement") or {}).get("id", ""),
        },
    )


__all__ = ["service_period_entitlement_consumer"]
