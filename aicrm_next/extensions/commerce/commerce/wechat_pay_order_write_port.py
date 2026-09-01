from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class WeChatPayOrderCreate:
    out_trade_no: str
    order_source: str
    product_code: str
    product_name: str
    description: str
    amount_total: int
    currency: str
    customer_id: int
    payer_identity_id: int
    unionid: str
    payer_name_snapshot: str
    success_url: str
    metadata_json: str
    request_meta_json: str
    expires_at: Any
    client_order_ref: str


class WeChatPayOrderWritePort(Protocol):
    def create_dbapi(self, executor: Any, request: WeChatPayOrderCreate) -> dict[str, Any]: ...

    def update_payment_request_dbapi(
        self,
        executor: Any,
        *,
        out_trade_no: str,
        prepay_id: str,
        request_payload_json: str,
        response_payload_json: str,
    ) -> dict[str, Any]: ...

    def mark_failed_dbapi(self, executor: Any, *, out_trade_no: str, last_error: str) -> None: ...

    def mark_provider_unknown_dbapi(
        self,
        executor: Any,
        *,
        out_trade_no: str,
        last_error: str,
    ) -> None: ...

    def apply_provider_transaction_dbapi(
        self,
        executor: Any,
        *,
        out_trade_no: str,
        status: str,
        trade_state: str,
        transaction_id: str,
        bank_type: str,
        payer_total: int,
        success_time: str,
        notify_payload_json: str,
    ) -> dict[str, Any]: ...

    def close_reconcilable_dbapi(
        self,
        executor: Any,
        *,
        out_trade_no: str,
        reason: str,
    ) -> None: ...

    def mark_reconciliation_error_dbapi(
        self,
        executor: Any,
        *,
        out_trade_no: str,
        last_error: str,
    ) -> None: ...

    def record_provider_not_found_dbapi(
        self,
        executor: Any,
        *,
        out_trade_no: str,
        checked_at: datetime,
    ) -> None: ...

    def apply_refund_success_dbapi(
        self,
        executor: Any,
        *,
        order_id: int,
        refund_amount: int,
    ) -> str: ...

    def close_expired_dbapi(
        self,
        executor: Any,
        *,
        cutoff: datetime,
        limit: int,
        out_trade_no: str = "",
    ) -> list[dict[str, Any]]: ...

    def apply_coupon_pricing_dbapi(
        self,
        executor: Any,
        *,
        order_id: int,
        out_trade_no: str,
        subtotal: int,
        discount: int,
        payable: int,
        coupon_claim_id: int,
        coupon_snapshot_json: str,
    ) -> dict[str, Any]: ...


def build_wechat_pay_order_write_port() -> WeChatPayOrderWritePort:
    from .wechat_pay_order_write_repository import PostgresWeChatPayOrderWriteRepository

    return PostgresWeChatPayOrderWriteRepository()


__all__ = [
    "WeChatPayOrderCreate",
    "WeChatPayOrderWritePort",
    "build_wechat_pay_order_write_port",
]
