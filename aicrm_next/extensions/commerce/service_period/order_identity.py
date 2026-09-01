from __future__ import annotations

from datetime import datetime, timedelta
import json
from typing import Any

from aicrm_next.crm.identity_contact.dto import ResolvePersonIdentityRequest
from aicrm_next.crm.identity_contact.resolver import resolve_identity_with_dbapi, resolved_unionid

from .domain import isoformat, parse_datetime, text, utcnow


def paid_order(order: dict[str, Any]) -> bool:
    return text(order.get("status")).lower() == "paid" or text(order.get("trade_state")).upper() == "SUCCESS"


def order_identity(order: dict[str, Any]) -> dict[str, str]:
    metadata = order.get("metadata_json")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
    metadata = metadata if isinstance(metadata, dict) else {}
    identity = metadata.get("payer_identity") if isinstance(metadata.get("payer_identity"), dict) else {}
    return {
        "unionid": text(order.get("unionid") or identity.get("unionid")),
        "external_userid": text(identity.get("external_userid") or order.get("external_userid")),
        "mobile": text(identity.get("mobile") or order.get("mobile") or order.get("mobile_snapshot")),
        "payer_name": text(order.get("payer_name_snapshot") or identity.get("payer_name")),
        "openid": text(identity.get("openid") or order.get("openid")),
    }


def resolve_paid_order_unionid(conn: Any, identity: dict[str, str], *, resolve=resolve_identity_with_dbapi) -> str:
    unionid = text(identity.get("unionid"))
    query = (
        ResolvePersonIdentityRequest(unionid=unionid)
        if unionid
        else ResolvePersonIdentityRequest(
            external_userid=text(identity.get("external_userid")) or None,
            openid=text(identity.get("openid")) or None,
            mobile=text(identity.get("mobile")) or None,
        )
    )
    return resolved_unionid(resolve(conn, query))


def paid_order_customer_id(conn: Any, order: dict[str, Any], unionid: str) -> int:
    customer_id = int(order.get("customer_id") or 0)
    function_name, value = ("aicrm_customer_root_id", customer_id) if customer_id else ("aicrm_customer_id_by_unionid", unionid)
    if not value:
        return 0
    row = conn.execute(f"SELECT {function_name}(%s) AS customer_id", (value,)).fetchone()
    return int((row or {}).get("customer_id") or 0)


def order_paid_at(order: dict[str, Any], transaction: dict[str, Any] | None = None) -> datetime:
    return parse_datetime(order.get("paid_at")) or parse_datetime((transaction or {}).get("success_time")) or utcnow()


def duration_end(start: datetime, duration_days: int) -> datetime:
    return start + timedelta(days=duration_days)


def duration_start(end: datetime, duration_days: int) -> datetime:
    return end - timedelta(days=duration_days)


def compact_trade_product_payload(product: dict[str, Any], *, product_id: Any | None = None) -> dict[str, Any]:
    price = int(product.get("price_cents") or product.get("amount_total") or 0)
    return {
        "id": text(product_id if product_id is not None else product.get("id")),
        "product_code": text(product.get("product_code")),
        "title": text(product.get("title") or product.get("name")),
        "name": text(product.get("title") or product.get("name")),
        "description": text(product.get("description")),
        "price_cents": price,
        "amount_total": price,
        "currency": text(product.get("currency")) or "CNY",
        "status": text(product.get("status")) or "draft",
        "enabled": bool(product.get("enabled")),
        "slice_count": int(product.get("slice_count") or len(product.get("slices") or [])),
        "updated_at": isoformat(product.get("trade_updated_at") or product.get("updated_at")),
    }
