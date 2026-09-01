from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from aicrm_next.platform.shared.postgres_connection import db_session
from aicrm_next.platform.shared.runtime_settings import managed_runtime_setting


WECOM_PROVIDER = "wecom"
WECOM_EXTERNAL_IDENTITY = "external_userid"
WECHAT_PROVIDER = "wechat"
WECHAT_UNION_IDENTITY = "unionid"
WECHAT_OPEN_IDENTITY = "openid"
DEFAULT_OPEN_PLATFORM_SCOPE = "wechat_open_platform:aicrm_primary"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str)


def unionid_scope_key() -> str:
    configured = _text(managed_runtime_setting("WECHAT_OPEN_PLATFORM_SCOPE_ID"))
    return f"wechat_open_platform:{configured}" if configured else DEFAULT_OPEN_PLATFORM_SCOPE


def wechat_app_scope_key(app_id: str) -> str:
    normalized = _text(app_id)
    if not normalized:
        raise ValueError("app_id is required")
    return f"wechat_app:{normalized}"


@dataclass(frozen=True)
class EnsuredCustomer:
    customer_id: int
    identity_id: int
    created: bool
    status: str = "active"


class PostgresOneIDService:
    """Transaction boundary for verified identity attachment and customer merge."""

    def ensure_verified_identity_with_db(
        self,
        db: Any,
        *,
        provider: str,
        identity_type: str,
        scope_key: str,
        normalized_value: str,
        source_type: str,
        source_event_id: str = "",
    ) -> EnsuredCustomer:
        provider_value = _text(provider)
        type_value = _text(identity_type)
        scope_value = _text(scope_key)
        identity_value = _text(normalized_value)
        if not all((provider_value, type_value, scope_value, identity_value)):
            raise ValueError("provider, identity_type, scope_key and normalized_value are required")
        db.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
            (f"oneid:{provider_value}:{type_value}:{scope_value}:{identity_value}",),
        )
        row = db.execute(
            """
            SELECT identity.id AS identity_id, identity.customer_id
            FROM customer_identities identity
            WHERE identity.provider = ? AND identity.identity_type = ?
              AND identity.scope_key = ? AND identity.normalized_value = ?
              AND identity.status = 'active'
            FOR UPDATE
            """,
            (provider_value, type_value, scope_value, identity_value),
        ).fetchone()
        if row:
            customer_id = self._root_customer_id(db, int(row["customer_id"]), for_update=True)
            db.execute(
                "UPDATE customer_identities SET customer_id = ?, updated_at = NOW() WHERE id = ?",
                (customer_id, int(row["identity_id"])),
            )
            return EnsuredCustomer(
                customer_id=customer_id,
                identity_id=int(row["identity_id"]),
                created=False,
            )
        customer = db.execute(
            """
            INSERT INTO customers(status, identity_completeness, created_at, updated_at)
            VALUES ('active', 'single_identity', NOW(), NOW())
            RETURNING id
            """
        ).fetchone()
        customer_id = int((customer or {})["id"])
        identity = db.execute(
            """
            INSERT INTO customer_identities(
                customer_id, provider, identity_type, scope_key, normalized_value,
                assurance_level, status, verified_at, source_type, source_event_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'provider_verified', 'active', NOW(), ?, ?, NOW(), NOW())
            RETURNING id
            """,
            (
                customer_id,
                provider_value,
                type_value,
                scope_value,
                identity_value,
                _text(source_type),
                _text(source_event_id),
            ),
        ).fetchone()
        return EnsuredCustomer(customer_id=customer_id, identity_id=int((identity or {})["id"]), created=True)

    def ensure_verified_wechat_identity_with_db(
        self,
        db: Any,
        *,
        app_id: str,
        openid: str,
        unionid: str = "",
        source_type: str,
        source_event_id: str = "",
    ) -> dict[str, Any]:
        ensured = self.ensure_verified_identity_with_db(
            db,
            provider=WECHAT_PROVIDER,
            identity_type=WECHAT_OPEN_IDENTITY,
            scope_key=wechat_app_scope_key(app_id),
            normalized_value=openid,
            source_type=source_type,
            source_event_id=source_event_id,
        )
        if not _text(unionid):
            return {
                "status": "resolved",
                "action": "created" if ensured.created else "already_attached",
                "customer_id": ensured.customer_id,
                "identity_id": ensured.identity_id,
                "unionid_status": "pending",
            }
        attached = self._attach_unionid_to_customer_with_db(
            db,
            customer_id=ensured.customer_id,
            unionid=unionid,
            source_type=source_type,
            source_event_id=source_event_id,
        )
        return {**attached, "openid_identity_id": ensured.identity_id, "unionid_status": attached["status"]}

    def ensure_verified_wechat_identity(
        self,
        *,
        app_id: str,
        openid: str,
        unionid: str = "",
        source_type: str,
        source_event_id: str = "",
    ) -> dict[str, Any]:
        with db_session() as db:
            result = self.ensure_verified_wechat_identity_with_db(
                db,
                app_id=app_id,
                openid=openid,
                unionid=unionid,
                source_type=source_type,
                source_event_id=source_event_id,
            )
            db.commit()
            return result

    def ensure_verified_wecom_identity(
        self,
        *,
        corp_id: str,
        external_userid: str,
        owner_userid: str = "",
        source_event_id: str = "",
        source_type: str = "wecom_callback",
    ) -> EnsuredCustomer:
        corp = _text(corp_id)
        external = _text(external_userid)
        owner = _text(owner_userid)
        if not corp or not external:
            raise ValueError("corp_id and external_userid are required")
        with db_session() as db:
            db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
                (f"oneid:{WECOM_PROVIDER}:{corp}:{external}",),
            )
            row = db.execute(
                """
                SELECT identity.id AS identity_id,
                       COALESCE(root.id, customer.id) AS customer_id,
                       COALESCE(root.status, customer.status) AS customer_status
                FROM customer_identities identity
                JOIN customers customer ON customer.id = identity.customer_id
                LEFT JOIN customers root ON root.id = customer.merged_into_customer_id
                WHERE identity.provider = ?
                  AND identity.identity_type = ?
                  AND identity.scope_key = ?
                  AND identity.normalized_value = ?
                  AND identity.status = 'active'
                FOR UPDATE OF identity, customer
                """,
                (WECOM_PROVIDER, WECOM_EXTERNAL_IDENTITY, corp, external),
            ).fetchone()
            created = row is None
            if row:
                customer_id = int(row["customer_id"])
                identity_id = int(row["identity_id"])
                if _text(row.get("customer_status")) != "active":
                    raise RuntimeError("verified identity does not resolve to an active customer")
                db.execute(
                    "UPDATE customer_identities SET customer_id = ?, updated_at = NOW() WHERE id = ?",
                    (customer_id, identity_id),
                )
            else:
                customer = db.execute(
                    """
                    INSERT INTO customers(status, identity_completeness, created_at, updated_at)
                    VALUES ('active', 'wecom_only', NOW(), NOW())
                    RETURNING id
                    """
                ).fetchone()
                customer_id = int((customer or {})["id"])
                identity = db.execute(
                    """
                    INSERT INTO customer_identities(
                        customer_id, provider, identity_type, scope_key, normalized_value,
                        assurance_level, status, verified_at, source_type, source_event_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'provider_verified', 'active', NOW(), ?, ?, NOW(), NOW())
                    RETURNING id
                    """,
                    (
                        customer_id,
                        WECOM_PROVIDER,
                        WECOM_EXTERNAL_IDENTITY,
                        corp,
                        external,
                        _text(source_type),
                        _text(source_event_id),
                    ),
                ).fetchone()
                identity_id = int((identity or {})["id"])

            db.execute(
                """
                INSERT INTO wecom_external_contact_identity_map(
                    corp_id, external_userid, follow_user_userid, status,
                    customer_id, identity_id, first_seen_at, last_seen_at, created_at, updated_at
                ) VALUES (?, ?, ?, 'active', ?, ?, NOW(), NOW(), NOW(), NOW())
                ON CONFLICT (corp_id, external_userid) DO UPDATE SET
                    follow_user_userid = COALESCE(NULLIF(EXCLUDED.follow_user_userid, ''), wecom_external_contact_identity_map.follow_user_userid),
                    status = 'active',
                    customer_id = EXCLUDED.customer_id,
                    identity_id = EXCLUDED.identity_id,
                    last_seen_at = NOW(),
                    updated_at = NOW()
                """,
                (corp, external, owner, customer_id, identity_id),
            )
            if owner:
                db.execute(
                    """
                    INSERT INTO wecom_external_contact_follow_users(
                        corp_id, external_userid, user_id, relation_status, is_primary,
                        customer_id, identity_id, raw_follow_user,
                        first_seen_at, last_seen_at, created_at, updated_at
                    ) VALUES (?, ?, ?, 'active', TRUE, ?, ?, ?::jsonb, NOW(), NOW(), NOW(), NOW())
                    ON CONFLICT (corp_id, external_userid, user_id) DO UPDATE SET
                        relation_status = 'active',
                        customer_id = EXCLUDED.customer_id,
                        identity_id = EXCLUDED.identity_id,
                        raw_follow_user = wecom_external_contact_follow_users.raw_follow_user || EXCLUDED.raw_follow_user,
                        last_seen_at = NOW(),
                        updated_at = NOW()
                    """,
                    (
                        corp,
                        external,
                        owner,
                        customer_id,
                        identity_id,
                        _json({"userid": owner, "source": source_type}),
                    ),
                )
            db.execute(
                """
                UPDATE crm_user_identity_resolution_queue
                SET customer_id = ?, identity_id = ?, enrichment_status = CASE
                        WHEN status = 'resolved' THEN 'resolved'
                        WHEN status = 'conflict' THEN 'conflict'
                        ELSE enrichment_status
                    END,
                    updated_at = NOW()
                WHERE corp_id = ? AND external_userid = ?
                """,
                (customer_id, identity_id, corp, external),
            )
            db.commit()
            return EnsuredCustomer(customer_id=customer_id, identity_id=identity_id, created=created)

    def attach_verified_unionid(
        self,
        *,
        corp_id: str,
        external_userid: str,
        unionid: str,
        owner_userid: str = "",
        source_event_id: str = "",
    ) -> dict[str, Any]:
        external_customer = self.ensure_verified_wecom_identity(
            corp_id=corp_id,
            external_userid=external_userid,
            owner_userid=owner_userid,
            source_event_id=source_event_id,
            source_type="wecom_official_detail",
        )
        normalized_unionid = _text(unionid)
        if not normalized_unionid:
            return {
                "status": "pending",
                "customer_id": external_customer.customer_id,
                "identity_id": external_customer.identity_id,
            }
        scope = unionid_scope_key()
        with db_session() as db:
            for lock_key in sorted(
                (
                    f"oneid:{WECOM_PROVIDER}:{_text(corp_id)}:{_text(external_userid)}",
                    f"oneid:{WECHAT_PROVIDER}:{scope}:{normalized_unionid}",
                )
            ):
                db.execute("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", (lock_key,))
            external_root = self._root_customer_id(db, external_customer.customer_id, for_update=True)
            union_row = db.execute(
                """
                SELECT id, customer_id
                FROM customer_identities
                WHERE provider = ? AND identity_type = ? AND scope_key = ?
                  AND normalized_value = ? AND status = 'active'
                FOR UPDATE
                """,
                (WECHAT_PROVIDER, WECHAT_UNION_IDENTITY, scope, normalized_unionid),
            ).fetchone()
            if not union_row:
                identity = db.execute(
                    """
                    INSERT INTO customer_identities(
                        customer_id, provider, identity_type, scope_key, normalized_value,
                        assurance_level, status, verified_at, source_type, source_event_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'provider_verified', 'active', NOW(),
                              'wecom_official_detail', ?, NOW(), NOW())
                    RETURNING id
                    """,
                    (
                        external_root,
                        WECHAT_PROVIDER,
                        WECHAT_UNION_IDENTITY,
                        scope,
                        normalized_unionid,
                        _text(source_event_id),
                    ),
                ).fetchone()
                self._mark_enriched(db, external_root, external_userid, normalized_unionid)
                db.commit()
                return {
                    "status": "resolved",
                    "action": "attached",
                    "customer_id": external_root,
                    "identity_id": int((identity or {})["id"]),
                }

            union_customer = self._root_customer_id(db, int(union_row["customer_id"]), for_update=True)
            if union_customer == external_root:
                self._mark_enriched(db, union_customer, external_userid, normalized_unionid)
                db.commit()
                return {
                    "status": "resolved",
                    "action": "already_attached",
                    "customer_id": union_customer,
                    "identity_id": int(union_row["id"]),
                }

            conflict_reason = self._strong_identity_conflict(db, external_root, union_customer)
            if conflict_reason:
                self._record_conflict(
                    db,
                    left_customer_id=external_root,
                    right_customer_id=union_customer,
                    scope_key=scope,
                    unionid=normalized_unionid,
                    reason=conflict_reason,
                )
                db.execute(
                    "UPDATE customers SET identity_completeness = 'conflict', updated_at = NOW() WHERE id IN (?, ?)",
                    (external_root, union_customer),
                )
                db.execute(
                    """
                    UPDATE crm_user_identity_resolution_queue
                    SET status = 'conflict', enrichment_status = 'conflict', updated_at = NOW()
                    WHERE corp_id = ? AND external_userid = ? AND status IN ('pending', 'polling', 'held')
                    """,
                    (_text(corp_id), _text(external_userid)),
                )
                db.commit()
                return {
                    "status": "conflict",
                    "reason": conflict_reason,
                    "customer_id": external_root,
                    "unionid_customer_id": union_customer,
                }

            self._merge_into_unionid_customer(
                db,
                from_customer_id=external_root,
                to_customer_id=union_customer,
                unionid=normalized_unionid,
                source_event_id=source_event_id,
            )
            self._mark_enriched(db, union_customer, external_userid, normalized_unionid)
            db.commit()
            return {
                "status": "resolved",
                "action": "merged",
                "customer_id": union_customer,
                "merged_customer_id": external_root,
                "identity_id": int(union_row["id"]),
            }

    def _attach_unionid_to_customer_with_db(
        self,
        db: Any,
        *,
        customer_id: int,
        unionid: str,
        source_type: str,
        source_event_id: str = "",
    ) -> dict[str, Any]:
        normalized_unionid = _text(unionid)
        if not normalized_unionid:
            raise ValueError("unionid is required")
        scope = unionid_scope_key()
        db.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
            (f"oneid:{WECHAT_PROVIDER}:{WECHAT_UNION_IDENTITY}:{scope}:{normalized_unionid}",),
        )
        anchor_customer = self._root_customer_id(db, int(customer_id), for_update=True)
        union_row = db.execute(
            """
            SELECT id, customer_id
            FROM customer_identities
            WHERE provider = ? AND identity_type = ? AND scope_key = ?
              AND normalized_value = ? AND status = 'active'
            FOR UPDATE
            """,
            (WECHAT_PROVIDER, WECHAT_UNION_IDENTITY, scope, normalized_unionid),
        ).fetchone()
        if not union_row:
            identity = db.execute(
                """
                INSERT INTO customer_identities(
                    customer_id, provider, identity_type, scope_key, normalized_value,
                    assurance_level, status, verified_at, source_type, source_event_id,
                    created_at, updated_at
                ) VALUES (?, 'wechat', 'unionid', ?, ?, 'provider_verified', 'active',
                          NOW(), ?, ?, NOW(), NOW())
                RETURNING id
                """,
                (anchor_customer, scope, normalized_unionid, _text(source_type), _text(source_event_id)),
            ).fetchone()
            self._mark_ecosystem_enriched(db, anchor_customer, normalized_unionid)
            return {
                "status": "resolved",
                "action": "attached",
                "customer_id": anchor_customer,
                "identity_id": int((identity or {})["id"]),
            }
        union_customer = self._root_customer_id(db, int(union_row["customer_id"]), for_update=True)
        if union_customer == anchor_customer:
            self._mark_ecosystem_enriched(db, union_customer, normalized_unionid)
            return {
                "status": "resolved",
                "action": "already_attached",
                "customer_id": union_customer,
                "identity_id": int(union_row["id"]),
            }
        conflict_reason = self._strong_identity_conflict(db, anchor_customer, union_customer)
        if conflict_reason:
            self._record_conflict(
                db,
                left_customer_id=anchor_customer,
                right_customer_id=union_customer,
                scope_key=scope,
                unionid=normalized_unionid,
                reason=conflict_reason,
                source_type=source_type,
            )
            db.execute(
                "UPDATE customers SET identity_completeness = 'conflict', updated_at = NOW() WHERE id IN (?, ?)",
                (anchor_customer, union_customer),
            )
            return {
                "status": "conflict",
                "reason": conflict_reason,
                "customer_id": anchor_customer,
                "unionid_customer_id": union_customer,
                "identity_id": int(union_row["id"]),
            }
        self._merge_into_unionid_customer(
            db,
            from_customer_id=anchor_customer,
            to_customer_id=union_customer,
            unionid=normalized_unionid,
            source_event_id=source_event_id,
            source_type=source_type,
        )
        self._mark_ecosystem_enriched(db, union_customer, normalized_unionid)
        return {
            "status": "resolved",
            "action": "merged",
            "customer_id": union_customer,
            "merged_customer_id": anchor_customer,
            "identity_id": int(union_row["id"]),
        }

    def apply_verified_wecom_detail(
        self,
        *,
        corp_id: str,
        external_userid: str,
        owner_userid: str,
        detail_payload: dict[str, Any],
        source_event_id: str = "",
    ) -> dict[str, Any]:
        contact = dict((detail_payload or {}).get("external_contact") or detail_payload or {})
        detail_external = _text(contact.get("external_userid"))
        if detail_external and detail_external != _text(external_userid):
            raise ValueError("provider target mismatch")
        unionid = _text(contact.get("unionid"))
        resolution = self.attach_verified_unionid(
            corp_id=corp_id,
            external_userid=external_userid,
            unionid=unionid,
            owner_userid=owner_userid,
            source_event_id=source_event_id,
        )
        customer_id = int(resolution["customer_id"])
        with db_session() as db:
            identity = db.execute(
                """
                SELECT id FROM customer_identities
                WHERE provider = 'wecom' AND identity_type = 'external_userid'
                  AND scope_key = ? AND normalized_value = ? AND status = 'active'
                """,
                (_text(corp_id), _text(external_userid)),
            ).fetchone()
            identity_id = int((identity or {})["id"])
            db.execute(
                """
                UPDATE wecom_external_contact_identity_map
                SET unionid = COALESCE(NULLIF(?, ''), unionid),
                    openid = COALESCE(NULLIF(?, ''), openid),
                    follow_user_userid = COALESCE(NULLIF(?, ''), follow_user_userid),
                    name = COALESCE(NULLIF(?, ''), name),
                    avatar = COALESCE(NULLIF(?, ''), avatar),
                    gender = COALESCE(?, gender),
                    status = 'active', raw_profile = ?::jsonb,
                    customer_id = ?, identity_id = ?, last_seen_at = NOW(), updated_at = NOW()
                WHERE corp_id = ? AND external_userid = ?
                """,
                (
                    unionid,
                    _text(contact.get("openid")),
                    _text(owner_userid),
                    _text(contact.get("name")),
                    _text(contact.get("avatar")),
                    contact.get("gender"),
                    _json(detail_payload),
                    customer_id,
                    identity_id,
                    _text(corp_id),
                    _text(external_userid),
                ),
            )
            follow_users = [dict(item or {}) for item in list((detail_payload or {}).get("follow_user") or []) if _text((item or {}).get("userid"))]
            active_userids = {_text(item.get("userid")) for item in follow_users}
            if active_userids:
                db.execute(
                    """
                    UPDATE wecom_external_contact_follow_users
                    SET relation_status = 'inactive', is_primary = FALSE, last_seen_at = NOW(), updated_at = NOW()
                    WHERE corp_id = ? AND external_userid = ?
                      AND NOT (user_id = ANY(?))
                    """,
                    (_text(corp_id), _text(external_userid), sorted(active_userids)),
                )
            for follow_user in follow_users:
                userid = _text(follow_user.get("userid"))
                db.execute(
                    """
                    INSERT INTO wecom_external_contact_follow_users(
                        corp_id, external_userid, user_id, relation_status, is_primary,
                        remark, description, customer_id, identity_id, raw_follow_user,
                        first_seen_at, last_seen_at, created_at, updated_at
                    ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?::jsonb, NOW(), NOW(), NOW(), NOW())
                    ON CONFLICT (corp_id, external_userid, user_id) DO UPDATE SET
                        relation_status = 'active', is_primary = EXCLUDED.is_primary,
                        remark = EXCLUDED.remark, description = EXCLUDED.description,
                        customer_id = EXCLUDED.customer_id, identity_id = EXCLUDED.identity_id,
                        raw_follow_user = EXCLUDED.raw_follow_user,
                        last_seen_at = NOW(), updated_at = NOW()
                    """,
                    (
                        _text(corp_id),
                        _text(external_userid),
                        userid,
                        userid == _text(owner_userid),
                        _text(follow_user.get("remark")),
                        _text(follow_user.get("description")),
                        customer_id,
                        identity_id,
                        _json(follow_user),
                    ),
                )
            if not unionid:
                enrichment_status = "not_applicable" if int(contact.get("type") or 0) == 2 else "pending"
                db.execute(
                    """
                    UPDATE crm_user_identity_resolution_queue
                    SET customer_id = ?, identity_id = ?, enrichment_status = ?,
                        status = CASE WHEN ? = 'not_applicable' THEN 'ignored' ELSE status END,
                        completed_at = CASE WHEN ? = 'not_applicable' THEN COALESCE(completed_at, NOW()) ELSE completed_at END,
                        updated_at = NOW()
                    WHERE corp_id = ? AND external_userid = ?
                    """,
                    (
                        customer_id,
                        identity_id,
                        enrichment_status,
                        enrichment_status,
                        enrichment_status,
                        _text(corp_id),
                        _text(external_userid),
                    ),
                )
            db.commit()
        return {**resolution, "identity_id": identity_id, "unionid_status": "resolved" if unionid else enrichment_status}

    def customer_context_state(self, *, corp_id: str, external_userid: str, owner_userid: str) -> dict[str, Any]:
        corp = _text(corp_id)
        external = _text(external_userid)
        owner = _text(owner_userid)
        with db_session() as db:
            row = db.execute(
                """
                SELECT identity.id AS identity_id,
                       COALESCE(root.id, customer.id) AS customer_id,
                       COALESCE(root.status, customer.status) AS customer_status,
                       CASE
                           WHEN COALESCE(root.identity_completeness, customer.identity_completeness) = 'conflict'
                               THEN 'conflict'
                           WHEN EXISTS (
                               SELECT 1 FROM customer_identities union_identity
                               WHERE union_identity.customer_id = COALESCE(root.id, customer.id)
                                 AND union_identity.provider = 'wechat'
                                 AND union_identity.identity_type = 'unionid'
                                 AND union_identity.status = 'active'
                           ) THEN 'resolved'
                           ELSE COALESCE((
                               SELECT queue.enrichment_status
                               FROM crm_user_identity_resolution_queue queue
                               WHERE queue.corp_id = ? AND queue.external_userid = ?
                               ORDER BY queue.updated_at DESC, queue.id DESC
                               LIMIT 1
                           ), 'pending')
                       END AS unionid_status,
                       EXISTS (
                           SELECT 1 FROM wecom_external_contact_follow_users relation
                           WHERE relation.corp_id = ?
                             AND relation.external_userid = ?
                             AND relation.user_id = ?
                             AND relation.relation_status = 'active'
                       ) AS relation_active
                FROM customer_identities identity
                JOIN customers customer ON customer.id = identity.customer_id
                LEFT JOIN customers root ON root.id = customer.merged_into_customer_id
                WHERE identity.provider = 'wecom'
                  AND identity.identity_type = 'external_userid'
                  AND identity.scope_key = ?
                  AND identity.normalized_value = ?
                  AND identity.status = 'active'
                """,
                (corp, external, corp, external, owner, corp, external),
            ).fetchone()
        if not row:
            return {"identity_exists": False, "relation_active": False}
        return {
            "identity_exists": True,
            "identity_id": int(row["identity_id"]),
            "customer_id": int(row["customer_id"]),
            "customer_status": _text(row.get("customer_status")),
            "unionid_status": _text(row.get("unionid_status")) or "pending",
            "relation_active": bool(row.get("relation_active")),
        }

    def close_relationship(self, *, corp_id: str, external_userid: str, owner_userid: str = "") -> int:
        corp = _text(corp_id)
        external = _text(external_userid)
        owner = _text(owner_userid)
        with db_session() as db:
            if owner:
                cursor = db.execute(
                    """
                    UPDATE wecom_external_contact_follow_users
                    SET relation_status = 'inactive', is_primary = FALSE, last_seen_at = NOW(), updated_at = NOW()
                    WHERE corp_id = ? AND external_userid = ? AND user_id = ?
                      AND relation_status = 'active'
                    """,
                    (corp, external, owner),
                )
            else:
                cursor = db.execute(
                    """
                    UPDATE wecom_external_contact_follow_users
                    SET relation_status = 'inactive', is_primary = FALSE, last_seen_at = NOW(), updated_at = NOW()
                    WHERE corp_id = ? AND external_userid = ? AND relation_status = 'active'
                    """,
                    (corp, external),
                )
            db.commit()
            return cursor.rowcount

    @staticmethod
    def _root_customer_id(db: Any, customer_id: int, *, for_update: bool) -> int:
        current = int(customer_id)
        seen: set[int] = set()
        while current not in seen:
            seen.add(current)
            row = db.execute(
                f"SELECT id, status, merged_into_customer_id FROM customers WHERE id = ?{' FOR UPDATE' if for_update else ''}",
                (current,),
            ).fetchone()
            if not row:
                raise RuntimeError("customer not found")
            target = int(row.get("merged_into_customer_id") or 0)
            if _text(row.get("status")) != "merged" or not target:
                return int(row["id"])
            current = target
        raise RuntimeError("customer merge cycle detected")

    @staticmethod
    def _strong_identity_conflict(db: Any, left_customer_id: int, right_customer_id: int) -> str:
        row = db.execute(
            """
            SELECT 1
            FROM customer_identities left_identity
            JOIN customer_identities right_identity
              ON right_identity.provider = left_identity.provider
             AND right_identity.identity_type = left_identity.identity_type
             AND right_identity.scope_key = left_identity.scope_key
             AND right_identity.normalized_value <> left_identity.normalized_value
             AND right_identity.status = 'active'
            WHERE left_identity.customer_id = ?
              AND right_identity.customer_id = ?
              AND left_identity.status = 'active'
              AND left_identity.assurance_level = 'provider_verified'
              AND right_identity.assurance_level = 'provider_verified'
            LIMIT 1
            """,
            (left_customer_id, right_customer_id),
        ).fetchone()
        return "same_scope_strong_identity_conflict" if row else ""

    @staticmethod
    def _record_conflict(
        db: Any,
        *,
        left_customer_id: int,
        right_customer_id: int,
        scope_key: str,
        unionid: str,
        reason: str,
        source_type: str = "wecom_official_detail",
    ) -> None:
        ordered = sorted((left_customer_id, right_customer_id))
        dedupe_key = hashlib.sha256(f"{ordered[0]}|{ordered[1]}|{scope_key}|{unionid}|{reason}".encode("utf-8")).hexdigest()
        db.execute(
            """
            INSERT INTO customer_identity_conflicts(
                left_customer_id, right_customer_id, provider, identity_type,
                scope_key, reason, evidence, dedupe_key, status, created_at, updated_at
            ) VALUES (?, ?, 'wechat', 'unionid', ?, ?, ?::jsonb, ?, 'open', NOW(), NOW())
            ON CONFLICT (dedupe_key) DO UPDATE SET updated_at = NOW()
            """,
            (
                ordered[0],
                ordered[1],
                scope_key,
                reason,
                _json({"unionid_present": True, "source": _text(source_type)}),
                dedupe_key,
            ),
        )

    @staticmethod
    def _merge_into_unionid_customer(
        db: Any,
        *,
        from_customer_id: int,
        to_customer_id: int,
        unionid: str,
        source_event_id: str,
        source_type: str = "wecom_official_detail",
    ) -> None:
        if from_customer_id == to_customer_id:
            return
        for customer_id in sorted((from_customer_id, to_customer_id)):
            db.execute("SELECT id FROM customers WHERE id = ? FOR UPDATE", (customer_id,)).fetchone()
        identities = db.execute(
            "SELECT id, provider, identity_type, scope_key, normalized_value FROM customer_identities WHERE customer_id = ? AND status = 'active' FOR UPDATE",
            (from_customer_id,),
        ).fetchall()
        for identity in identities:
            duplicate = db.execute(
                """
                SELECT id FROM customer_identities
                WHERE customer_id = ? AND provider = ? AND identity_type = ?
                  AND scope_key = ? AND normalized_value = ? AND status = 'active'
                """,
                (
                    to_customer_id,
                    identity["provider"],
                    identity["identity_type"],
                    identity["scope_key"],
                    identity["normalized_value"],
                ),
            ).fetchone()
            if duplicate:
                db.execute("UPDATE customer_identities SET status = 'retired', updated_at = NOW() WHERE id = ?", (identity["id"],))
            else:
                db.execute("UPDATE customer_identities SET customer_id = ?, updated_at = NOW() WHERE id = ?", (to_customer_id, identity["id"]))
        for table in (
            "wecom_external_contact_identity_map",
            "wecom_external_contact_follow_users",
            "crm_user_identity",
            "crm_user_identity_resolution_queue",
            "questionnaire_submissions",
        ):
            db.execute(f"UPDATE {table} SET customer_id = ? WHERE customer_id = ?", (to_customer_id, from_customer_id))
        db.execute(
            """
            UPDATE sidebar_customer_profile_fields target
            SET source = COALESCE(NULLIF(target.source, ''), source.source),
                industry = COALESCE(NULLIF(target.industry, ''), source.industry),
                industry_description = COALESCE(NULLIF(target.industry_description, ''), source.industry_description),
                needs_blockers_followup = COALESCE(NULLIF(target.needs_blockers_followup, ''), source.needs_blockers_followup),
                updated_at = NOW()
            FROM sidebar_customer_profile_fields source
            WHERE target.customer_id = ? AND source.customer_id = ?
            """,
            (to_customer_id, from_customer_id),
        )
        db.execute(
            "DELETE FROM sidebar_customer_profile_fields WHERE customer_id = ? AND EXISTS (SELECT 1 FROM sidebar_customer_profile_fields WHERE customer_id = ?)",
            (from_customer_id, to_customer_id),
        )
        db.execute(
            "UPDATE sidebar_customer_profile_fields SET customer_id = ? WHERE customer_id = ?",
            (to_customer_id, from_customer_id),
        )
        db.execute(
            """
            DELETE FROM contact_tags source
            USING contact_tags target
            WHERE source.customer_id = ? AND target.customer_id = ?
              AND source.userid = target.userid AND source.tag_id = target.tag_id
            """,
            (from_customer_id, to_customer_id),
        )
        db.execute("UPDATE contact_tags SET customer_id = ? WHERE customer_id = ?", (to_customer_id, from_customer_id))
        db.execute(
            """
            UPDATE customers
            SET status = 'merged', merged_into_customer_id = ?, merged_at = NOW(), updated_at = NOW()
            WHERE id = ? AND status = 'active'
            """,
            (to_customer_id, from_customer_id),
        )
        db.execute(
            """
            INSERT INTO customer_merges(
                from_customer_id, to_customer_id, evidence, rule, operator,
                source_type, source_event_id, reversible_status, merged_at
            ) VALUES (?, ?, ?::jsonb, 'official_unionid_conditional_merge', 'system',
                      ?, ?, 'not_reversed', NOW())
            ON CONFLICT (from_customer_id) DO NOTHING
            """,
            (
                from_customer_id,
                to_customer_id,
                _json(
                    {
                        "unionid_present": bool(unionid),
                        "unionid_customer_survives": True,
                        "source": _text(source_type),
                    }
                ),
                _text(source_type),
                _text(source_event_id),
            ),
        )

    @staticmethod
    def _mark_ecosystem_enriched(db: Any, customer_id: int, unionid: str) -> None:
        db.execute(
            "UPDATE customers SET identity_completeness = 'enriched', updated_at = NOW() WHERE id = ? AND status = 'active'",
            (customer_id,),
        )
        db.execute(
            "UPDATE crm_user_identity SET customer_id = ?, last_seen_at = NOW(), updated_at = NOW() WHERE unionid = ?",
            (customer_id, _text(unionid)),
        )

    @staticmethod
    def _mark_enriched(db: Any, customer_id: int, external_userid: str, unionid: str) -> None:
        db.execute(
            "UPDATE customers SET identity_completeness = 'enriched', updated_at = NOW() WHERE id = ? AND status = 'active'",
            (customer_id,),
        )
        db.execute(
            "UPDATE crm_user_identity SET customer_id = ?, last_seen_at = NOW(), updated_at = NOW() WHERE unionid = ?",
            (customer_id, unionid),
        )
        db.execute(
            """
            UPDATE crm_user_identity_resolution_queue
            SET customer_id = ?, status = 'resolved', enrichment_status = 'resolved',
                resolved_unionid = ?, resolved_at = NOW(), completed_at = COALESCE(completed_at, NOW()),
                updated_at = NOW()
            WHERE external_userid = ? AND status IN ('pending', 'polling', 'held', 'resolved')
            """,
            (customer_id, unionid, _text(external_userid)),
        )
        db.execute(
            "UPDATE sidebar_customer_profile_fields SET customer_id = ? WHERE unionid = ? AND customer_id IS NULL",
            (customer_id, unionid),
        )
        db.execute(
            "UPDATE contact_tags SET customer_id = ? WHERE unionid = ? AND customer_id IS NULL",
            (customer_id, unionid),
        )
        db.execute(
            "UPDATE questionnaire_submissions SET customer_id = ? WHERE unionid = ? AND customer_id IS NULL",
            (customer_id, unionid),
        )


__all__ = ["EnsuredCustomer", "PostgresOneIDService", "unionid_scope_key"]
