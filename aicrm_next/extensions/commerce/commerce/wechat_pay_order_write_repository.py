from __future__ import annotations

from datetime import datetime
from typing import Any

from .wechat_pay_order_write_port import WeChatPayOrderCreate


class PostgresWeChatPayOrderWriteRepository:
    """Canonical writer for the WeChat Pay order fact."""

    def create_dbapi(self, executor: Any, request: WeChatPayOrderCreate) -> dict[str, Any]:
        row = executor.execute(
            """
            INSERT INTO wechat_pay_orders (
                out_trade_no, order_source, product_code, product_name, description,
                amount_total, currency, customer_id, payer_identity_id, unionid,
                payer_name_snapshot, status, success_url, metadata_json,
                request_meta_json, expires_at, client_order_ref, created_at, updated_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                'created', %s, %s::jsonb, %s::jsonb, %s::timestamptz, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            RETURNING *
            """,
            (
                request.out_trade_no,
                request.order_source,
                request.product_code,
                request.product_name,
                request.description,
                int(request.amount_total),
                request.currency,
                int(request.customer_id),
                int(request.payer_identity_id),
                request.unionid,
                request.payer_name_snapshot,
                request.success_url,
                request.metadata_json,
                request.request_meta_json,
                request.expires_at,
                request.client_order_ref,
            ),
        ).fetchone()
        return dict(row or {})

    def update_payment_request_dbapi(
        self,
        executor: Any,
        *,
        out_trade_no: str,
        prepay_id: str,
        request_payload_json: str,
        response_payload_json: str,
    ) -> dict[str, Any]:
        row = executor.execute(
            """
            UPDATE wechat_pay_orders
            SET prepay_id = %s,
                status = 'paying',
                request_payload_json = %s::jsonb,
                response_payload_json = %s::jsonb,
                provider_unknown_at = NULL,
                reconciliation_not_found_count = 0,
                reconciliation_last_checked_at = NULL,
                last_error = '',
                updated_at = CURRENT_TIMESTAMP
            WHERE out_trade_no = %s
            RETURNING *
            """,
            (prepay_id, request_payload_json, response_payload_json, out_trade_no),
        ).fetchone()
        return dict(row or {})

    def mark_failed_dbapi(self, executor: Any, *, out_trade_no: str, last_error: str) -> None:
        executor.execute(
            """
            UPDATE wechat_pay_orders
            SET status = 'failed', last_error = %s, updated_at = CURRENT_TIMESTAMP
            WHERE out_trade_no = %s
            """,
            (str(last_error or "")[:500], str(out_trade_no or "").strip()),
        )

    def mark_provider_unknown_dbapi(
        self,
        executor: Any,
        *,
        out_trade_no: str,
        last_error: str,
    ) -> None:
        executor.execute(
            """
            UPDATE wechat_pay_orders
            SET status = CASE WHEN status = 'paid' THEN status ELSE 'provider_unknown' END,
                provider_unknown_at = CASE
                    WHEN status = 'provider_unknown' THEN COALESCE(provider_unknown_at, CURRENT_TIMESTAMP)
                    ELSE CURRENT_TIMESTAMP
                END,
                reconciliation_not_found_count = CASE
                    WHEN status = 'provider_unknown' THEN reconciliation_not_found_count
                    ELSE 0
                END,
                reconciliation_last_checked_at = CASE
                    WHEN status = 'provider_unknown' THEN reconciliation_last_checked_at
                    ELSE NULL
                END,
                last_error = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE out_trade_no = %s
            """,
            (str(last_error or "")[:500], str(out_trade_no or "").strip()),
        )

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
    ) -> dict[str, Any]:
        row = executor.execute(
            """
            UPDATE wechat_pay_orders
            SET status = %s,
                trade_state = %s,
                transaction_id = %s,
                bank_type = %s,
                payer_total = %s,
                paid_at = CASE WHEN %s = 'SUCCESS' THEN NULLIF(%s, '')::timestamptz ELSE paid_at END,
                notify_payload_json = %s::jsonb,
                last_error = '',
                updated_at = CURRENT_TIMESTAMP
            WHERE out_trade_no = %s
            RETURNING *
            """,
            (
                status,
                trade_state,
                transaction_id,
                bank_type,
                int(payer_total),
                trade_state,
                success_time,
                notify_payload_json,
                out_trade_no,
            ),
        ).fetchone()
        return dict(row or {})

    def close_reconcilable_dbapi(
        self,
        executor: Any,
        *,
        out_trade_no: str,
        reason: str,
    ) -> None:
        executor.execute(
            """
            UPDATE wechat_pay_orders
            SET status = 'closed',
                trade_state = 'CLOSED',
                last_error = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE out_trade_no = %s
            """,
            (str(reason or "")[:500], str(out_trade_no or "").strip()),
        )

    def mark_reconciliation_error_dbapi(
        self,
        executor: Any,
        *,
        out_trade_no: str,
        last_error: str,
    ) -> None:
        executor.execute(
            """
            UPDATE wechat_pay_orders
            SET last_error = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE out_trade_no = %s
            """,
            (str(last_error or "")[:500], str(out_trade_no or "").strip()),
        )

    def record_provider_not_found_dbapi(
        self,
        executor: Any,
        *,
        out_trade_no: str,
        checked_at: datetime,
    ) -> None:
        executor.execute(
            """
            UPDATE wechat_pay_orders
            SET reconciliation_not_found_count = COALESCE(reconciliation_not_found_count, 0) + 1,
                reconciliation_last_checked_at = %s::timestamptz,
                last_error = 'wechat_pay_reconciliation_provider_not_found_confirmation_pending',
                updated_at = CURRENT_TIMESTAMP
            WHERE out_trade_no = %s
            """,
            (checked_at, str(out_trade_no or "").strip()),
        )

    def apply_refund_success_dbapi(
        self,
        executor: Any,
        *,
        order_id: int,
        refund_amount: int,
    ) -> str:
        executor.execute(
            """
            UPDATE wechat_pay_orders
            SET refunded_amount_total = LEAST(amount_total, COALESCE(refunded_amount_total, 0) + %s),
                refund_status = CASE
                    WHEN LEAST(amount_total, COALESCE(refunded_amount_total, 0) + %s) >= amount_total THEN 'full_refunded'
                    ELSE 'partial_refunded'
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            RETURNING refund_status
            """,
            (int(refund_amount), int(refund_amount), int(order_id)),
        )
        row = executor.fetchone()
        return str((row or {}).get("refund_status") or "")

    def close_expired_dbapi(
        self,
        executor: Any,
        *,
        cutoff: datetime,
        limit: int,
        out_trade_no: str = "",
    ) -> list[dict[str, Any]]:
        normalized_trade_no = str(out_trade_no or "").strip()
        order_filter = "AND out_trade_no = %s" if normalized_trade_no else ""
        params: list[Any] = [cutoff]
        if normalized_trade_no:
            params.append(normalized_trade_no)
        params.append(max(1, min(int(limit or 200), 500)))
        rows = executor.execute(
            f"""
            WITH expired AS (
                SELECT id
                FROM wechat_pay_orders
                WHERE COALESCE(status, '') IN ('created', 'paying', 'pending', '')
                  AND COALESCE(trade_state, '') <> 'SUCCESS'
                  AND paid_at IS NULL
                  AND coupon_claim_id IS NULL
                  AND created_at <= %s::timestamptz
                  {order_filter}
                ORDER BY created_at ASC, id ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            UPDATE wechat_pay_orders order_fact
            SET status = 'closed',
                trade_state = 'CLOSED',
                last_error = 'order_auto_closed_after_2h',
                updated_at = CURRENT_TIMESTAMP
            FROM expired
            WHERE order_fact.id = expired.id
            RETURNING order_fact.id, order_fact.out_trade_no, order_fact.status,
                      order_fact.trade_state, order_fact.created_at, order_fact.updated_at
            """,
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

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
    ) -> dict[str, Any]:
        row = executor.execute(
            """
            UPDATE wechat_pay_orders
            SET subtotal_amount_total = %s,
                discount_amount_total = %s,
                amount_total = %s,
                coupon_claim_id = %s,
                coupon_snapshot_json = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND out_trade_no = %s
            RETURNING *
            """,
            (
                int(subtotal),
                int(discount),
                int(payable),
                int(coupon_claim_id),
                coupon_snapshot_json,
                int(order_id),
                str(out_trade_no or "").strip(),
            ),
        ).fetchone()
        return dict(row or {})


__all__ = ["PostgresWeChatPayOrderWriteRepository"]
