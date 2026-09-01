from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from aicrm_next.crm.identity_contact.dto import IdentityResolution, ResolvePersonIdentityRequest
from aicrm_next.crm.identity_contact.resolver import SQLAlchemyIdentityResolver, classify_identity_candidates
from aicrm_next.crm.customer_read_model.application import GetCustomerContextQuery, _close_repository
from aicrm_next.crm.customer_read_model.dto import CustomerContextRequest
from aicrm_next.crm.customer_read_model.errors import CustomerScopeForbiddenError
from aicrm_next.crm.customer_read_model.repo import CustomerReadRepository, build_customer_live_source_repository
from aicrm_next.crm.customer_read_model.sidebar_customer_resolution import (
    customer_text as _customer_text,
    resolve_customer_payload as _resolve_customer_payload,
)
from aicrm_next.crm.customer_read_model.extension_port import (
    GetImageThumbnailQuery,
    ListMediaItemsQuery,
    ListProductsQuery,
    ListServicePeriodProductsQuery,
    build_commerce_repository,
    huangyoucan_usage_match_joins,
    huangyoucan_usage_select_fields,
    public_huangyoucan_usage_fields,
    service_period_entitlement_status,
    service_period_isoformat,
    service_period_remaining_days,
)
from aicrm_next.platform.shared.db_session import get_engine
from aicrm_next.platform.shared.errors import ContractError, NotFoundError
from aicrm_next.platform.shared.runtime import raw_database_url
from aicrm_next.platform.shared.runtime_settings import runtime_setting
from aicrm_next.platform.shared.signed_context import append_ctx_fragment, build_sidebar_product_context_token

MODULES = ["profile", "questionnaires", "products", "orders", "periodic_orders", "materials", "other_staff_messages"]
ORDER_STATUS_LABELS = {
    "pending": "待支付",
    "paid": "已支付",
    "refund_processing": "退款处理中",
    "partial_refunded": "部分退款",
    "full_refunded": "全额退款",
    "closed": "已关闭",
    "failed": "支付失败",
}
SERVICE_PERIOD_STATUS_LABELS = {
    "active": "使用中",
    "expired": "已过期",
}
_QUESTIONNAIRE_TITLE_PLACEHOLDER_TEXTS = {"questionnaire_title", "title", "name", "submitted_at"}
_QUESTION_PLACEHOLDER_TEXTS = {"question", "question_title", "question_title_snapshot"}
_ANSWER_PLACEHOLDER_TEXTS = {"text_value", "selected_option_texts_snapshot", "answer", "value"}
SIDEBAR_IMAGE_QUICK_KEYWORDS_SETTING = "AICRM_SIDEBAR_IMAGE_QUICK_KEYWORDS"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _limit(value: Any, *, default: int = 50, maximum: int = 200) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def _int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _sidebar_image_quick_keywords() -> list[str]:
    values: list[str] = []
    raw = runtime_setting(SIDEBAR_IMAGE_QUICK_KEYWORDS_SETTING, "")
    for item in re.split(r"[,，\r\n]+", raw):
        keyword = _text(item)
        if keyword and keyword not in values:
            values.append(keyword)
    return values if 3 <= len(values) <= 5 else []


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except Exception:
        return default


def _format_time(value: Any) -> str:
    if value in (None, ""):
        return ""
    beijing = timezone(timedelta(hours=8))
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(beijing).strftime("%Y-%m-%d %H:%M")
        return value.strftime("%Y-%m-%d %H:%M")
    value_text = _text(value).replace("T", " ")
    if not value_text:
        return ""
    try:
        parsed = datetime.fromisoformat(value_text)
        if parsed.tzinfo is not None:
            return parsed.astimezone(beijing).strftime("%Y-%m-%d %H:%M")
        return parsed.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value_text[:16]


def _money_label(amount_total: Any) -> str:
    cents = _int(amount_total)
    yuan = cents / 100
    if cents % 100 == 0:
        return f"¥{int(yuan)}"
    return f"¥{yuan:.2f}"


def _normalize_mobile(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _clean_placeholder_text(value: Any, placeholders: set[str]) -> str:
    value_text = _text(value)
    if not value_text:
        return ""
    if value_text.lower() in placeholders:
        return ""
    return value_text


def _questionnaire_title_text(value: Any) -> str:
    return _clean_placeholder_text(value, _QUESTIONNAIRE_TITLE_PLACEHOLDER_TEXTS)


def _question_text(value: Any) -> str:
    return _clean_placeholder_text(value, _QUESTION_PLACEHOLDER_TEXTS)


def _answer_text_value(value: Any) -> str:
    return _clean_placeholder_text(value, _ANSWER_PLACEHOLDER_TEXTS)


def _diagnostics(**extra: Any) -> dict[str, Any]:
    return {
        "source_status": "next_read_model",
        "read_model_status": "primary",
        "route_owner": "ai_crm_next",
        "fallback_used": False,
        "degraded": False,
        **extra,
    }


def _with_route_owner(payload: dict[str, Any]) -> dict[str, Any]:
    payload.setdefault("route_owner", "ai_crm_next")
    payload.setdefault("fallback_used", False)
    payload.setdefault("source_status", "next_read_model")
    payload.setdefault("read_model_status", "primary")
    payload.setdefault("degraded", False)
    return payload


def _database_url() -> str:
    url = raw_database_url()
    if not url:
        raise ContractError("DATABASE_URL is required for sidebar v2 read model")
    return url


class SidebarV2SqlRepository:
    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine or get_engine(database_url=_database_url())

    def get_profile_fields(self, external_userid: str) -> dict[str, Any] | None:
        return self._one(
            """
            WITH customer_scope AS (
                SELECT COALESCE(root.id, c.id) customer_id, im.unionid
                FROM wecom_external_contact_identity_map im JOIN customers c ON c.id = im.customer_id
                LEFT JOIN customers root ON root.id = c.merged_into_customer_id
                WHERE im.external_userid = :external_userid AND im.status = 'active'
                ORDER BY im.updated_at DESC, im.id DESC LIMIT 1
            )
            SELECT :external_userid external_userid, profile.source, profile.industry,
                   profile.industry_description, profile.needs_blockers_followup,
                   profile.updated_by, profile.updated_at
            FROM sidebar_customer_profile_fields profile
            JOIN customer_scope scope ON profile.customer_id = scope.customer_id
              OR (profile.customer_id IS NULL AND profile.unionid <> '' AND profile.unionid = scope.unionid)
            ORDER BY profile.updated_at DESC LIMIT 1
            """,
            {"external_userid": external_userid},
        )
    def get_workflow_title_for_customer(self, external_userid: str) -> str:
        identity = self._resolve_identity(external_userid=external_userid)
        if identity is None:
            return ""
        row = self._one(
            """
            SELECT COALESCE(NULLIF(l.link_name, ''), NULLIF(c.channel_name, ''), NULLIF(l.initial_audience_code, ''), NULLIF(c.channel_code, '')) AS title
            FROM automation_channel_contact channel_contact
            LEFT JOIN automation_channel c ON c.id = channel_contact.channel_id
            LEFT JOIN wecom_customer_acquisition_links l ON l.automation_channel_id = c.id
            WHERE channel_contact.unionid = :unionid
            ORDER BY channel_contact.updated_at DESC, channel_contact.id DESC, l.updated_at DESC, l.id DESC
            LIMIT 1
            """,
            {"unionid": _text(identity.unionid)},
        )
        return _text((row or {}).get("title"))

    def get_contact_snapshot(self, external_userid: str) -> dict[str, Any] | None:
        return self._one(
            """
            SELECT
                im.external_userid,
                COALESCE(NULLIF(im.name, ''), NULLIF(im.raw_profile ->> 'name', '')) AS customer_name,
                COALESCE(NULLIF(fu.user_id, ''), NULLIF(im.follow_user_userid, '')) AS owner_userid,
                COALESCE(NULLIF(fu.remark, ''), NULLIF(im.raw_profile ->> 'remark', '')) AS remark,
                COALESCE(NULLIF(im.raw_profile ->> 'description', ''), '') AS description
            FROM wecom_external_contact_identity_map im
            LEFT JOIN wecom_external_contact_follow_users fu
              ON fu.corp_id = im.corp_id
             AND fu.external_userid = im.external_userid
             AND COALESCE(fu.relation_status, 'active') = 'active'
            WHERE im.external_userid = :external_userid
            ORDER BY fu.is_primary DESC NULLS LAST, fu.updated_at DESC NULLS LAST, im.updated_at DESC, im.id DESC
            LIMIT 1
            """,
            {"external_userid": external_userid},
        )

    def get_external_identity_snapshot(self, external_userid: str) -> dict[str, Any] | None:
        identity = self._resolve_identity(external_userid=external_userid)
        if identity is None:
            return self._one(
                """
                SELECT im.external_userid, COALESCE(NULLIF(fu.user_id, ''), im.follow_user_userid) follow_user_userid,
                       im.name, im.unionid, im.openid, im.status, im.customer_id
                FROM wecom_external_contact_identity_map im LEFT JOIN wecom_external_contact_follow_users fu
                  ON fu.corp_id = im.corp_id AND fu.external_userid = im.external_userid AND fu.relation_status = 'active'
                WHERE im.external_userid = :external_userid AND im.status = 'active'
                ORDER BY fu.is_primary DESC NULLS LAST, fu.updated_at DESC NULLS LAST
                LIMIT 1
                """,
                {"external_userid": external_userid},
            )
        return {"external_userid": _text(identity.external_userid), "name": _text(identity.customer_name),
                "follow_user_userid": _text(identity.follow_user_userid or identity.owner_userid),
                "unionid": _text(identity.unionid), "openid": _text(identity.openid), "status": "active"}
    def get_contact_owner_userids(self, external_userid: str) -> set[str]:
        rows = self._all(
            """
            SELECT DISTINCT owner_userid
            FROM (
                SELECT COALESCE(NULLIF(user_id, ''), NULLIF(raw_follow_user ->> 'userid', '')) AS owner_userid
                FROM wecom_external_contact_follow_users
                WHERE external_userid = :external_userid
                  AND COALESCE(relation_status, 'active') = 'active'
                UNION ALL
                SELECT NULLIF(follow_user_userid, '') AS owner_userid
                FROM wecom_external_contact_identity_map
                WHERE external_userid = :external_userid
                  AND COALESCE(status, 'active') = 'active'
            ) owners
            WHERE COALESCE(owner_userid, '') <> ''
            """,
            {"external_userid": external_userid},
        )
        return {_text(row.get("owner_userid")) for row in rows if _text(row.get("owner_userid"))}

    def get_contact_binding_status(self, external_userid: str) -> dict[str, Any]:
        identity = self._resolve_identity(external_userid=external_userid)
        if identity is None:
            return {"is_bound": False, "external_userid": external_userid}
        row = self._one(
            """
            SELECT
                :external_userid AS external_userid,
                b.person_id,
                '' AS first_bound_by_userid,
                b.first_owner_userid,
                b.last_owner_userid,
                NULL::timestamptz AS created_at,
                b.updated_at,
                :mobile AS mobile,
                :unionid AS third_party_user_id,
                :owner_userid AS primary_owner_userid,
                :customer_name AS customer_name
            FROM external_contact_bindings b
            WHERE b.external_userid = :canonical_external_userid
            ORDER BY b.updated_at DESC
            LIMIT 1
            """,
            {
                "external_userid": external_userid,
                "canonical_external_userid": _text(identity.external_userid),
                "mobile": _text(identity.mobile),
                "unionid": _text(identity.unionid),
                "owner_userid": _text(identity.owner_userid),
                "customer_name": _text(identity.customer_name),
            },
        )
        row = row or {}
        return {
            "is_bound": bool(_text(identity.mobile)),
            "external_userid": _text(identity.external_userid) or external_userid,
            "person_id": row.get("person_id"),
            "mobile": _text(identity.mobile),
            "third_party_user_id": _text(identity.unionid),
            "first_bound_by_userid": _text(row.get("first_bound_by_userid")),
            "first_owner_userid": _text(row.get("first_owner_userid")),
            "last_owner_userid": _text(row.get("last_owner_userid")),
            "owner_userid": _text(row.get("last_owner_userid") or row.get("first_owner_userid") or identity.owner_userid),
            "customer_name": _text(identity.customer_name),
            "created_at": _text(row.get("created_at")),
            "updated_at": _text(row.get("updated_at")),
        }

    def _resolve_identity(self, *, external_userid: str, mobile: str = "") -> IdentityResolution | None:
        query = ResolvePersonIdentityRequest(
            external_userid=_text(external_userid) or None,
            mobile=_normalize_mobile(mobile) or None,
        )
        if self._engine.dialect.name == "sqlite":
            columns = {column["name"] for column in inspect(self._engine).get_columns("crm_user_identity")}
            projection = []
            for column in (
                "unionid",
                "primary_external_userid",
                "external_userids_json",
                "primary_openid",
                "openids_json",
                "mobile",
                "mobile_normalized",
                "mobile_verified",
                "mobile_source",
                "primary_owner_userid",
                "customer_name",
                "remark",
                "description",
                "identity_status",
            ):
                projection.append(column if column in columns else f"'' AS {column}")
            with self._engine.connect() as connection:
                rows = connection.execute(
                    text(f"SELECT {', '.join(projection)} FROM crm_user_identity ORDER BY unionid")
                ).mappings().all()
            resolution = classify_identity_candidates(query, rows)
            return resolution.identity if resolution.status == "resolved" else None
        with self._engine.connect() as connection:
            resolution = SQLAlchemyIdentityResolver(connection).resolve(
                query
            )
        return resolution.identity if resolution.status == "resolved" else None

    def get_bindable_wechat_pay_order_mobile(self, external_userid: str) -> dict[str, Any] | None:
        identity = self._resolve_identity(external_userid=external_userid)
        if identity is None:
            return None
        rows = self._all(
            """
            WITH identity_scope(unionid, mobile) AS (VALUES (:unionid, :mobile)),
            matching_orders AS (
                SELECT mobile.mobile_snapshot, '' AS userid_snapshot, paid_at, created_at, o.id::text AS id
                FROM wechat_pay_orders o
                JOIN identity_scope identity ON identity.unionid = o.unionid
                CROSS JOIN LATERAL (
                    SELECT regexp_replace(
                        COALESCE(
                            NULLIF(identity.mobile, ''),
                            NULLIF(o.metadata_json #>> '{payer_identity,mobile}', ''),
                            NULLIF(o.metadata_json #>> '{buyer_identity,mobile}', '')
                        ),
                        '[^0-9]',
                        '',
                        'g'
                    ) AS mobile_snapshot
                ) mobile
                WHERE COALESCE(mobile.mobile_snapshot, '') <> ''
                  AND (status = 'paid' OR trade_state = 'SUCCESS')
                UNION ALL
                SELECT identity.mobile AS mobile_snapshot, '' AS userid_snapshot, o.paid_at, o.created_at, o.order_id::text AS id
                FROM wechat_shop_orders o
                JOIN identity_scope identity ON identity.unionid = o.unionid
                WHERE COALESCE(identity.mobile, '') <> ''
                  AND (deal_recorded IS TRUE OR status_code::text = '30' OR business_status IN ('deal', 'paid'))
            )
            SELECT
                mobile_snapshot,
                MAX(COALESCE(NULLIF(userid_snapshot, ''), '')) AS userid_snapshot,
                COUNT(*) AS order_count,
                MAX(COALESCE(paid_at, created_at)) AS latest_order_at
            FROM matching_orders
            GROUP BY mobile_snapshot
            ORDER BY latest_order_at DESC, mobile_snapshot ASC
            LIMIT 2
            """,
            {"unionid": _text(identity.unionid), "mobile": _text(identity.mobile)},
        )
        return rows[0] if len(rows) == 1 else None

    def list_customer_wechat_pay_orders(self, *, external_userid: str, mobile: str = "", limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = _limit(limit, default=20, maximum=100)
        candidate_limit = max(safe_limit, 20)
        identity = self._resolve_identity(external_userid=external_userid, mobile=mobile)
        if identity is None:
            return []
        return self._all(
            """
            WITH identity_scope(unionid, primary_external_userid, mobile) AS (
                VALUES (:unionid, :canonical_external_userid, :canonical_mobile)
            ),
            unionid_orders AS (
                SELECT
                    'wechat_pay' AS provider, 'wechat_pay' AS channel, '微信支付' AS channel_label,
                    o.id::text AS id, o.out_trade_no, o.transaction_id, o.product_code,
                    COALESCE(NULLIF(o.product_name, ''), o.product_code) AS product_name,
                    o.amount_total, o.currency, identity.primary_external_userid AS order_external_userid,
                    identity.mobile AS mobile_snapshot, '' AS payer_openid, o.unionid, o.status, o.trade_state,
                    o.refunded_amount_total, o.refund_status, o.paid_at, o.created_at,
                    COALESCE(o.paid_at, o.created_at) AS sort_at
                FROM wechat_pay_orders o
                JOIN identity_scope identity ON identity.unionid = o.unionid
                ORDER BY COALESCE(o.paid_at, o.created_at) DESC, o.id DESC
                LIMIT :candidate_limit
            ),
            wechat_shop_unionid_orders AS (
                SELECT
                    'wechat_shop' AS provider, 'wechat_shop' AS channel, '微信小店' AS channel_label,
                    o.order_id::text AS id, o.order_id::text AS out_trade_no, o.transaction_id, o.product_code,
                    COALESCE(NULLIF(o.product_name, ''), o.product_code) AS product_name,
                    o.amount_total, o.currency, identity.primary_external_userid AS order_external_userid,
                    identity.mobile AS mobile_snapshot, '' AS payer_openid, o.unionid,
                    CASE
                        WHEN o.deal_recorded IS TRUE OR o.status_code::text = '30' OR o.business_status IN ('deal', 'paid') THEN 'paid'
                        WHEN o.business_status IN ('closed', 'cancelled') THEN 'closed'
                        ELSE COALESCE(NULLIF(o.business_status, ''), 'pending')
                    END AS status,
                    CASE
                        WHEN o.deal_recorded IS TRUE OR o.status_code::text = '30' OR o.business_status IN ('deal', 'paid') THEN 'SUCCESS'
                        ELSE o.status_code::text
                    END AS trade_state,
                    o.refunded_amount_total, '' AS refund_status, o.paid_at, o.created_at,
                    COALESCE(o.paid_at, o.created_at) AS sort_at
                FROM wechat_shop_orders o
                JOIN identity_scope identity ON identity.unionid = o.unionid
                ORDER BY COALESCE(o.paid_at, o.created_at) DESC, o.order_id DESC
                LIMIT :candidate_limit
            ),
            candidate_orders AS (
                SELECT * FROM unionid_orders
                UNION ALL
                SELECT * FROM wechat_shop_unionid_orders
            ),
            deduped_orders AS (
                SELECT DISTINCT ON (provider, id) *
                FROM candidate_orders
                ORDER BY provider, id, sort_at DESC NULLS LAST
            )
            SELECT
                provider, channel, channel_label, id, out_trade_no, transaction_id, product_code,
                product_name, amount_total, currency, order_external_userid,
                mobile_snapshot, payer_openid, unionid, status, trade_state,
                refunded_amount_total, refund_status, paid_at, created_at
            FROM deduped_orders
            ORDER BY sort_at DESC NULLS LAST, id DESC
            LIMIT :limit
            """,
            {
                "unionid": _text(identity.unionid),
                "canonical_external_userid": _text(identity.external_userid),
                "canonical_mobile": _text(identity.mobile),
                "limit": safe_limit,
                "candidate_limit": candidate_limit,
            },
        )

    def list_customer_service_period_orders(self, *, external_userid: str, mobile: str = "", limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = _limit(limit, default=20, maximum=50)
        identity = self._resolve_identity(external_userid=external_userid, mobile=mobile)
        if identity is None:
            return []
        return self._all(
            f"""
            WITH identity_scope(unionid, mobile) AS (VALUES (:unionid, :mobile))
            SELECT
                e.id::text AS entitlement_id,
                e.service_product_id::text AS service_product_id,
                e.trade_product_id::text AS trade_product_id,
                e.unionid,
                e.external_userid_snapshot,
                e.status,
                e.start_at,
                e.end_at,
                e.last_order_id::text AS last_order_id,
                e.last_out_trade_no,
                COALESCE(NULLIF(e.metadata_json->>'admin_remark', ''), NULLIF(e.metadata_json->>'remark', '')) AS remark,
                sp.duration_days,
                sp.link_slug,
                p.product_code,
                COALESCE(NULLIF(p.name, ''), p.product_code) AS product_name,
                p.amount_total AS product_amount_total,
                p.currency,
                o.id::text AS order_id,
                o.amount_total AS last_order_amount,
                o.paid_at AS last_order_paid_at,
                o.created_at AS last_order_created_at,
                {huangyoucan_usage_select_fields()}
            FROM service_period_entitlements e
            JOIN service_period_products sp ON sp.id = e.service_product_id
            JOIN wechat_pay_products p ON p.id = e.trade_product_id
            LEFT JOIN wechat_pay_orders o ON o.id = e.last_order_id
            LEFT JOIN identity_scope identity ON identity.unionid = e.unionid
            {huangyoucan_usage_match_joins(unionid_sql="identity.unionid", mobile_sql="identity.mobile")}
            WHERE e.tenant_id = 'aicrm'
              AND e.status IN ('active', 'expired')
              AND identity.unionid IS NOT NULL
            ORDER BY
                CASE WHEN e.status = 'active' AND e.end_at > CURRENT_TIMESTAMP THEN 0 ELSE 1 END,
                e.end_at DESC NULLS LAST,
                e.id DESC
            LIMIT :limit
            """,
            {"unionid": _text(identity.unionid), "mobile": _text(identity.mobile), "limit": safe_limit},
        )

    def get_customer_service_period_order(self, *, external_userid: str, entitlement_id: str, mobile: str = "") -> dict[str, Any] | None:
        identity = self._resolve_identity(external_userid=external_userid, mobile=mobile)
        if identity is None:
            return None
        rows = self._all(
            f"""
            WITH identity_scope(unionid, mobile) AS (VALUES (:unionid, :mobile))
            SELECT
                e.id::text AS entitlement_id,
                e.service_product_id::text AS service_product_id,
                e.trade_product_id::text AS trade_product_id,
                e.unionid,
                e.external_userid_snapshot,
                e.status,
                e.start_at,
                e.end_at,
                e.last_order_id::text AS last_order_id,
                e.last_out_trade_no,
                COALESCE(NULLIF(e.metadata_json->>'admin_remark', ''), NULLIF(e.metadata_json->>'remark', '')) AS remark,
                sp.duration_days,
                sp.link_slug,
                p.product_code,
                COALESCE(NULLIF(p.name, ''), p.product_code) AS product_name,
                p.amount_total AS product_amount_total,
                p.currency,
                o.id::text AS order_id,
                o.amount_total AS last_order_amount,
                o.paid_at AS last_order_paid_at,
                o.created_at AS last_order_created_at,
                {huangyoucan_usage_select_fields()}
            FROM service_period_entitlements e
            JOIN service_period_products sp ON sp.id = e.service_product_id
            JOIN wechat_pay_products p ON p.id = e.trade_product_id
            LEFT JOIN wechat_pay_orders o ON o.id = e.last_order_id
            LEFT JOIN identity_scope identity ON identity.unionid = e.unionid
            {huangyoucan_usage_match_joins(unionid_sql="identity.unionid", mobile_sql="identity.mobile")}
            WHERE e.tenant_id = 'aicrm'
              AND e.id::text = :entitlement_id
              AND e.status IN ('active', 'expired')
              AND identity.unionid IS NOT NULL
            LIMIT 1
            """,
            {
                "unionid": _text(identity.unionid),
                "mobile": _text(identity.mobile),
                "entitlement_id": entitlement_id,
            },
        )
        return rows[0] if rows else None

    def list_questionnaire_answers(self, *, external_userid: str, mobile: str = "") -> list[dict[str, Any]]:
        return self._all(
            """
            WITH customer_scope AS (
                SELECT COALESCE(root.id, c.id) customer_id, im.unionid
                FROM wecom_external_contact_identity_map im JOIN customers c ON c.id = im.customer_id
                LEFT JOIN customers root ON root.id = c.merged_into_customer_id
                WHERE im.external_userid = :external_userid AND im.status = 'active'
                ORDER BY im.updated_at DESC, im.id DESC LIMIT 1
            )
            SELECT s.id submission_id, q.id questionnaire_id,
                   COALESCE(NULLIF(q.title, ''), NULLIF(q.name, ''), q.slug, '未命名问卷') questionnaire_title,
                   s.submitted_at, a.question_id, COALESCE(NULLIF(a.question_title_snapshot, ''), '未命名问题') question,
                   a.selected_option_texts_snapshot, a.text_value
            FROM questionnaire_submissions s
            JOIN customer_scope scope ON s.customer_id = scope.customer_id
              OR (s.customer_id IS NULL AND s.unionid <> '' AND s.unionid = scope.unionid)
            LEFT JOIN questionnaires q ON q.id = s.questionnaire_id LEFT JOIN questionnaire_submission_answers a ON a.submission_id = s.id
            ORDER BY s.submitted_at DESC, s.id DESC, a.id ASC
            """,
            {"external_userid": external_userid},
        )
    def list_other_staff_messages(
        self,
        external_userid: str,
        *,
        current_userid: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        identity = self._resolve_identity(external_userid=external_userid)
        if identity is None:
            return []
        safe_limit = _limit(limit, default=20, maximum=100)
        return self._all(
            """
            SELECT message.id, message.msgid, message.chat_type,
                   :canonical_external_userid AS external_userid,
                   message.owner_userid, message.sender, message.receiver,
                   message.msgtype, message.content, message.send_time, message.raw_payload, message.created_at
            FROM archived_messages message
            WHERE message.unionid = :unionid
              AND message.unionid <> ''
              AND message.send_time IS NOT NULL
              AND message.sender <> ''
              AND message.sender <> :requested_external_userid
              AND message.sender <> :canonical_external_userid
              AND message.sender <> :current_userid
              AND message.msgtype IN ('text', 'image')
            ORDER BY message.send_time DESC, message.id DESC
            LIMIT :limit_plus_one
            """,
            {
                "unionid": _text(identity.unionid),
                "requested_external_userid": _text(external_userid),
                "canonical_external_userid": _text(identity.external_userid),
                "current_userid": _text(current_userid),
                "limit_plus_one": safe_limit + 1,
            },
        )

    def owner_names(self, userids: set[str]) -> dict[str, str]:
        normalized = sorted({_text(value) for value in userids if _text(value)})
        if not normalized:
            return {}
        rows = self._all(
            """
            SELECT userid, display_name
            FROM owner_role_map
            WHERE userid = ANY(:userids)
            """,
            {"userids": normalized},
        )
        return {_text(row.get("userid")): _text(row.get("display_name")) for row in rows if _text(row.get("userid"))}

    def group_names(self, chat_ids: set[str]) -> dict[str, str]:
        normalized = sorted({_text(value) for value in chat_ids if _text(value)})
        if not normalized:
            return {}
        rows = self._all(
            """
            SELECT chat_id, group_name
            FROM group_chats
            WHERE chat_id = ANY(:chat_ids)
            """,
            {"chat_ids": normalized},
        )
        return {_text(row.get("chat_id")): _text(row.get("group_name")) for row in rows if _text(row.get("chat_id"))}

    def _one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    def _all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            return [dict(row) for row in conn.execute(text(sql), params).mappings()]


def _snapshot_owner_candidates(
    *,
    repo: SidebarV2SqlRepository,
    external_userid: str,
    contact: dict[str, Any],
    identity: dict[str, Any],
    binding: dict[str, Any],
) -> set[str]:
    candidates = {_text(identity.get("follow_user_userid"))}
    owner_list = getattr(repo, "get_contact_owner_userids", None)
    if callable(owner_list):
        candidates.update(owner_list(external_userid))
    candidates.discard("")
    return candidates


def _assert_snapshot_owner_scope(
    *,
    repo: SidebarV2SqlRepository,
    external_userid: str,
    owner_userid: str,
    owner_verified: bool,
    contact: dict[str, Any],
    identity: dict[str, Any],
    binding: dict[str, Any],
) -> None:
    if owner_verified: return
    owner_candidates = _snapshot_owner_candidates(
        repo=repo,
        external_userid=external_userid,
        contact=contact,
        identity=identity,
        binding=binding,
    )
    if _text(owner_userid) not in owner_candidates:
        raise CustomerScopeForbiddenError("customer scope forbidden")


def verify_sidebar_identity_snapshot_owner_scope(
    *,
    external_userid: str,
    owner_userid: str,
    owner_verified: bool = False,
    repo: SidebarV2SqlRepository | None = None,
) -> None:
    normalized_external = _text(external_userid)
    normalized_owner = _text(owner_userid)
    if not normalized_external:
        raise ValueError("external_userid is required")
    if not normalized_owner:
        raise ValueError("owner_userid is required")
    sql_repo = repo or SidebarV2SqlRepository()
    contact = sql_repo.get_contact_snapshot(normalized_external) or {}
    identity = sql_repo.get_external_identity_snapshot(normalized_external) or {}
    binding = sql_repo.get_contact_binding_status(normalized_external)
    if not contact and not identity and not binding.get("is_bound"):
        raise NotFoundError("customer not found")
    _assert_snapshot_owner_scope(
        repo=sql_repo,
        external_userid=normalized_external,
        owner_userid=normalized_owner,
        owner_verified=owner_verified,
        contact=contact,
        identity=identity,
        binding=binding,
    )


def _customer_from_identity_snapshot(
    *,
    repo: SidebarV2SqlRepository,
    external_userid: str,
    owner_userid: str,
    owner_verified: bool = False,
    context_source_status: str = "identity_snapshot_fallback",
) -> tuple[dict[str, Any], dict[str, Any]]:
    contact = repo.get_contact_snapshot(external_userid) or {}
    identity = repo.get_external_identity_snapshot(external_userid) or {}
    binding = repo.get_contact_binding_status(external_userid)
    if not contact and not identity and not binding.get("is_bound"):
        raise NotFoundError("customer not found")
    _assert_snapshot_owner_scope(
        repo=repo,
        external_userid=external_userid,
        owner_userid=owner_userid,
        owner_verified=owner_verified,
        contact=contact,
        identity=identity,
        binding=binding,
    )
    customer, resolution = _resolve_customer_payload(
        context={},
        binding=binding,
        contacts=contact,
        identity_map=identity,
        external_userid=external_userid,
        owner_userid=owner_userid,
    )
    return customer, {"context_source_status": context_source_status, **resolution}


class SidebarWorkbenchReadModel:
    def __init__(
        self,
        repo: SidebarV2SqlRepository | None = None,
        *,
        context_query: GetCustomerContextQuery | None = None,
        live_source_repo: CustomerReadRepository | None = None,
    ) -> None:
        self._repo = repo
        self._context_query = context_query or GetCustomerContextQuery()
        self._live_source_repo = live_source_repo

    def _sql_repo(self) -> SidebarV2SqlRepository:
        if self._repo is None:
            self._repo = SidebarV2SqlRepository()
        return self._repo

    def __call__(self, *, external_userid: str, owner_userid: str = "", owner_verified: bool = False) -> dict[str, Any]:
        customer, context, diagnostics, fallback_profile = self._customer_snapshot(
            external_userid=external_userid,
            owner_userid=owner_userid,
            owner_verified=owner_verified,
        )
        normalized_external = _text(external_userid)
        repo = self._sql_repo()
        profile = fallback_profile if fallback_profile is not None else (repo.get_profile_fields(normalized_external) or {})
        sidebar_context = dict((context.get("customer") or {}).get("sidebar_context") or {})
        workflow_title = (
            _text(sidebar_context.get("workflow_title"))
            or _text(sidebar_context.get("sop_title"))
            or repo.get_workflow_title_for_customer(normalized_external)
        )
        payload = {
            "ok": True,
            "customer": customer,
            "workflow": {"title": workflow_title},
            "profile": self._profile_payload(normalized_external, context, persisted=profile),
            "modules": list(MODULES),
            "diagnostics": diagnostics,
        }
        return _with_route_owner(payload)

    def _customer_snapshot(
        self,
        *,
        external_userid: str,
        owner_userid: str = "",
        owner_verified: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None]:
        normalized_external = _text(external_userid)
        if not normalized_external:
            raise ValueError("external_userid is required")
        normalized_owner = _text(owner_userid)
        if not normalized_owner:
            raise ValueError("owner_userid is required")
        try:
            context, context_diagnostics = self._context(
                normalized_external,
                owner_userid=normalized_owner,
                owner_verified=owner_verified,
            )
        except NotFoundError:
            if self._repo is None:
                from aicrm_next.platform.shared.runtime import production_data_ready

                if not production_data_ready():
                    raise
            context, context_diagnostics = {}, {"context_source_status": "not_found"}
        repo = self._sql_repo()
        contact = repo.get_contact_snapshot(normalized_external) or {}
        identity = repo.get_external_identity_snapshot(normalized_external) or {}
        binding = repo.get_contact_binding_status(normalized_external)
        fallback_profile: dict[str, Any] | None = None
        if not context.get("customer") and not contact and not identity and not binding.get("is_bound"):
            # Preserve the historical profile-only fallback without making every
            # panel request pay for a full profile read.
            fallback_profile = repo.get_profile_fields(normalized_external) or {}
            if not fallback_profile:
                raise NotFoundError("customer not found")
        if not context.get("customer") or context_diagnostics.get("context_source_status") in {"missing", "error", "live_source", "not_found"}:
            _assert_snapshot_owner_scope(
                repo=repo,
                external_userid=normalized_external,
                owner_userid=normalized_owner,
                owner_verified=owner_verified,
                contact=contact,
                identity=identity,
                binding=binding,
            )
            if not context.get("customer"):
                context_diagnostics["context_source_status"] = "identity_snapshot_fallback"
        customer, resolution = _resolve_customer_payload(
            context=context,
            binding=binding,
            contacts=contact,
            identity_map=identity,
            external_userid=normalized_external,
            owner_userid=normalized_owner,
        )
        self._overlay_paid_order_mobile(customer, diagnostics := {**context_diagnostics, **resolution})
        return customer, context, diagnostics, fallback_profile

    def customer_with_overlay(
        self,
        *,
        external_userid: str,
        owner_userid: str = "",
        owner_verified: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        customer, _context, diagnostics, _fallback_profile = self._customer_snapshot(
            external_userid=external_userid,
            owner_userid=owner_userid,
            owner_verified=owner_verified,
        )
        return customer, diagnostics

    def _context(
        self,
        external_userid: str,
        *,
        owner_userid: str = "",
        owner_verified: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            payload = self._context_query(
                CustomerContextRequest(
                    external_userid=external_userid,
                    owner_userid=_text(owner_userid) or None,
                    require_owner_scope=True,
                    owner_verified=owner_verified,
                    include_activity=False,
                    recent_message_limit=20,
                    timeline_limit=20,
                )
            )
            if (payload or {}).get("ok", True) and payload.get("customer"):
                return dict(payload or {}), {"context_source_status": payload.get("source_status") or "next_read_model"}
        except Exception:
            if _text(owner_userid) and not owner_verified:
                raise
        repo = self._live_source_repo
        owned_repo = repo is None
        try:
            repo = repo or build_customer_live_source_repository()
            customer = repo.get_customer(external_userid)
            if not customer:
                return {}, {"context_source_status": "missing"}
            return (
                {
                    "ok": True,
                    "customer": customer,
                    "binding": dict(customer.get("binding") or {}),
                    "messages": [],
                    "timeline": {"items": []},
                },
                {"context_source_status": "live_source"},
            )
        except Exception as exc:
            return {}, {"context_source_status": "error", "context_error": str(exc).strip() or exc.__class__.__name__}
        finally:
            if owned_repo:
                _close_repository(repo)

    def _overlay_paid_order_mobile(self, customer: dict[str, Any], diagnostics: dict[str, Any]) -> None:
        if customer.get("is_bound") or _text(customer.get("mobile")):
            diagnostics["paid_order_mobile_binding"] = {"ok": True, "status": "already_bound"}
            return
        candidate = self._sql_repo().get_bindable_wechat_pay_order_mobile(_text(customer.get("external_userid")))
        mobile = _text((candidate or {}).get("mobile_snapshot"))
        if not mobile:
            diagnostics["paid_order_mobile_binding"] = {"ok": True, "status": "no_single_candidate"}
            return
        customer["mobile"] = mobile
        customer["is_bound"] = True
        diagnostics["paid_order_mobile_binding"] = {"ok": True, "status": "read_overlay"}

    def _profile_payload(self, external_userid: str, context: dict[str, Any], *, persisted: dict[str, Any] | None = None) -> dict[str, str]:
        persisted = persisted if persisted is not None else self._sql_repo().get_profile_fields(external_userid) or {}
        if persisted:
            return {
                "source": _text(persisted.get("source")),
                "industry": _text(persisted.get("industry")),
                "industry_description": _text(persisted.get("industry_description")),
                "needs_blockers_followup": _text(persisted.get("needs_blockers_followup")),
            }
        sidebar_context = dict((context.get("customer") or {}).get("sidebar_context") or {})
        return {
            "source": _customer_text(sidebar_context.get("source")),
            "industry": _customer_text(sidebar_context.get("industry")),
            "industry_description": _customer_text(sidebar_context.get("industry_description")),
            "needs_blockers_followup": _customer_text(sidebar_context.get("needs_blockers_followup")),
        }


class SidebarQuestionnaireReadModel:
    def __init__(
        self,
        repo: SidebarV2SqlRepository | None = None,
        *,
        context_query: GetCustomerContextQuery | None = None,
        live_source_repo: CustomerReadRepository | None = None,
    ) -> None:
        self._repo = repo
        self._context_query = context_query
        self._live_source_repo = live_source_repo

    def _sql_repo(self) -> SidebarV2SqlRepository:
        if self._repo is None:
            self._repo = SidebarV2SqlRepository()
        return self._repo

    def __call__(self, *, external_userid: str, owner_userid: str = "", owner_verified: bool = False) -> dict[str, Any]:
        try:
            customer, diagnostics = SidebarWorkbenchReadModel(
                self._repo,
                context_query=self._context_query,
                live_source_repo=self._live_source_repo,
            ).customer_with_overlay(external_userid=external_userid, owner_userid=owner_userid, owner_verified=owner_verified)
        except NotFoundError as primary_not_found:
            try:
                customer, diagnostics = _customer_from_identity_snapshot(
                    repo=self._sql_repo(),
                    external_userid=_text(external_userid),
                    owner_userid=_text(owner_userid),
                    owner_verified=owner_verified,
                    context_source_status="identity_snapshot_fallback",
                )
            except NotFoundError:
                raise
            except Exception:
                raise primary_not_found
        rows = self._sql_repo().list_questionnaire_answers(external_userid=_text(external_userid), mobile=_text(customer.get("mobile")))
        return _with_route_owner({"ok": True, "questionnaires": self._group(rows), "diagnostics": diagnostics})

    def _group(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        order: list[tuple[str, str, str]] = []
        for row in rows:
            submission_id = _text(row.get("submission_id"))
            questionnaire_id = _text(row.get("questionnaire_id"))
            submitted_at = _format_time(row.get("submitted_at"))
            title = _questionnaire_title_text(row.get("questionnaire_title")) or "未命名问卷"
            key = (submission_id or questionnaire_id, title, submitted_at)
            if key not in grouped:
                order.append(key)
                grouped[key] = {
                    "id": submission_id or questionnaire_id or f"q_{len(order)}",
                    "submission_id": submission_id,
                    "questionnaire_id": questionnaire_id,
                    "title": title,
                    "submitted_at": submitted_at,
                    "answer_count": 0,
                    "total_count": 0,
                    "answers": [],
                }
            answer = self._answer_text(row)
            question = _question_text(row.get("question"))
            if question or answer:
                grouped[key]["answers"].append({"question": question or "未命名问题", "answer": answer})
                grouped[key]["answer_count"] += 1
                grouped[key]["total_count"] += 1
        return [grouped[key] for key in order if grouped[key]["answer_count"] > 0]

    def _answer_text(self, row: dict[str, Any]) -> str:
        selected = _json(row.get("selected_option_texts_snapshot"), [])
        selected_text = "、".join(_answer_text_value(item) for item in selected if _answer_text_value(item)) if isinstance(selected, list) else ""
        detail_text = _answer_text_value(row.get("text_value"))
        return f"{selected_text}（{detail_text}）" if selected_text and detail_text else selected_text or detail_text


class SidebarMaterialReadModel:
    def __call__(self, *, material_type: str, limit: int = 50, offset: int = 0, q: str = "") -> dict[str, Any]:
        normalized_type = _text(material_type)
        safe_limit = _limit(limit, default=50, maximum=200)
        safe_offset, normalized_query = max(0, _int(offset)), _text(q)[:100]
        kind_map = {"image": "image", "mini": "miniprogram", "pdf": "attachment"}
        if normalized_type not in kind_map:
            raise ValueError("type must be image, mini, or pdf")
        filters: dict[str, Any] = {"enabled_only": True}
        if normalized_type == "image" and normalized_query:
            filters["q"] = normalized_query
        payload = ListMediaItemsQuery(kind_map[normalized_type])(limit=safe_limit, offset=safe_offset, filters=filters)
        rows = list(payload.get("items") or [])
        total, next_offset = max(0, _int(payload.get("total"))), safe_offset + len(rows)
        return _with_route_owner(
            {
                "ok": True,
                "materials": [self._material_item(dict(item), normalized_type) for item in rows],
                "quick_keywords": _sidebar_image_quick_keywords() if normalized_type == "image" else [],
                "total": total,
                "limit": safe_limit,
                "offset": safe_offset,
                "has_more": next_offset < total,
                "next_offset": next_offset if next_offset < total else None,
            }
        )

    def thumbnail(self, image_id: int) -> dict[str, Any]:
        try:
            payload = GetImageThumbnailQuery()(str(int(image_id)), 160, enabled_only=True)
        except NotFoundError:
            raise LookupError("image not found") from None
        except ContractError as exc:
            raise ValueError("invalid image data") from exc
        thumbnail = dict(payload.get("thumbnail") or {})
        redirect_url = _text(thumbnail.get("redirect_url"))
        if redirect_url:
            return {"redirect_url": redirect_url}
        return {
            "body": thumbnail.get("bytes") or b"",
            "mime_type": _text(thumbnail.get("mime_type")) or "image/png",
            "etag": _text(thumbnail.get("etag")),
            "generation_required": bool(thumbnail.get("generation_required")),
        }

    def _material_item(self, item: dict[str, Any], material_type: str) -> dict[str, Any]:
        item_id = _int(item.get("id"))
        thumbnail_url = ""
        if material_type == "image":
            title = _text(item.get("name")) or _text(item.get("file_name")) or "未命名图片素材"
            label = "图"
            if item_id:
                version = quote(_text(item.get("updated_at")), safe="")
                thumbnail_url = f"/api/sidebar/v2/materials/image/{item_id}/thumbnail"
                if version:
                    thumbnail_url += f"?v={version}"
        elif material_type == "mini":
            title = _text(item.get("title")) or _text(item.get("name")) or "未命名小程序素材"
            label = "小"
        else:
            title = _text(item.get("name")) or _text(item.get("file_name")) or "未命名 PDF 素材"
            label = "PDF"
        tags = [_text(tag) for tag in list(item.get("tags") or []) if _text(tag)][:3]
        return {
            "id": item_id,
            "type": material_type,
            "title": title,
            "thumbnail_label": label,
            "thumbnail_url": thumbnail_url,
            "tags": tags,
            "enabled": bool(item.get("enabled", True)),
        }


class SidebarOtherStaffMessagesReadModel:
    def __init__(self, repo: SidebarV2SqlRepository | None = None) -> None:
        self._repo = repo

    def _sql_repo(self) -> SidebarV2SqlRepository:
        if self._repo is None:
            self._repo = SidebarV2SqlRepository()
        return self._repo

    def __call__(self, *, external_userid: str, current_userid: str = "", limit: int = 20) -> dict[str, Any]:
        if not _text(external_userid):
            raise ValueError("external_userid is required")
        safe_limit = _limit(limit, default=20, maximum=100)
        repo = self._sql_repo()
        rows = repo.list_other_staff_messages(
            _text(external_userid),
            current_userid=_text(current_userid),
            limit=safe_limit,
        )
        has_more = len(rows) > safe_limit
        selected = list(reversed(rows[:safe_limit]))
        staff_names = repo.owner_names({_text(item.get("sender")) for item in selected})
        chat_ids = {self._chat_id(item) for item in selected}
        group_names = repo.group_names(chat_ids)
        return _with_route_owner(
            {
                "ok": True,
                "messages": [self._message_item(item, staff_names, group_names) for item in selected],
                "has_more": has_more,
            }
        )

    def _chat_id(self, message: dict[str, Any]) -> str:
        payload = _json(message.get("raw_payload"), {})
        decrypted = dict(payload.get("decrypted_message") or {}) if isinstance(payload, dict) else {}
        return _text(message.get("chat_id")) or _text(message.get("roomid")) or _text(decrypted.get("roomid"))

    def _message_item(self, message: dict[str, Any], staff_names: dict[str, str], group_names: dict[str, str]) -> dict[str, Any]:
        sender = _text(message.get("sender"))
        msgtype = _text(message.get("msgtype"))
        chat_id = self._chat_id(message)
        scene = "group" if _text(message.get("chat_type")) == "group" or chat_id else "private"
        scene_label = group_names.get(chat_id) or ("群聊" if scene == "group" else "私聊")
        staff_name = staff_names.get(sender) or sender
        return {
            "id": _text(message.get("id")) or _text(message.get("msgid")),
            "type": msgtype,
            "content": _text(message.get("content")) if msgtype == "text" else "发送了图片",
            "send_time": _format_time(message.get("send_time")),
            "scene": scene,
            "scene_label": scene_label,
            "staff_name": staff_name,
            "staff_userid": sender,
            "sender_label": staff_name,
        }


class SidebarCommerceReadModel:
    def __init__(
        self,
        repo: SidebarV2SqlRepository | None = None,
        *,
        context_query: GetCustomerContextQuery | None = None,
        live_source_repo: CustomerReadRepository | None = None,
    ) -> None:
        self._repo = repo
        self._context_query = context_query or GetCustomerContextQuery()
        self._live_source_repo = live_source_repo

    def _sql_repo(self) -> SidebarV2SqlRepository:
        if self._repo is None:
            self._repo = SidebarV2SqlRepository()
        return self._repo

    def products(self, *, external_userid: str, owner_userid: str = "", bind_by_userid: str = "") -> dict[str, Any]:
        normalized_external = _text(external_userid)
        if not normalized_external:
            raise ValueError("external_userid is required")
        diagnostics = {"context_source": "sidebar_product_link"}
        context_token = ""
        context_status = "missing"
        if _text(owner_userid):
            try:
                context_token = build_sidebar_product_context_token(
                    external_userid=normalized_external,
                    owner_userid=_text(owner_userid),
                    bind_by_userid=_text(bind_by_userid) or _text(owner_userid),
                )
                context_status = "signed"
            except Exception as exc:
                context_status = "sign_failed"
                diagnostics["context_error"] = str(exc).strip() or exc.__class__.__name__
        else:
            context_status = "owner_pending"
        diagnostics["context_status"] = context_status
        rows = self._list_sidebar_products(limit=100)
        active = [dict(item) for item in rows if bool(item.get("enabled")) and _text(item.get("status")) == "active"]
        service_period_rows = self._list_sidebar_service_period_products(limit=100)
        active_service_period = [
            dict(item)
            for item in service_period_rows
            if bool(item.get("enabled")) and _text(item.get("status")) == "active" and _text(item.get("link_slug"))
        ]
        return _with_route_owner(
            {
                "ok": True,
                "products": [self._product_item(item, context_token=context_token, context_status=context_status) for item in active],
                "service_period_products": [
                    self._service_period_product_item(item, context_token=context_token, context_status=context_status)
                    for item in active_service_period
                ],
                "diagnostics": diagnostics,
            }
        )

    def _customer_for_order_context(
        self,
        *,
        external_userid: str,
        owner_userid: str,
        owner_verified: bool = False,
        diagnostics_key: str = "orders_context",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            customer, diagnostics = SidebarWorkbenchReadModel(
                self._repo,
                context_query=self._context_query,
                live_source_repo=self._live_source_repo,
            ).customer_with_overlay(
                external_userid=external_userid,
                owner_userid=owner_userid,
                owner_verified=owner_verified,
            )
            context_status = (
                "identity_snapshot_fallback"
                if diagnostics.get("context_source_status") == "identity_snapshot_fallback"
                else "workbench_customer_overlay"
            )
            return customer, {**diagnostics, diagnostics_key: context_status}
        except NotFoundError as primary_not_found:
            try:
                customer, diagnostics = _customer_from_identity_snapshot(
                    repo=self._sql_repo(),
                    external_userid=external_userid,
                    owner_userid=owner_userid,
                    owner_verified=owner_verified,
                )
                return customer, {**diagnostics, diagnostics_key: "identity_snapshot_fallback"}
            except NotFoundError:
                raise
            except Exception:
                raise primary_not_found

    def orders(self, *, external_userid: str, owner_userid: str = "", owner_verified: bool = False) -> dict[str, Any]:
        normalized_external = _text(external_userid)
        normalized_owner = _text(owner_userid)
        if not normalized_external:
            raise ValueError("external_userid is required")
        if not normalized_owner:
            raise ValueError("owner_userid is required")
        customer, diagnostics = self._customer_for_order_context(
            external_userid=normalized_external,
            owner_userid=normalized_owner,
            owner_verified=owner_verified,
            diagnostics_key="orders_context",
        )
        rows = self._sql_repo().list_customer_wechat_pay_orders(
            external_userid=normalized_external,
            mobile=_text(customer.get("mobile")),
            limit=20,
        )
        return _with_route_owner(
            {
                "ok": True,
                "orders": [self._order_item(dict(item)) for item in rows],
                "customer": customer,
                "diagnostics": diagnostics,
            }
        )

    def periodic_orders(self, *, external_userid: str, owner_userid: str = "", owner_verified: bool = False) -> dict[str, Any]:
        normalized_external = _text(external_userid)
        normalized_owner = _text(owner_userid)
        if not normalized_external:
            raise ValueError("external_userid is required")
        if not normalized_owner:
            raise ValueError("owner_userid is required")
        customer, diagnostics = self._customer_for_order_context(
            external_userid=normalized_external,
            owner_userid=normalized_owner,
            owner_verified=owner_verified,
            diagnostics_key="periodic_orders_context",
        )
        rows = self._sql_repo().list_customer_service_period_orders(
            external_userid=normalized_external,
            mobile=_text(customer.get("mobile")),
            limit=20,
        )
        items = [item for item in (self._periodic_order_item(dict(row)) for row in rows) if item]
        return _with_route_owner(
            {
                "ok": True,
                "periodic_orders": items,
                "customer": customer,
                "diagnostics": diagnostics,
            }
        )

    def periodic_order_remark_target(
        self,
        *,
        external_userid: str,
        owner_userid: str = "",
        owner_verified: bool = False,
        entitlement_id: str,
    ) -> dict[str, Any]:
        normalized_external = _text(external_userid)
        normalized_owner = _text(owner_userid)
        normalized_entitlement = _text(entitlement_id)
        if not normalized_external:
            raise ValueError("external_userid is required")
        if not normalized_owner:
            raise ValueError("owner_userid is required")
        if not normalized_entitlement:
            raise ValueError("entitlement_id is required")
        customer, diagnostics = self._customer_for_order_context(
            external_userid=normalized_external,
            owner_userid=normalized_owner,
            owner_verified=owner_verified,
            diagnostics_key="periodic_orders_context",
        )
        row = self._sql_repo().get_customer_service_period_order(
            external_userid=normalized_external,
            entitlement_id=normalized_entitlement,
            mobile=_text(customer.get("mobile")),
        )
        if not row:
            raise NotFoundError("periodic order not found")
        item = self._periodic_order_item(dict(row))
        if not item or item["status"] not in {"active", "expired"}:
            raise NotFoundError("periodic order not found")
        unionid = _text(row.get("unionid"))
        if not unionid:
            raise NotFoundError("periodic order member not found")
        return {
            "entitlement_id": normalized_entitlement,
            "service_product_id": _text(row.get("service_product_id")),
            "unionid": unionid,
            "periodic_order": item,
            "customer": customer,
            "diagnostics": diagnostics,
        }

    def _list_sidebar_products(self, *, limit: int) -> list[dict[str, Any]]:
        repo = build_commerce_repository()
        if hasattr(repo, "list_sidebar_active_products"):
            return list(repo.list_sidebar_active_products(limit=limit, offset=0).get("items") or [])
        return list(ListProductsQuery(repo=repo)(limit=limit, offset=0).get("items") or [])

    def _list_sidebar_service_period_products(self, *, limit: int) -> list[dict[str, Any]]:
        return list(ListServicePeriodProductsQuery()(limit=limit, offset=0).get("items") or [])

    def _product_item(self, item: dict[str, Any], *, context_token: str = "", context_status: str = "") -> dict[str, Any]:
        product_code = _text(item.get("product_code"))
        product_id = _text(item.get("id"))
        public_path = f"/p/{product_code}" if product_code else ""
        checkout_path = f"/pay/{product_code}" if product_code else ""
        product_url = append_ctx_fragment(public_path, context_token) if context_token else public_path
        checkout_url = append_ctx_fragment(checkout_path, context_token) if context_token else checkout_path
        return {
            "id": product_code or product_id,
            "title": _text(item.get("title") or item.get("name")) or product_code or "未命名商品",
            "price_label": _money_label(item.get("price_cents") or item.get("amount_total")),
            "product_url": product_url,
            "checkout_url": checkout_url,
            "context_source": "sidebar_product_link" if context_token else "",
            "context_status": context_status,
        }

    def _service_period_product_item(
        self,
        item: dict[str, Any],
        *,
        context_token: str = "",
        context_status: str = "",
    ) -> dict[str, Any]:
        service_product_id = _text(item.get("id") or item.get("service_product_id"))
        link_slug = _text(item.get("link_slug"))
        public_path = f"/s/{link_slug}" if link_slug else ""
        product_url = append_ctx_fragment(public_path, context_token) if context_token else public_path
        return {
            "id": service_product_id,
            "service_product_id": service_product_id,
            "trade_product_id": _text(item.get("trade_product_id")),
            "title": _text(item.get("title") or item.get("name")) or _text(item.get("product_code")) or "未命名周期商品",
            "price_label": _money_label(item.get("price_cents") or item.get("amount_total")),
            "duration_days": _int(item.get("duration_days")),
            "link_slug": link_slug,
            "product_url": product_url,
            "context_source": "sidebar_product_link" if context_token else "",
            "context_status": context_status,
        }

    def _order_item(self, order: dict[str, Any]) -> dict[str, Any]:
        order_id = _text(order.get("id"))
        provider = _text(order.get("provider") or "wechat_pay")
        channel = _text(order.get("channel") or provider)
        channel_label = _text(order.get("channel_label")) or ("微信小店" if provider == "wechat_shop" else "微信支付")
        product_code = _text(order.get("product_code"))
        product_name = _text(order.get("product_name")) or product_code or "未命名商品"
        status = self._order_status(order)
        detail_base = "/admin/wechat-shop/transactions" if provider == "wechat_shop" else "/admin/wechat-pay/transactions"
        return {
            "id": _text(order.get("out_trade_no")) or order_id,
            "order_id": order_id,
            "provider": provider,
            "channel": channel,
            "channel_label": channel_label,
            "title": product_name,
            "amount_label": _money_label(order.get("amount_total")),
            "status_label": ORDER_STATUS_LABELS.get(status, status),
            "paid_at": _format_time(order.get("paid_at") or order.get("created_at")),
            "detail_url": f"{detail_base}/{order_id}" if order_id else "",
        }

    def _periodic_order_item(self, row: dict[str, Any]) -> dict[str, Any] | None:
        status = service_period_entitlement_status(row.get("end_at"), row.get("status"))
        if status not in {"active", "expired"}:
            return None
        order_id = _text(row.get("order_id") or row.get("last_order_id"))
        amount_total = row.get("last_order_amount")
        if amount_total in (None, ""):
            amount_total = row.get("product_amount_total")
        product_code = _text(row.get("product_code"))
        return {
            "id": _text(row.get("entitlement_id") or row.get("id")),
            "service_product_id": _text(row.get("service_product_id")),
            "trade_product_id": _text(row.get("trade_product_id")),
            "title": _text(row.get("product_name")) or product_code or "未命名周期商品",
            "product_code": product_code,
            "status": status,
            "status_label": SERVICE_PERIOD_STATUS_LABELS.get(status, status),
            "remaining_days": service_period_remaining_days(row.get("end_at")),
            "end_at": service_period_isoformat(row.get("end_at")),
            "duration_days": _int(row.get("duration_days")),
            "amount_label": _money_label(amount_total),
            "last_out_trade_no": _text(row.get("last_out_trade_no")),
            "last_order_paid_at": _format_time(row.get("last_order_paid_at") or row.get("last_order_created_at")),
            "remark": _text(row.get("remark")),
            "detail_url": f"/admin/wechat-pay/transactions/{order_id}" if order_id else "",
            **public_huangyoucan_usage_fields(row),
        }

    def _order_status(self, order: dict[str, Any]) -> str:
        amount_total = _int(order.get("amount_total"))
        refunded = _int(order.get("refunded_amount_total"))
        refund_status = _text(order.get("refund_status"))
        status = _text(order.get("status"))
        trade_state = _text(order.get("trade_state"))
        if refund_status == "full_refunded" or (amount_total > 0 and refunded >= amount_total):
            return "full_refunded"
        if refund_status == "partial_refunded" or refunded > 0:
            return "partial_refunded"
        if status == "paid" or trade_state == "SUCCESS":
            return "paid"
        if status in {"closed", "cancelled"} or trade_state in {"CLOSED", "REVOKED"}:
            return "closed"
        if status in {"failed", "error"} or trade_state == "PAYERROR":
            return "failed"
        return "pending"
