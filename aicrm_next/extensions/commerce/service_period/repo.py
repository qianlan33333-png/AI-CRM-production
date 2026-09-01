from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import json
import logging
from typing import Any, Protocol

from aicrm_next.crm.identity_contact.resolver import resolve_identity_with_dbapi
from aicrm_next.extensions.commerce.commerce.repo import build_commerce_repository
from aicrm_next.platform.platform_foundation.command_bus.models import CommandContext
from aicrm_next.platform.platform_foundation.internal_events.models import InternalEventCreateRequest
from aicrm_next.platform.platform_foundation.internal_events.outbox import enqueue_transactional_internal_event_outbox
from aicrm_next.platform.shared.db_session import connect_pooled_postgres
from aicrm_next.platform.shared.errors import ContractError, NotFoundError
from aicrm_next.platform.shared.repository_provider import assert_repository_allowed
from aicrm_next.platform.shared.runtime import production_data_ready, raw_database_url
from aicrm_next.platform.shared.safe_logging import safe_log_fields

from .domain import (
    TENANT_ID,
    entitlement_status,
    event_id_for,
    isoformat,
    normalize_link_slug,
    parse_datetime,
    remaining_days,
    text,
    utcnow,
    validate_duration_days,
)
from .huangyoucan_usage import huangyoucan_usage_match_joins, huangyoucan_usage_select_fields, public_huangyoucan_usage_fields
from .member_admin_fields import InMemoryMemberAdminFieldsMixin, PostgresMemberAdminFieldsMixin
from .member_grid_access_repo import (
    InMemoryMemberGridAccessRepositoryMixin,
    MemberGridAccessRepositoryProtocol,
    PostgresMemberGridAccessRepositoryMixin,
)
from .member_grid_repo import (
    InMemoryMemberGridRepositoryMixin,
    MemberGridRepositoryProtocol,
    PostgresMemberGridRepositoryMixin,
    effective_renewal_count_from_events,
)
from .order_identity import (
    compact_trade_product_payload as _compact_trade_product_payload,
    duration_end as _duration_end,
    duration_start as _duration_start,
    order_identity as _order_identity,
    order_paid_at as _order_paid_at,
    paid_order as _paid_order,
    paid_order_customer_id as _paid_order_customer_id,
    resolve_paid_order_unionid as _resolve_paid_order_unionid,
)

LOGGER = logging.getLogger(__name__)


def _jsonb(value: Any) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(value if isinstance(value, (dict, list)) else {}, dumps=lambda data: json.dumps(data, ensure_ascii=False, default=str))


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


class ServicePeriodRepository(MemberGridRepositoryProtocol, MemberGridAccessRepositoryProtocol, Protocol):
    def list_products(self, *, limit: int, offset: int) -> dict[str, Any]: ...
    def create_service_product(
        self,
        *,
        trade_product: dict[str, Any],
        duration_days: int,
        membership_config_id: str,
        membership_config_name: str,
        link_slug: str,
        metadata_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...
    def get_product(self, service_product_id: str) -> dict[str, Any] | None: ...
    def get_product_by_slug(self, link_slug: str) -> dict[str, Any] | None: ...
    def get_public_product_by_slug(self, link_slug: str) -> dict[str, Any] | None: ...
    def update_service_product(
        self,
        service_product_id: str,
        *,
        trade_product: dict[str, Any],
        duration_days: int,
        membership_config_id: str,
        membership_config_name: str,
        metadata_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...
    def copy_service_product(self, service_product_id: str, *, copied_trade_product: dict[str, Any]) -> dict[str, Any]: ...
    def delete_service_product(self, service_product_id: str) -> dict[str, Any]: ...
    def has_entitlements(self, service_product_id: str) -> bool: ...
    def stats(self, service_product_id: str) -> dict[str, Any]: ...
    def members(self, service_product_id: str, *, status: str | None, limit: int, offset: int) -> dict[str, Any]: ...
    def update_member_remark(self, service_product_id: str, unionid: str, remark: str) -> dict[str, Any]: ...
    def update_member_alliance(self, service_product_id: str, unionid: str, alliance: str) -> dict[str, Any]: ...
    def entitlement_for_unionid(self, service_product_id: str, unionid: str) -> dict[str, Any] | None: ...
    def grant_or_renew_from_paid_order(self, *, order: dict[str, Any], transaction: dict[str, Any] | None = None) -> dict[str, Any]: ...
    def apply_refund_from_order(self, *, out_trade_no: str, refund: dict[str, Any] | None = None) -> dict[str, Any]: ...
    def expire_due_entitlements(self, *, now: datetime | None = None) -> dict[str, Any]: ...


class InMemoryServicePeriodRepository(
    InMemoryMemberAdminFieldsMixin,
    InMemoryMemberGridRepositoryMixin,
    InMemoryMemberGridAccessRepositoryMixin,
):
    def __init__(self) -> None:
        self._products: list[dict[str, Any]] = []
        self._entitlements: list[dict[str, Any]] = []
        self._events: list[dict[str, Any]] = []
        self._member_views: list[dict[str, Any]] = []
        self._member_grid_collaborators: list[dict[str, Any]] = []
        self._member_grid_shares: list[dict[str, Any]] = []
        self._next_product_id = 1
        self._next_entitlement_id = 1
        self._next_event_id = 1
        self._next_member_view_id = 1
        self._next_member_grid_collaborator_id = 1

    def list_products(self, *, limit: int, offset: int) -> dict[str, Any]:
        rows = [self._serialize_product(row, include_trade_product=False) for row in self._products if not row.get("deleted")]
        rows.sort(key=lambda item: (text(item.get("updated_at")), text(item.get("id"))), reverse=True)
        return {"ok": True, "items": rows[offset : offset + limit], "total": len(rows), "limit": limit, "offset": offset}

    def create_service_product(
        self,
        *,
        trade_product: dict[str, Any],
        duration_days: int,
        membership_config_id: str,
        membership_config_name: str,
        link_slug: str,
        metadata_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        trade_id = text(trade_product.get("id"))
        slug = normalize_link_slug(link_slug)
        if any(text(row.get("trade_product_id")) == trade_id and not row.get("deleted") for row in self._products):
            raise ContractError("trade_product_id is already bound to a service period product")
        if any(text(row.get("link_slug")) == slug and not row.get("deleted") for row in self._products):
            raise ContractError("link_slug must be unique")
        now = utcnow().isoformat()
        row = {
            "id": f"sp_{self._next_product_id:03d}",
            "tenant_id": TENANT_ID,
            "trade_product_id": trade_id,
            "link_slug": slug,
            "membership_config_id": text(membership_config_id),
            "membership_config_name": text(membership_config_name),
            "duration_days": validate_duration_days(duration_days),
            "deleted": False,
            "metadata_json": deepcopy(metadata_json or {}),
            "created_at": now,
            "updated_at": now,
        }
        self._next_product_id += 1
        self._products.append(row)
        self._append_default_member_view(text(row["id"]), actor="system")
        self._append_default_member_grid_share(text(row["id"]), actor="system")
        return self._serialize_product(row)

    def get_product(self, service_product_id: str) -> dict[str, Any] | None:
        row = self._find_product(service_product_id)
        return self._serialize_product(row) if row else None

    def get_product_by_slug(self, link_slug: str) -> dict[str, Any] | None:
        slug = text(link_slug)
        for row in self._products:
            if not row.get("deleted") and text(row.get("link_slug")) == slug:
                return self._serialize_product(row)
        return None

    def get_public_product_by_slug(self, link_slug: str) -> dict[str, Any] | None:
        slug = text(link_slug)
        for row in self._products:
            if row.get("deleted") or text(row.get("link_slug")) != slug:
                continue
            item = self._serialize_product(row)
            trade_product = item.get("trade_product") or {}
            if not trade_product.get("enabled") or text(trade_product.get("status")) != "active":
                return None
            return item
        return None

    def update_service_product(
        self,
        service_product_id: str,
        *,
        trade_product: dict[str, Any],
        duration_days: int,
        membership_config_id: str,
        membership_config_name: str,
        metadata_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = self._find_product(service_product_id)
        if not row:
            raise NotFoundError("service period product not found")
        row.update(
            {
                "duration_days": validate_duration_days(duration_days),
                "membership_config_id": text(membership_config_id),
                "membership_config_name": text(membership_config_name),
                "metadata_json": deepcopy(metadata_json if metadata_json is not None else row.get("metadata_json") or {}),
                "updated_at": utcnow().isoformat(),
            }
        )
        return self._serialize_product(row)

    def copy_service_product(self, service_product_id: str, *, copied_trade_product: dict[str, Any]) -> dict[str, Any]:
        source = self._find_product(service_product_id)
        if not source:
            raise NotFoundError("service period product not found")
        return self.create_service_product(
            trade_product=copied_trade_product,
            duration_days=int(source.get("duration_days") or 0),
            membership_config_id=text(source.get("membership_config_id")),
            membership_config_name=text(source.get("membership_config_name")),
            link_slug=text(copied_trade_product.get("product_code")),
            metadata_json=deepcopy(source.get("metadata_json") or {}),
        )

    def delete_service_product(self, service_product_id: str) -> dict[str, Any]:
        row = self._find_product(service_product_id)
        if not row:
            raise NotFoundError("service period product not found")
        if self.has_entitlements(service_product_id):
            raise ContractError("已有服务期凭证的周期商品不能硬删除，请先下架")
        self._products = [item for item in self._products if item is not row]
        self._delete_member_views(service_product_id)
        self._delete_member_grid_access(service_product_id)
        return {"ok": True, "deleted": True, "service_product_id": service_product_id, "trade_product_id": text(row.get("trade_product_id"))}

    def has_entitlements(self, service_product_id: str) -> bool:
        return any(text(row.get("service_product_id")) == text(service_product_id) for row in self._entitlements)

    def stats(self, service_product_id: str) -> dict[str, Any]:
        now = utcnow()
        entitlements = [row for row in self._entitlements if text(row.get("service_product_id")) == text(service_product_id)]
        active = [row for row in entitlements if entitlement_status(row.get("end_at"), row.get("status"), now=now) == "active"]

        def _expiring_soon(row: dict[str, Any]) -> bool:
            end = parse_datetime(row.get("end_at"))
            return bool(end and now < end <= now + timedelta(days=7))

        return {
            "ok": True,
            "active_user_count": len(active),
            "expiring_7d_count": sum(1 for row in active if _expiring_soon(row)),
            "renewal_order_count": sum(
                1
                for row in self._events
                if text(row.get("service_product_id")) == text(service_product_id)
                and row.get("event_type") == "renewed"
                and not self._event_for_out_trade_no(text(row.get("out_trade_no")), {"refunded"})
            ),
            "total_paid_amount_cents": sum(
                int((row.get("payload_json") or {}).get("amount_total") or 0)
                for row in self._events
                if text(row.get("service_product_id")) == text(service_product_id)
                and row.get("event_type") in {"activated", "renewed"}
                and not self._event_for_out_trade_no(text(row.get("out_trade_no")), {"refunded"})
            ),
        }

    def members(self, service_product_id: str, *, status: str | None, limit: int, offset: int) -> dict[str, Any]:
        now = utcnow()
        rows = [row for row in self._entitlements if text(row.get("service_product_id")) == text(service_product_id)]
        if status:
            rows = [row for row in rows if entitlement_status(row.get("end_at"), row.get("status"), now=now) == status]
        items = [self._member_payload(row, now=now) for row in rows]
        items.sort(key=lambda item: text(item.get("end_at")), reverse=True)
        return {"ok": True, "items": items[offset : offset + limit], "total": len(items), "limit": limit, "offset": offset}

    def entitlement_for_unionid(self, service_product_id: str, unionid: str) -> dict[str, Any] | None:
        normalized = text(unionid)
        for row in self._entitlements:
            if text(row.get("service_product_id")) == text(service_product_id) and text(row.get("unionid")) == normalized:
                return self._entitlement_payload(row)
        return None

    def grant_or_renew_from_paid_order(self, *, order: dict[str, Any], transaction: dict[str, Any] | None = None) -> dict[str, Any]:
        if not _paid_order(order):
            return {"ok": True, "skipped": True, "reason": "order_not_paid"}
        product = self._find_product_for_order(order)
        if not product:
            return {"ok": True, "skipped": True, "reason": "not_service_period_product"}
        out_trade_no = text(order.get("out_trade_no") or (transaction or {}).get("out_trade_no"))
        if self._event_for_out_trade_no(out_trade_no, {"activated", "renewed"}):
            return {"ok": True, "idempotent": True, "skipped": True, "reason": "event_already_applied"}
        identity = _order_identity(order)
        unionid = identity["unionid"]
        customer_id = int(order.get("customer_id") or 0)
        if not customer_id and not unionid:
            event = self._append_event(
                product=product,
                entitlement_id="",
                order=order,
                out_trade_no=out_trade_no,
                unionid="",
                event_type="grant_failed_missing_unionid",
                duration_days=0,
                before=None,
                after=None,
                payload={"reason": "missing_customer_id", "order": order, "transaction": transaction or {}},
            )
            LOGGER.warning(
                "service_period_grant_failed_missing_unionid",
                extra=safe_log_fields(out_trade_no=out_trade_no, service_product_id=product.get("id")),
            )
            return {"ok": False, "skipped": True, "reason": "missing_customer_id", "event": event}
        now = utcnow()
        paid_at = _order_paid_at(order, transaction)
        entitlement = self._find_entitlement(text(product.get("id")), unionid, customer_id=customer_id)
        before = deepcopy(entitlement) if entitlement else None
        duration_days = int(product.get("duration_days") or 0)
        if entitlement and entitlement_status(entitlement.get("end_at"), entitlement.get("status"), now=now) == "active":
            start_at = parse_datetime(entitlement.get("start_at")) or paid_at
            end_at = _duration_end(parse_datetime(entitlement.get("end_at")) or paid_at, duration_days)
            event_type = "renewed"
            renewal_count = int(entitlement.get("renewal_count") or 0) + 1
        else:
            start_at = paid_at
            end_at = _duration_end(start_at, duration_days)
            event_type = "activated"
            renewal_count = 0 if not entitlement else int(entitlement.get("renewal_count") or 0) + 1
        metadata = {**deepcopy(entitlement.get("metadata_json") or {})} if entitlement else {}
        metadata.update({"last_order": order, "payer_name": identity["payer_name"]})
        if entitlement:
            entitlement.update(
                {
                    "trade_product_id": text(product.get("trade_product_id")),
                    "customer_id": customer_id or entitlement.get("customer_id"),
                    "external_userid_snapshot": identity["external_userid"],
                    "mobile_snapshot": identity["mobile"],
                    "membership_config_id": text(product.get("membership_config_id")),
                    "status": "active",
                    "start_at": start_at.isoformat(),
                    "end_at": end_at.isoformat(),
                    "last_order_id": order.get("id"),
                    "last_out_trade_no": out_trade_no,
                    "renewal_count": renewal_count,
                    "metadata_json": metadata,
                    "updated_at": now.isoformat(),
                }
            )
        else:
            entitlement = {
                "id": f"spe_{self._next_entitlement_id:03d}",
                "tenant_id": TENANT_ID,
                "service_product_id": text(product.get("id")),
                "trade_product_id": text(product.get("trade_product_id")),
                "customer_id": customer_id or None,
                "unionid": unionid,
                "external_userid_snapshot": identity["external_userid"],
                "mobile_snapshot": identity["mobile"],
                "membership_config_id": text(product.get("membership_config_id")),
                "status": "active",
                "start_at": start_at.isoformat(),
                "end_at": end_at.isoformat(),
                "last_order_id": order.get("id"),
                "last_out_trade_no": out_trade_no,
                "renewal_count": renewal_count,
                "metadata_json": metadata,
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
            }
            self._next_entitlement_id += 1
            self._entitlements.append(entitlement)
        event = self._append_event(
            product=product,
            entitlement_id=text(entitlement.get("id")),
            order=order,
            out_trade_no=out_trade_no,
            unionid=unionid,
            event_type=event_type,
            duration_days=duration_days,
            before=before,
            after=entitlement,
            payload={"order": order, "transaction": transaction or {}, "amount_total": int(order.get("amount_total") or 0)},
        )
        return {"ok": True, "event_type": event_type, "entitlement": self._entitlement_payload(entitlement), "event": event}

    def apply_refund_from_order(self, *, out_trade_no: str, refund: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized = text(out_trade_no)
        if not normalized:
            return {"ok": True, "skipped": True, "reason": "out_trade_no_missing"}
        source_event = self._event_for_out_trade_no(normalized, {"activated", "renewed"})
        if not source_event:
            return {"ok": True, "skipped": True, "reason": "not_service_period_order"}
        existing_refund = self._event_for_out_trade_no(normalized, {"refunded"})
        if existing_refund:
            return {"ok": True, "idempotent": True, "skipped": True, "reason": "refund_already_applied", "event": existing_refund}
        entitlement = None
        for row in self._entitlements:
            if text(row.get("id")) == text(source_event.get("entitlement_id")):
                entitlement = row
                break
        if not entitlement:
            return {"ok": True, "skipped": True, "reason": "entitlement_not_found"}
        product = self._find_product(source_event.get("service_product_id"))
        if not product:
            return {"ok": True, "skipped": True, "reason": "service_period_product_not_found"}
        before = deepcopy(entitlement)
        duration_days = int(source_event.get("duration_days") or product.get("duration_days") or 0)
        now = utcnow()
        other_active_events = [
            row
            for row in self._events
            if text(row.get("entitlement_id")) == text(entitlement.get("id"))
            and text(row.get("event_type")) in {"activated", "renewed"}
            and text(row.get("out_trade_no")) != normalized
            and not self._event_for_out_trade_no(text(row.get("out_trade_no")), {"refunded"})
        ]
        if not other_active_events:
            entitlement["status"] = "refunded"
            entitlement["end_at"] = now.isoformat()
        else:
            current_end = parse_datetime(entitlement.get("end_at")) or now
            new_end = _duration_start(current_end, duration_days) if duration_days > 0 else now
            entitlement["status"] = "active" if new_end > now else "refunded"
            entitlement["end_at"] = (new_end if new_end > now else now).isoformat()
        entitlement["updated_at"] = now.isoformat()
        metadata = deepcopy(entitlement.get("metadata_json") or {})
        metadata["last_refund"] = deepcopy(refund or {})
        entitlement["metadata_json"] = metadata
        event = self._append_event(
            product=product,
            entitlement_id=text(entitlement.get("id")),
            order={"out_trade_no": normalized},
            out_trade_no=normalized,
            unionid=text(entitlement.get("unionid")),
            event_type="refunded",
            duration_days=duration_days,
            before=before,
            after=entitlement,
            payload={"refund": refund or {}, "source_event": source_event},
        )
        return {"ok": True, "event_type": "refunded", "entitlement": self._entitlement_payload(entitlement), "event": event}

    def expire_due_entitlements(self, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        expired_count = 0
        for entitlement in self._entitlements:
            if entitlement_status(entitlement.get("end_at"), entitlement.get("status"), now=now) != "expired":
                continue
            if text(entitlement.get("status")) == "expired":
                continue
            product = self._find_product(entitlement.get("service_product_id"))
            if not product:
                continue
            before = deepcopy(entitlement)
            entitlement["status"] = "expired"
            entitlement["updated_at"] = now.isoformat()
            self._append_event(
                product=product,
                entitlement_id=text(entitlement.get("id")),
                order={},
                out_trade_no="",
                unionid=text(entitlement.get("unionid")),
                event_type="expired",
                duration_days=0,
                before=before,
                after=entitlement,
                payload={"source": "expire_due_entitlements"},
            )
            expired_count += 1
        return {"ok": True, "expired_count": expired_count}

    def _find_product(self, service_product_id: Any) -> dict[str, Any] | None:
        normalized = text(service_product_id)
        for row in self._products:
            if text(row.get("id")) == normalized and not row.get("deleted"):
                return row
        return None

    def _find_product_for_order(self, order: dict[str, Any]) -> dict[str, Any] | None:
        code = text(order.get("product_code"))
        commerce = build_commerce_repository()
        for row in self._products:
            if row.get("deleted"):
                continue
            trade = commerce.get_product(text(row.get("trade_product_id"))) or {}
            if text(trade.get("product_code")) == code:
                return row
        return None

    def _find_entitlement(
        self,
        service_product_id: str,
        unionid: str,
        *,
        customer_id: int = 0,
    ) -> dict[str, Any] | None:
        for row in self._entitlements:
            same_customer = bool(customer_id) and int(row.get("customer_id") or 0) == customer_id
            same_unionid = bool(text(unionid)) and text(row.get("unionid")) == text(unionid)
            if text(row.get("service_product_id")) == text(service_product_id) and (same_customer or same_unionid):
                return row
        return None

    def _event_for_out_trade_no(self, out_trade_no: str, event_types: set[str]) -> dict[str, Any] | None:
        if not out_trade_no:
            return None
        for row in self._events:
            if text(row.get("out_trade_no")) == out_trade_no and text(row.get("event_type")) in event_types:
                return row
        return None

    def _append_event(
        self,
        *,
        product: dict[str, Any],
        entitlement_id: str,
        order: dict[str, Any],
        out_trade_no: str,
        unionid: str,
        event_type: str,
        duration_days: int,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        existing = self._event_for_out_trade_no(out_trade_no, {event_type})
        if existing:
            return deepcopy(existing)
        row = {
            "id": f"spev_{self._next_event_id:03d}",
            "tenant_id": TENANT_ID,
            "event_id": event_id_for(event_type, out_trade_no or f"{product.get('id')}:{entitlement_id}:{self._next_event_id}"),
            "service_product_id": text(product.get("id")),
            "entitlement_id": entitlement_id,
            "trade_product_id": text(product.get("trade_product_id")),
            "order_id": order.get("id"),
            "out_trade_no": out_trade_no,
            "unionid": unionid,
            "event_type": event_type,
            "duration_days": int(duration_days or 0),
            "before_start_at": before.get("start_at") if before else None,
            "before_end_at": before.get("end_at") if before else None,
            "after_start_at": after.get("start_at") if after else None,
            "after_end_at": after.get("end_at") if after else None,
            "payload_json": deepcopy(payload),
            "created_at": utcnow().isoformat(),
        }
        self._next_event_id += 1
        self._events.append(row)
        return deepcopy(row)

    def _serialize_product(self, row: dict[str, Any], *, include_trade_product: bool = True) -> dict[str, Any]:
        commerce = build_commerce_repository()
        full_trade_product = commerce.get_product(text(row.get("trade_product_id"))) or {}
        trade_product = deepcopy(full_trade_product) if include_trade_product else _compact_trade_product_payload(full_trade_product)
        product_code = text(full_trade_product.get("product_code"))
        sold_count = commerce.count_orders_for_product_code(product_code) if product_code else 0
        updated_at = text(full_trade_product.get("updated_at") or row.get("updated_at"))
        return {
            **deepcopy(row),
            "id": text(row.get("id")),
            "trade_product_id": text(row.get("trade_product_id")),
            "product_code": product_code,
            "title": text(full_trade_product.get("title") or full_trade_product.get("name")),
            "name": text(full_trade_product.get("title") or full_trade_product.get("name")),
            "description": text(full_trade_product.get("description")),
            "price_cents": int(full_trade_product.get("price_cents") or full_trade_product.get("amount_total") or 0),
            "amount_total": int(full_trade_product.get("price_cents") or full_trade_product.get("amount_total") or 0),
            "currency": text(full_trade_product.get("currency")) or "CNY",
            "status": text(full_trade_product.get("status")) or "draft",
            "enabled": bool(full_trade_product.get("enabled")),
            "sold_count": sold_count,
            "updated_at": updated_at,
            "trade_product": trade_product,
        }

    def _entitlement_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        status = entitlement_status(row.get("end_at"), row.get("status"))
        return {**deepcopy(row), "status": status, "remaining_days": remaining_days(row.get("end_at")), "end_at": isoformat(row.get("end_at"))}

    def _member_payload(self, row: dict[str, Any], *, now: datetime) -> dict[str, Any]:
        metadata = row.get("metadata_json") or {}
        last_order = metadata.get("last_order") if isinstance(metadata.get("last_order"), dict) else {}
        status = entitlement_status(row.get("end_at"), row.get("status"), now=now)
        return {
            "unionid": text(row.get("unionid")),
            "display_name": text(metadata.get("payer_name")) or text(last_order.get("payer_name_snapshot")),
            "external_userid": text(row.get("external_userid_snapshot")),
            "mobile": text(row.get("mobile_snapshot")),
            "status": status,
            "remaining_days": remaining_days(row.get("end_at"), now=now),
            "end_at": isoformat(row.get("end_at")),
            "last_order_amount": int(last_order.get("amount_total") or 0),
            "last_order_duration_days": int((self._find_product(row.get("service_product_id")) or {}).get("duration_days") or 0),
            "renewal_count": effective_renewal_count_from_events(
                self._events,
                service_product_id=text(row.get("service_product_id")),
                unionid=text(row.get("unionid")),
            ),
            "remark": text(metadata.get("admin_remark") or metadata.get("remark")),
            "alliance": text(metadata.get("admin_alliance")),
            **public_huangyoucan_usage_fields({}),
        }


class PostgresServicePeriodRepository(
    PostgresMemberAdminFieldsMixin,
    PostgresMemberGridRepositoryMixin,
    PostgresMemberGridAccessRepositoryMixin,
):
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    def _connect(self):
        return connect_pooled_postgres(self._database_url)

    def list_products(self, *, limit: int, offset: int) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    sp.*,
                    p.product_code,
                    p.name,
                    p.amount_total,
                    p.currency,
                    p.status,
                    p.enabled,
                    p.updated_at AS trade_updated_at,
                    COALESCE(sold.sold_count, 0) AS sold_count
                FROM service_period_products sp
                JOIN wechat_pay_products p ON p.id = sp.trade_product_id
                LEFT JOIN LATERAL (
                    SELECT count(*) AS sold_count
                    FROM wechat_pay_orders o
                    WHERE o.product_code = p.product_code
                      AND (o.status = 'paid' OR o.trade_state = 'SUCCESS')
                ) sold ON TRUE
                WHERE sp.tenant_id = 'aicrm'
                  AND sp.deleted = FALSE
                ORDER BY sp.updated_at DESC, sp.id DESC
                LIMIT %s OFFSET %s
                """,
                (int(limit), int(offset)),
            ).fetchall()
            total_row = conn.execute("SELECT count(*) AS total FROM service_period_products WHERE tenant_id = 'aicrm' AND deleted = FALSE").fetchone() or {}
            items = [self._serialize_join_row(dict(row), include_trade_product=False) for row in rows]
        return {"ok": True, "items": items, "total": int(total_row.get("total") or 0), "limit": limit, "offset": offset}

    def create_service_product(
        self,
        *,
        trade_product: dict[str, Any],
        duration_days: int,
        membership_config_id: str,
        membership_config_name: str,
        link_slug: str,
        metadata_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO service_period_products (
                    tenant_id, trade_product_id, link_slug, membership_config_id,
                    membership_config_name, duration_days, metadata_json, created_at, updated_at
                )
                VALUES ('aicrm', %s, %s, %s, %s, %s, %s::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                RETURNING *
                """,
                (
                    int(trade_product["id"]),
                    normalize_link_slug(link_slug),
                    text(membership_config_id),
                    text(membership_config_name),
                    validate_duration_days(duration_days),
                    _jsonb(metadata_json or {}),
                ),
            ).fetchone()
            self._insert_default_member_view(conn, row["id"], actor="system")
            self._insert_default_member_grid_share(conn, text(row["id"]), actor="system")
            conn.commit()
        return self.get_product(text(row.get("id"))) or {}

    def get_product(self, service_product_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    sp.*,
                    p.product_code,
                    p.name,
                    p.amount_total,
                    p.currency,
                    p.status,
                    p.enabled,
                    p.updated_at AS trade_updated_at,
                    COALESCE(sold.sold_count, 0) AS sold_count
                FROM service_period_products sp
                JOIN wechat_pay_products p ON p.id = sp.trade_product_id
                LEFT JOIN LATERAL (
                    SELECT count(*) AS sold_count
                    FROM wechat_pay_orders o
                    WHERE o.product_code = p.product_code
                      AND (o.status = 'paid' OR o.trade_state = 'SUCCESS')
                ) sold ON TRUE
                WHERE sp.id::text = %s
                  AND sp.tenant_id = 'aicrm'
                  AND sp.deleted = FALSE
                LIMIT 1
                """,
                (text(service_product_id),),
            ).fetchone()
        return self._serialize_join_row(dict(row)) if row else None

    def get_product_by_slug(self, link_slug: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT sp.id
                FROM service_period_products sp
                WHERE sp.tenant_id = 'aicrm'
                  AND sp.deleted = FALSE
                  AND sp.link_slug = %s
                LIMIT 1
                """,
                (text(link_slug),),
            ).fetchone()
        return self.get_product(text(row.get("id"))) if row else None

    def get_public_product_by_slug(self, link_slug: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT sp.id
                FROM service_period_products sp
                JOIN wechat_pay_products p ON p.id = sp.trade_product_id
                WHERE sp.tenant_id = 'aicrm'
                  AND sp.deleted = FALSE
                  AND sp.link_slug = %s
                  AND p.enabled = TRUE
                  AND p.status = 'active'
                LIMIT 1
                """,
                (text(link_slug),),
            ).fetchone()
        return self.get_product(text(row.get("id"))) if row else None

    def update_service_product(
        self,
        service_product_id: str,
        *,
        trade_product: dict[str, Any],
        duration_days: int,
        membership_config_id: str,
        membership_config_name: str,
        metadata_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE service_period_products
                SET duration_days = %s,
                    membership_config_id = %s,
                    membership_config_name = %s,
                    metadata_json = COALESCE(%s::jsonb, metadata_json),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id::text = %s
                  AND tenant_id = 'aicrm'
                  AND deleted = FALSE
                RETURNING id
                """,
                (
                    validate_duration_days(duration_days),
                    text(membership_config_id),
                    text(membership_config_name),
                    _jsonb(metadata_json or {}),
                    text(service_product_id),
                ),
            ).fetchone()
            conn.commit()
        if not row:
            raise NotFoundError("service period product not found")
        return self.get_product(service_product_id) or {}

    def copy_service_product(self, service_product_id: str, *, copied_trade_product: dict[str, Any]) -> dict[str, Any]:
        source = self.get_product(service_product_id)
        if not source:
            raise NotFoundError("service period product not found")
        return self.create_service_product(
            trade_product=copied_trade_product,
            duration_days=int(source.get("duration_days") or 0),
            membership_config_id=text(source.get("membership_config_id")),
            membership_config_name=text(source.get("membership_config_name")),
            link_slug=text(copied_trade_product.get("product_code")),
            metadata_json=_json_object(source.get("metadata_json")),
        )

    def delete_service_product(self, service_product_id: str) -> dict[str, Any]:
        product = self.get_product(service_product_id)
        if not product:
            raise NotFoundError("service period product not found")
        if self.has_entitlements(service_product_id):
            raise ContractError("已有服务期凭证的周期商品不能硬删除，请先下架")
        with self._connect() as conn:
            row = conn.execute(
                """
                DELETE FROM service_period_products
                WHERE id::text = %s
                  AND tenant_id = 'aicrm'
                  AND deleted = FALSE
                RETURNING id, trade_product_id
                """,
                (text(service_product_id),),
            ).fetchone()
            conn.commit()
        if not row:
            raise NotFoundError("service period product not found")
        return {"ok": True, "deleted": True, "service_product_id": text(row.get("id")), "trade_product_id": text(row.get("trade_product_id"))}

    def has_entitlements(self, service_product_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM service_period_entitlements WHERE service_product_id::text = %s LIMIT 1",
                (text(service_product_id),),
            ).fetchone()
        return bool(row)

    def stats(self, service_product_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = (
                conn.execute(
                    """
                SELECT
                    count(*) FILTER (WHERE status = 'active' AND end_at > CURRENT_TIMESTAMP) AS active_user_count,
                    count(*) FILTER (WHERE status = 'active' AND end_at > CURRENT_TIMESTAMP AND end_at <= CURRENT_TIMESTAMP + INTERVAL '7 days') AS expiring_7d_count
                FROM service_period_entitlements
                WHERE service_product_id::text = %s
                """,
                    (text(service_product_id),),
                ).fetchone()
                or {}
            )
            renewal = (
                conn.execute(
                    """
                SELECT count(*) AS total
                FROM service_period_events renewed
                WHERE renewed.service_product_id::text = %s
                  AND renewed.event_type = 'renewed'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM service_period_events refunded
                      WHERE refunded.tenant_id = renewed.tenant_id
                        AND refunded.event_type = 'refunded'
                        AND refunded.out_trade_no = renewed.out_trade_no
                  )
                """,
                    (text(service_product_id),),
                ).fetchone()
                or {}
            )
            amount = (
                conn.execute(
                    """
                SELECT COALESCE(sum(GREATEST(COALESCE(o.amount_total, 0) - COALESCE(o.refunded_amount_total, 0), 0)), 0) AS total
                FROM service_period_events e
                JOIN wechat_pay_orders o ON o.out_trade_no = e.out_trade_no
                WHERE e.service_product_id::text = %s
                  AND e.event_type IN ('activated', 'renewed')
                """,
                    (text(service_product_id),),
                ).fetchone()
                or {}
            )
        return {
            "ok": True,
            "active_user_count": int(row.get("active_user_count") or 0),
            "expiring_7d_count": int(row.get("expiring_7d_count") or 0),
            "renewal_order_count": int(renewal.get("total") or 0),
            "total_paid_amount_cents": int(amount.get("total") or 0),
        }

    def members(self, service_product_id: str, *, status: str | None, limit: int, offset: int) -> dict[str, Any]:
        status_filter = text(status)
        params: list[Any] = [text(service_product_id)]
        where = ["e.service_product_id::text = %s"]
        if status_filter:
            where.append("e.status = %s")
            params.append(status_filter)
        params.extend([int(limit), int(offset)])
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    e.*,
                    p.duration_days AS last_order_duration_days,
                    o.amount_total AS last_order_amount,
                    COALESCE(
                        NULLIF(c.remark, ''),
                        NULLIF(wfu.remark, ''),
                        NULLIF(NULLIF(c.customer_name, ''), '问卷提交用户'),
                        NULLIF(NULLIF(c.profile_json->>'name', ''), '问卷提交用户'),
                        NULLIF(wim.name, ''),
                        NULLIF(c.customer_name, ''),
                        NULLIF(e.metadata_json->>'payer_name', ''),
                        NULLIF(o.payer_name_snapshot, '')
                    ) AS display_name,
                    COALESCE(
                        NULLIF(e.external_userid_snapshot, ''),
                        NULLIF(c.primary_external_userid, ''),
                        NULLIF(wim.external_userid, '')
                    ) AS external_userid,
                    COALESCE(NULLIF(c.mobile, ''), NULLIF(c.mobile_normalized, '')) AS mobile,
                    COALESCE(NULLIF(e.metadata_json->>'admin_remark', ''), NULLIF(e.metadata_json->>'remark', '')) AS remark,
                    {huangyoucan_usage_select_fields()}
                FROM service_period_entitlements e
                JOIN service_period_products p ON p.id = e.service_product_id
                LEFT JOIN wechat_pay_orders o ON o.id = e.last_order_id
                LEFT JOIN crm_user_identity c ON c.unionid = e.unionid
                LEFT JOIN LATERAL (
                    SELECT im.external_userid, im.name
                    FROM wecom_external_contact_identity_map im
                    WHERE im.unionid = e.unionid
                    ORDER BY im.updated_at DESC NULLS LAST, im.id DESC
                    LIMIT 1
                ) wim ON TRUE
                LEFT JOIN LATERAL (
                    SELECT fu.remark
                    FROM wecom_external_contact_follow_users fu
                    WHERE fu.external_userid = COALESCE(NULLIF(e.external_userid_snapshot, ''), NULLIF(c.primary_external_userid, ''), NULLIF(wim.external_userid, ''))
                      AND COALESCE(fu.relation_status, 'active') = 'active'
                    ORDER BY fu.is_primary DESC NULLS LAST, fu.updated_at DESC NULLS LAST, fu.id DESC
                    LIMIT 1
                ) wfu ON TRUE
                {huangyoucan_usage_match_joins(unionid_sql="e.unionid", mobile_sql="COALESCE(NULLIF(c.mobile, ''), NULLIF(c.mobile_normalized, ''))")}
                WHERE {" AND ".join(where)}
                ORDER BY e.end_at DESC, e.id DESC
                LIMIT %s OFFSET %s
                """,
                tuple(params),
            ).fetchall()
            total_row = (
                conn.execute(
                    f"SELECT count(*) AS total FROM service_period_entitlements e WHERE {' AND '.join(where)}",
                    tuple(params[:-2]),
                ).fetchone()
                or {}
            )
        now = utcnow()
        items = [
            {
                "unionid": text(row.get("unionid")),
                "display_name": text(row.get("display_name")),
                "external_userid": text(row.get("external_userid")),
                "mobile": text(row.get("mobile")),
                "status": entitlement_status(row.get("end_at"), row.get("status"), now=now),
                "remaining_days": remaining_days(row.get("end_at"), now=now),
                "end_at": isoformat(row.get("end_at")),
                "last_order_amount": int(row.get("last_order_amount") or 0),
                "last_order_duration_days": int(row.get("last_order_duration_days") or 0),
                "remark": text(row.get("remark")),
                **public_huangyoucan_usage_fields(dict(row)),
            }
            for row in rows
        ]
        return {"ok": True, "items": items, "total": int(total_row.get("total") or 0), "limit": limit, "offset": offset}

    def entitlement_for_unionid(self, service_product_id: str, unionid: str) -> dict[str, Any] | None:
        if not text(unionid):
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM service_period_entitlements
                WHERE tenant_id = 'aicrm'
                  AND service_product_id::text = %s
                  AND unionid = %s
                LIMIT 1
                """,
                (text(service_product_id), text(unionid)),
            ).fetchone()
        return self._entitlement_payload(dict(row)) if row else None

    def grant_or_renew_from_paid_order(self, *, order: dict[str, Any], transaction: dict[str, Any] | None = None) -> dict[str, Any]:
        if not _paid_order(order):
            return {"ok": True, "skipped": True, "reason": "order_not_paid"}
        out_trade_no = text(order.get("out_trade_no") or (transaction or {}).get("out_trade_no"))
        with self._connect() as conn:
            product = self._find_product_for_order(conn, order)
            if not product:
                return {"ok": True, "skipped": True, "reason": "not_service_period_product"}
            if self._event_exists(conn, out_trade_no, {"activated", "renewed"}):
                return {"ok": True, "idempotent": True, "skipped": True, "reason": "event_already_applied"}
            identity = _order_identity(order)
            unionid = _resolve_paid_order_unionid(conn, identity, resolve=resolve_identity_with_dbapi)
            customer_id = _paid_order_customer_id(conn, order, unionid)
            if not customer_id:
                event = self._insert_event(
                    conn,
                    product=product,
                    entitlement_id=None,
                    order=order,
                    out_trade_no=out_trade_no,
                    unionid="",
                    event_type="grant_failed_missing_unionid",
                    duration_days=0,
                    before=None,
                    after=None,
                    payload={"reason": "missing_customer_id", "order": order, "transaction": transaction or {}},
                )
                conn.commit()
                LOGGER.warning(
                    "service_period_grant_failed_missing_unionid",
                    extra=safe_log_fields(out_trade_no=out_trade_no, service_product_id=product.get("id")),
                )
                return {"ok": False, "skipped": True, "reason": "missing_customer_id", "event": event}
            result = self._grant_or_renew_with_customer(
                conn,
                product=product,
                order=order,
                transaction=transaction or {},
                customer_id=customer_id,
                unionid=unionid,
                out_trade_no=out_trade_no,
            )
            conn.commit()
            return result

    def apply_refund_from_order(self, *, out_trade_no: str, refund: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized = text(out_trade_no)
        if not normalized:
            return {"ok": True, "skipped": True, "reason": "out_trade_no_missing"}
        with self._connect() as conn:
            source_event = conn.execute(
                """
                SELECT *
                FROM service_period_events
                WHERE tenant_id = 'aicrm'
                  AND out_trade_no = %s
                  AND event_type IN ('activated', 'renewed')
                ORDER BY id DESC
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
            if not source_event:
                return {"ok": True, "skipped": True, "reason": "not_service_period_order"}
            existing_refund = conn.execute(
                """
                SELECT *
                FROM service_period_events
                WHERE tenant_id = 'aicrm'
                  AND out_trade_no = %s
                  AND event_type = 'refunded'
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
            if existing_refund:
                return {"ok": True, "idempotent": True, "skipped": True, "reason": "refund_already_applied", "event": dict(existing_refund)}
            entitlement = conn.execute(
                """
                SELECT *
                FROM service_period_entitlements
                WHERE id = %s
                FOR UPDATE
                """,
                (int(source_event["entitlement_id"]),),
            ).fetchone()
            if not entitlement:
                return {"ok": True, "skipped": True, "reason": "entitlement_not_found"}
            product = conn.execute(
                """
                SELECT *
                FROM service_period_products
                WHERE id = %s
                  AND tenant_id = 'aicrm'
                  AND deleted = FALSE
                LIMIT 1
                """,
                (int(source_event["service_product_id"]),),
            ).fetchone()
            if not product:
                return {"ok": True, "skipped": True, "reason": "service_period_product_not_found"}
            before = dict(entitlement)
            duration_days = int(source_event.get("duration_days") or product.get("duration_days") or 0)
            now = utcnow()
            other_active_events = (
                conn.execute(
                    """
                SELECT count(*) AS total
                FROM service_period_events paid
                WHERE paid.tenant_id = 'aicrm'
                  AND paid.entitlement_id = %s
                  AND paid.event_type IN ('activated', 'renewed')
                  AND paid.out_trade_no <> %s
                  AND NOT EXISTS (
                      SELECT 1
                      FROM service_period_events refunded
                      WHERE refunded.tenant_id = 'aicrm'
                        AND refunded.event_type = 'refunded'
                        AND refunded.out_trade_no = paid.out_trade_no
                  )
                """,
                    (int(entitlement["id"]), normalized),
                ).fetchone()
                or {}
            )
            if int(other_active_events.get("total") or 0) <= 0:
                next_status = "refunded"
                next_end = now
            else:
                current_end = parse_datetime(entitlement.get("end_at")) or now
                new_end = _duration_start(current_end, duration_days) if duration_days > 0 else now
                next_status = "active" if new_end > now else "refunded"
                next_end = new_end if new_end > now else now
            metadata = _json_object(entitlement.get("metadata_json"))
            metadata["last_refund"] = dict(refund or {})
            updated = conn.execute(
                """
                UPDATE service_period_entitlements
                SET status = %s,
                    end_at = %s,
                    metadata_json = %s::jsonb,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                RETURNING *
                """,
                (next_status, next_end, _jsonb(metadata), int(entitlement["id"])),
            ).fetchone()
            event = self._insert_event(
                conn,
                product=dict(product),
                entitlement_id=int(updated["id"]),
                order={"out_trade_no": normalized},
                out_trade_no=normalized,
                unionid=text(updated.get("unionid")),
                event_type="refunded",
                duration_days=duration_days,
                before=before,
                after=dict(updated),
                payload={"refund": refund or {}, "source_event": dict(source_event)},
            )
            conn.commit()
        return {"ok": True, "event_type": "refunded", "entitlement": self._entitlement_payload(dict(updated)), "event": event}

    def expire_due_entitlements(self, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        expired_count = 0
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT e.*, p.trade_product_id, p.duration_days
                FROM service_period_entitlements e
                JOIN service_period_products p ON p.id = e.service_product_id
                WHERE e.status = 'active'
                  AND e.end_at <= %s
                ORDER BY e.end_at ASC, e.id ASC
                """,
                (now,),
            ).fetchall()
            for row in rows:
                before = dict(row)
                updated = conn.execute(
                    """
                    UPDATE service_period_entitlements
                    SET status = 'expired', updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                      AND status = 'active'
                    RETURNING *
                    """,
                    (int(row.get("id")),),
                ).fetchone()
                if not updated:
                    continue
                self._insert_event(
                    conn,
                    product={"id": row.get("service_product_id"), "trade_product_id": row.get("trade_product_id")},
                    entitlement_id=int(row.get("id")),
                    order={},
                    out_trade_no="",
                    unionid=text(row.get("unionid")),
                    event_type="expired",
                    duration_days=0,
                    before=before,
                    after=dict(updated),
                    payload={"source": "expire_due_entitlements"},
                )
                expired_count += 1
            conn.commit()
        return {"ok": True, "expired_count": expired_count}

    def _serialize_join_row(self, row: dict[str, Any], *, include_trade_product: bool = True) -> dict[str, Any]:
        if include_trade_product:
            trade_product = build_commerce_repository().get_product(text(row.get("trade_product_id"))) or {}
        else:
            trade_product = _compact_trade_product_payload(row, product_id=row.get("trade_product_id"))
        price = int(row.get("amount_total") or trade_product.get("price_cents") or 0)
        return {
            **row,
            "id": text(row.get("id")),
            "trade_product_id": text(row.get("trade_product_id")),
            "product_code": text(row.get("product_code") or trade_product.get("product_code")),
            "title": text(row.get("name") or trade_product.get("title")),
            "name": text(row.get("name") or trade_product.get("title")),
            "description": text(trade_product.get("description")),
            "price_cents": price,
            "amount_total": price,
            "currency": text(row.get("currency") or trade_product.get("currency")) or "CNY",
            "status": text(row.get("status") or trade_product.get("status")),
            "enabled": bool(row.get("enabled")),
            "sold_count": int(row.get("sold_count") or 0),
            "duration_days": int(row.get("duration_days") or 0),
            "updated_at": isoformat(row.get("trade_updated_at") or row.get("updated_at")),
            "trade_product": trade_product,
        }

    def _find_product_for_order(self, conn: Any, order: dict[str, Any]) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT sp.*
            FROM service_period_products sp
            JOIN wechat_pay_products p ON p.id = sp.trade_product_id
            WHERE sp.tenant_id = 'aicrm'
              AND sp.deleted = FALSE
              AND p.product_code = %s
            LIMIT 1
            """,
            (text(order.get("product_code")),),
        ).fetchone()
        return dict(row) if row else None

    def _event_exists(self, conn: Any, out_trade_no: str, event_types: set[str]) -> bool:
        if not out_trade_no:
            return False
        row = conn.execute(
            """
            SELECT 1
            FROM service_period_events
            WHERE tenant_id = 'aicrm'
              AND out_trade_no = %s
              AND event_type = ANY(%s)
            LIMIT 1
            """,
            (out_trade_no, list(event_types)),
        ).fetchone()
        return bool(row)

    def _grant_or_renew_with_customer(
        self,
        conn: Any,
        *,
        product: dict[str, Any],
        order: dict[str, Any],
        transaction: dict[str, Any],
        customer_id: int,
        unionid: str,
        out_trade_no: str,
    ) -> dict[str, Any]:
        now = utcnow()
        paid_at = _order_paid_at(order, transaction)
        identity = _order_identity(order)
        identity["unionid"] = unionid
        entitlement = conn.execute(
            """
            SELECT *
            FROM service_period_entitlements
            WHERE tenant_id = 'aicrm'
              AND service_product_id = %s
              AND customer_id = %s
            FOR UPDATE
            """,
            (int(product["id"]), customer_id),
        ).fetchone()
        before = dict(entitlement) if entitlement else None
        duration_days = int(product.get("duration_days") or 0)
        if entitlement and entitlement_status(entitlement.get("end_at"), entitlement.get("status"), now=now) == "active":
            start_at = parse_datetime(entitlement.get("start_at")) or paid_at
            end_at = _duration_end(parse_datetime(entitlement.get("end_at")) or paid_at, duration_days)
            event_type = "renewed"
            renewal_count = int(entitlement.get("renewal_count") or 0) + 1
        else:
            start_at = paid_at
            end_at = _duration_end(start_at, duration_days)
            event_type = "activated"
            renewal_count = 0 if not entitlement else int(entitlement.get("renewal_count") or 0) + 1
        metadata = _json_object(entitlement.get("metadata_json")) if entitlement else {}
        metadata.update({"last_order": order, "payer_name": identity["payer_name"]})
        if entitlement:
            updated = conn.execute(
                """
                UPDATE service_period_entitlements
                SET trade_product_id = %s,
                    external_userid_snapshot = %s,
                    membership_config_id = %s,
                    status = 'active',
                    start_at = %s,
                    end_at = %s,
                    last_order_id = %s,
                    last_out_trade_no = %s,
                    renewal_count = %s,
                    metadata_json = %s::jsonb,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                RETURNING *
                """,
                (
                    int(product["trade_product_id"]),
                    identity["external_userid"],
                    text(product.get("membership_config_id")),
                    start_at,
                    end_at,
                    order.get("id"),
                    out_trade_no,
                    renewal_count,
                    _jsonb(metadata),
                    int(entitlement["id"]),
                ),
            ).fetchone()
        else:
            updated = conn.execute(
                """
                INSERT INTO service_period_entitlements (
                    tenant_id, service_product_id, trade_product_id, customer_id, unionid,
                    external_userid_snapshot, membership_config_id,
                    status, start_at, end_at, last_order_id, last_out_trade_no,
                    renewal_count, metadata_json, created_at, updated_at
                )
                VALUES (
                    'aicrm', %s, %s, %s, %s, %s, %s,
                    'active', %s, %s, %s, %s, %s, %s::jsonb,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                RETURNING *
                """,
                (
                    int(product["id"]),
                    int(product["trade_product_id"]),
                    customer_id,
                    unionid,
                    identity["external_userid"],
                    text(product.get("membership_config_id")),
                    start_at,
                    end_at,
                    order.get("id"),
                    out_trade_no,
                    renewal_count,
                    _jsonb(metadata),
                ),
            ).fetchone()
        event = self._insert_event(
            conn,
            product=product,
            entitlement_id=int(updated["id"]),
            order=order,
            out_trade_no=out_trade_no,
            unionid=unionid,
            event_type=event_type,
            duration_days=duration_days,
            before=before,
            after=dict(updated),
            payload={"order": order, "transaction": transaction, "amount_total": int(order.get("amount_total") or 0)},
            customer_id=customer_id,
        )
        return {"ok": True, "event_type": event_type, "entitlement": self._entitlement_payload(dict(updated)), "event": event}

    def _insert_event(
        self,
        conn: Any,
        *,
        product: dict[str, Any],
        entitlement_id: int | None,
        order: dict[str, Any],
        out_trade_no: str,
        unionid: str,
        event_type: str,
        duration_days: int,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        payload: dict[str, Any],
        customer_id: int = 0,
    ) -> dict[str, Any]:
        row = conn.execute(
            """
            INSERT INTO service_period_events (
                tenant_id, event_id, service_product_id, entitlement_id,
                trade_product_id, order_id, out_trade_no, customer_id, unionid, event_type,
                duration_days, before_start_at, before_end_at,
                after_start_at, after_end_at, payload_json, created_at
            )
            VALUES (
                'aicrm', %s, %s, %s, %s, %s, %s, NULLIF(%s, 0), %s, %s,
                %s, %s, %s, %s, %s, %s::jsonb, CURRENT_TIMESTAMP
            )
            ON CONFLICT (event_id) DO UPDATE SET event_id = EXCLUDED.event_id
            RETURNING *
            """,
            (
                event_id_for(event_type, out_trade_no or f"{product.get('id')}:{entitlement_id}:{event_type}:{utcnow().timestamp():.6f}"),
                int(product["id"]),
                entitlement_id,
                int(product["trade_product_id"]),
                order.get("id"),
                out_trade_no,
                customer_id,
                unionid,
                event_type,
                int(duration_days or 0),
                before.get("start_at") if before else None,
                before.get("end_at") if before else None,
                after.get("start_at") if after else None,
                after.get("end_at") if after else None,
                _jsonb(payload),
            ),
        ).fetchone()
        event = dict(row or {})
        if event_type in {"activated", "renewed", "admin_adjusted"} and (customer_id or text(unionid)):
            source_event_id = text(event.get("event_id") or event.get("id"))
            product_title = text(product.get("title") or product.get("name") or product.get("product_code")) or "周期商品"
            enqueue_transactional_internal_event_outbox(
                conn,
                InternalEventCreateRequest(
                    event_type="commerce.product_enrolled",
                    aggregate_type="service_period_event",
                    aggregate_id=source_event_id,
                    subject_type="customer" if customer_id else "unionid",
                    subject_id=text(customer_id or unionid),
                    idempotency_key=f"commerce.product_enrolled:service_period:{source_event_id}",
                    source_module="service_period.repo",
                    source_command_id=source_event_id,
                    occurred_at=event.get("created_at"),
                    context=CommandContext(
                        actor_id="service_period",
                        actor_type="system",
                        source_route="service_period.entitlement",
                    ),
                    payload={
                        "source_table": "service_period_events",
                        "source_id": source_event_id,
                        "product_type": "service_period",
                        "order": {
                            "id": text(event.get("id")),
                            "out_trade_no": text(out_trade_no),
                            "unionid": text(unionid),
                            "customer_id": customer_id,
                            "product_id": text(product.get("trade_product_id")),
                            "product_code": text(product.get("product_code")),
                            "product_name": product_title,
                            "product_type": "service_period",
                            "activated_at": str(event.get("created_at") or ""),
                        },
                    },
                    payload_summary={
                        "service_period_event_id": source_event_id,
                        "event_type": event_type,
                        "product_code": text(product.get("product_code")),
                        "unionid_present": bool(text(unionid)),
                        "customer_id_present": bool(customer_id),
                    },
                ),
            )
        return event

    def _entitlement_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        status = entitlement_status(row.get("end_at"), row.get("status"))
        return {
            **row,
            "id": text(row.get("id")),
            "service_product_id": text(row.get("service_product_id")),
            "trade_product_id": text(row.get("trade_product_id")),
            "status": status,
            "remaining_days": remaining_days(row.get("end_at")),
            "start_at": isoformat(row.get("start_at")),
            "end_at": isoformat(row.get("end_at")),
        }


_GLOBAL_REPO = InMemoryServicePeriodRepository()


def build_service_period_repository() -> ServicePeriodRepository:
    if production_data_ready():
        return assert_repository_allowed(PostgresServicePeriodRepository(raw_database_url()), capability_owner="service_period")
    return assert_repository_allowed(_GLOBAL_REPO, capability_owner="service_period")


def reset_service_period_fixture_state() -> None:
    global _GLOBAL_REPO
    _GLOBAL_REPO = InMemoryServicePeriodRepository()
