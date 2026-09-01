from __future__ import annotations

import json
import re
import hashlib
from typing import Any, Protocol

from aicrm_next.platform.shared.postgres_connection import get_db

from .crm_port import (
    IdentityResolveResult,
    ResolvePersonIdentityRequest,
    plan_identity_resolution_effect,
    resolve_identity_with_dbapi,
    resolved_unionid,
)


class IdentityBridgeRepositoryError(RuntimeError):
    pass


class IdentityBridgeRepository(Protocol):
    def identity_bridge_state(self, external_userid: str) -> dict[str, Any]: ...

    def normalize_external_contact_identity(
        self,
        corp_id: str,
        detail: dict[str, Any],
        follow_user_userid: str,
        status: str = "active",
    ) -> dict[str, Any]: ...

    def upsert_external_contact_identity(self, record: dict[str, Any]) -> int: ...

    def replace_external_contact_follow_users(
        self,
        corp_id: str,
        external_userid: str,
        follow_users: list[dict[str, Any]],
        preferred_userid: str = "",
    ) -> None: ...

    def refresh_external_contact_identity_owner(self, corp_id: str, external_userid: str) -> None: ...

    def get_contact_binding_status(self, external_userid: str, owner_userid: str = "") -> dict[str, Any]: ...

    def get_unique_mobile_candidate_from_identity_sources(self, external_userid: str) -> dict[str, Any] | None: ...

    def list_unbound_external_userids_with_identity_sources(self, limit: int = 500) -> list[str]: ...

    def get_or_create_person_for_mobile(self, mobile: str) -> tuple[int, str]: ...

    def upsert_external_contact_binding_record(
        self,
        *,
        external_userid: str,
        person_id: int,
        mobile: str = "",
        bind_by_userid: str,
        owner_userid: str,
        force_rebind: bool = False,
    ) -> dict[str, Any]: ...

    def resolve_binding_owner_userid(self, external_userid: str, owner_userid: str = "") -> str: ...

    def merge_lead_pool_after_mobile_bind(self, *, external_userid: str, mobile: str, owner_userid: str) -> dict[str, Any]: ...

    def backfill_questionnaire_submissions_for_mobile_binding(
        self,
        *,
        external_userid: str,
        mobile: str,
        follow_user_userid: str = "",
    ) -> dict[str, Any]: ...


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json_dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str)


def _normalize_mobile(value: Any) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    if len(digits) == 13 and digits.startswith("86"):
        digits = digits[2:]
    return digits if re.fullmatch(r"1[3-9][0-9]{9}", digits) else ""


def _mask_mobile(value: Any) -> str:
    digits = _normalize_mobile(value)
    if not digits:
        return ""
    return f"{digits[:3]}****{digits[-4:]}"


def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row or {})


def _now_sql() -> str:
    return "NOW()"


class PostgresIdentityBridgeRepository:
    def _resolve_identity(
        self,
        *,
        external_userid: str = "",
        unionid: str = "",
        openid: str = "",
        mobile: str = "",
        for_update: bool = False,
    ) -> IdentityResolveResult:
        return resolve_identity_with_dbapi(
            get_db(),
            ResolvePersonIdentityRequest(
                external_userid=_text(external_userid) or None,
                unionid=_text(unionid) or None,
                openid=_text(openid) or None,
                mobile=_text(mobile) or None,
            ),
            placeholder="?",
            for_update=for_update,
        )

    def identity_bridge_state(self, external_userid: str) -> dict[str, Any]:
        resolution = self._resolve_identity(external_userid=external_userid)
        identity = resolution.identity if resolution.status == "resolved" else None
        if identity is None:
            pending = get_db().execute(
                """
                SELECT external_userid, openid, reason, updated_at
                FROM crm_user_identity_resolution_queue
                WHERE external_userid = ? AND status = 'pending'
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (_text(external_userid),),
            ).fetchone()
            if pending:
                payload = _row_dict(pending)
                return {
                    **payload,
                    "exists": False,
                    "unionid_present": False,
                    "openid_present": bool(_text(payload.get("openid"))),
                    "mobile_bound": False,
                    "reason": _text(payload.get("reason")) or "identity_pending_resolution",
                }
            return {
                "exists": False,
                "reason": "identity_conflict" if resolution.status == "conflict" else "identity_missing",
            }
        freshness = get_db().execute(
            "SELECT updated_at FROM crm_user_identity WHERE unionid = ?",
            (_text(identity.unionid),),
        ).fetchone()
        return {
            "exists": True,
            "external_userid": _text(identity.external_userid),
            "unionid": _text(identity.unionid),
            "openid": _text(identity.openid),
            "updated_at": (freshness or {}).get("updated_at"),
            "unionid_present": bool(_text(identity.unionid)),
            "openid_present": bool(_text(identity.openid)),
            "mobile_bound": bool(_text(identity.mobile)),
        }

    def normalize_external_contact_identity(
        self,
        corp_id: str,
        detail: dict[str, Any],
        follow_user_userid: str,
        status: str = "active",
    ) -> dict[str, Any]:
        payload = dict(detail or {})
        contact = dict(payload.get("external_contact") or payload)
        follow_users = list(payload.get("follow_user") or [])
        selected_follow_user: dict[str, Any] = {}
        preferred = _text(follow_user_userid)
        for item in follow_users:
            candidate = dict(item or {})
            if preferred and _text(candidate.get("userid")) == preferred:
                selected_follow_user = candidate
                break
        if not selected_follow_user and follow_users:
            selected_follow_user = dict(follow_users[0] or {})
        selected_userid = preferred or _text(selected_follow_user.get("userid"))
        return {
            "corp_id": _text(corp_id),
            "external_userid": _text(contact.get("external_userid")),
            "unionid": _text(contact.get("unionid")),
            "openid": _text(contact.get("openid")),
            "follow_user_userid": selected_userid,
            "name": _text(contact.get("name")),
            "type": contact.get("type"),
            "avatar": _text(contact.get("avatar")),
            "gender": contact.get("gender"),
            "status": _text(status) or "active",
            "raw_profile": _json_dumps(payload),
        }

    def upsert_external_contact_identity(self, record: dict[str, Any]) -> int:
        external_userid = _text(record.get("external_userid"))
        if not external_userid:
            raise IdentityBridgeRepositoryError("external_userid is required")
        unionid = _text(record.get("unionid"))
        if not unionid:
            self.enqueue_identity_resolution(record, reason="missing_unionid")
            return 0
        self._assert_aliases_assignable(record)
        row = get_db().execute(
            """
            INSERT INTO crm_user_identity (
                unionid,
                openids_json,
                external_userids_json,
                customer_name,
                avatar,
                gender,
                profile_json,
                follow_users_json,
                primary_external_userid,
                primary_openid,
                primary_owner_userid,
                identity_status,
                unionid_resolved_at,
                last_polled_at,
                legacy_sources_json,
                first_seen_at,
                last_seen_at,
                created_at,
                updated_at
            ) VALUES (
                ?,
                CASE WHEN CAST(? AS text) = '' THEN '[]'::jsonb ELSE jsonb_build_array(CAST(? AS text)) END,
                jsonb_build_array(CAST(? AS text)),
                ?,
                ?,
                ?,
                jsonb_strip_nulls(jsonb_build_object(
                    'name', NULLIF(?, ''),
                    'avatar', NULLIF(?, ''),
                    'gender', CAST(? AS text),
                    'type', CAST(? AS text),
                    'raw_profile', ?::jsonb
                )),
                CASE WHEN CAST(? AS text) = '' THEN '[]'::jsonb ELSE jsonb_build_array(jsonb_build_object('userid', CAST(? AS text))) END,
                ?,
                ?,
                ?,
                ?,
                NOW(),
                NOW(),
                jsonb_build_object('wecom_external_contact_detail', TRUE),
                NOW(),
                NOW(),
                NOW(),
                NOW()
            )
            ON CONFLICT (unionid) DO UPDATE SET
                openids_json = (
                    SELECT COALESCE(jsonb_agg(DISTINCT value), '[]'::jsonb)
                    FROM jsonb_array_elements_text(crm_user_identity.openids_json || EXCLUDED.openids_json) AS merged(value)
                ),
                external_userids_json = (
                    SELECT COALESCE(jsonb_agg(DISTINCT value), '[]'::jsonb)
                    FROM jsonb_array_elements_text(crm_user_identity.external_userids_json || EXCLUDED.external_userids_json) AS merged(value)
                ),
                customer_name = COALESCE(NULLIF(EXCLUDED.customer_name, ''), crm_user_identity.customer_name),
                avatar = COALESCE(NULLIF(EXCLUDED.avatar, ''), crm_user_identity.avatar),
                gender = COALESCE(EXCLUDED.gender, crm_user_identity.gender),
                profile_json = crm_user_identity.profile_json || EXCLUDED.profile_json,
                follow_users_json = CASE
                    WHEN jsonb_array_length(EXCLUDED.follow_users_json) > 0 THEN EXCLUDED.follow_users_json
                    ELSE crm_user_identity.follow_users_json
                END,
                primary_external_userid = COALESCE(NULLIF(EXCLUDED.primary_external_userid, ''), crm_user_identity.primary_external_userid),
                primary_openid = COALESCE(NULLIF(EXCLUDED.primary_openid, ''), crm_user_identity.primary_openid),
                primary_owner_userid = COALESCE(NULLIF(EXCLUDED.primary_owner_userid, ''), crm_user_identity.primary_owner_userid),
                identity_status = COALESCE(NULLIF(EXCLUDED.identity_status, ''), crm_user_identity.identity_status),
                unionid_resolved_at = COALESCE(crm_user_identity.unionid_resolved_at, EXCLUDED.unionid_resolved_at),
                last_polled_at = NOW(),
                legacy_sources_json = crm_user_identity.legacy_sources_json || EXCLUDED.legacy_sources_json,
                last_seen_at = NOW(),
                updated_at = NOW()
            RETURNING 1 AS id
            """,
            (
                unionid,
                _text(record.get("openid")),
                _text(record.get("openid")),
                external_userid,
                _text(record.get("name")),
                _text(record.get("avatar")),
                record.get("gender"),
                _text(record.get("name")),
                _text(record.get("avatar")),
                record.get("gender"),
                record.get("type"),
                _text(record.get("raw_profile")) or "{}",
                _text(record.get("follow_user_userid")),
                _text(record.get("follow_user_userid")),
                external_userid,
                _text(record.get("openid")),
                _text(record.get("follow_user_userid")),
                _text(record.get("status")) or "active",
            ),
        ).fetchone()
        self.mark_identity_resolution_resolved(external_userid=external_userid, unionid=unionid)
        get_db().commit()
        return int((row or {}).get("id") or 0)

    def _assert_aliases_assignable(self, record: dict[str, Any]) -> None:
        db = get_db()
        unionid = _text(record.get("unionid"))
        external_userid = _text(record.get("external_userid"))
        openid = _text(record.get("openid"))
        lock_keys = sorted(
            key
            for key in {
                f"unionid:{unionid}" if unionid else "",
                f"external_userid:{external_userid}" if external_userid else "",
                f"openid:{openid}" if openid else "",
            }
            if key
        )
        for lock_key in lock_keys:
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", (lock_key,))

        checks = [
            self._resolve_identity(unionid=unionid, for_update=True),
            self._resolve_identity(external_userid=external_userid, for_update=True),
        ]
        if openid:
            checks.append(self._resolve_identity(openid=openid, for_update=True))
        for resolution in checks:
            candidate_unionid = resolved_unionid(resolution)
            if resolution.status == "conflict" or (candidate_unionid and candidate_unionid != unionid):
                self._record_identity_conflict(
                    record,
                    reason=resolution.reason or "identity_alias_conflict",
                    candidate_unionid=candidate_unionid,
                )
                raise IdentityBridgeRepositoryError("identity alias conflict")

    def _record_identity_conflict(
        self,
        record: dict[str, Any],
        *,
        reason: str,
        candidate_unionid: str = "",
    ) -> None:
        unionid = _text(record.get("unionid"))
        external_userid = _text(record.get("external_userid"))
        openid = _text(record.get("openid"))
        mobile = _text(record.get("mobile"))
        source_key = hashlib.sha256(
            f"{reason}|{unionid}|{candidate_unionid}|{external_userid}|{openid}|{mobile}".encode("utf-8")
        ).hexdigest()
        get_db().execute(
            """
            INSERT INTO crm_user_identity_conflicts (
                conflict_type, unionid, candidate_unionid, external_userid, openid, mobile,
                source_type, source_key, payload_json, source_payload_json,
                status, resolution_status, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?,
                'wecom_external_contact', ?, ?::jsonb, ?::jsonb,
                'open', 'open', NOW(), NOW()
            )
            """,
            (
                _text(reason) or "identity_alias_conflict",
                unionid,
                _text(candidate_unionid),
                external_userid,
                openid,
                mobile,
                source_key,
                _json_dumps({"candidate_count": 1 if candidate_unionid else 0}),
                _json_dumps({"source": "wecom_external_contact_detail"}),
            ),
        )

    def enqueue_identity_resolution(self, record: dict[str, Any], *, reason: str) -> None:
        external_userid = _text(record.get("external_userid"))
        source_key = external_userid or _text(record.get("openid")) or _text(record.get("mobile"))
        db = get_db()
        cursor = db.execute(
            """
            INSERT INTO crm_user_identity_resolution_queue (
                source_type,
                source_key,
                corp_id,
                external_userid,
                openid,
                mobile,
                payload_json,
                reason,
                status,
                first_seen_at,
                last_seen_at,
                created_at,
                updated_at
            ) VALUES (
                'wecom_external_contact',
                ?,
                ?,
                ?,
                ?,
                ?,
                ?::jsonb,
                ?,
                'pending',
                NOW(),
                NOW(),
                NOW(),
                NOW()
            )
            ON CONFLICT (source_type, source_key) WHERE status = 'pending' AND source_type <> '' AND source_key <> ''
            DO UPDATE SET
                corp_id = COALESCE(NULLIF(EXCLUDED.corp_id, ''), crm_user_identity_resolution_queue.corp_id),
                external_userid = COALESCE(NULLIF(EXCLUDED.external_userid, ''), crm_user_identity_resolution_queue.external_userid),
                openid = COALESCE(NULLIF(EXCLUDED.openid, ''), crm_user_identity_resolution_queue.openid),
                mobile = COALESCE(NULLIF(EXCLUDED.mobile, ''), crm_user_identity_resolution_queue.mobile),
                payload_json = crm_user_identity_resolution_queue.payload_json || EXCLUDED.payload_json,
                reason = EXCLUDED.reason,
                last_seen_at = NOW(),
                updated_at = NOW()
            RETURNING *
            """,
            (
                source_key,
                _text(record.get("corp_id")),
                external_userid,
                _text(record.get("openid")),
                _text(record.get("mobile")),
                _json_dumps(record),
                _text(reason) or "identity_unresolved",
            ),
        )
        row = cursor.fetchone()
        if row:
            linked = db.execute(
                """
                UPDATE crm_user_identity_resolution_queue queue
                SET customer_id = identity_map.customer_id,
                    identity_id = identity_map.identity_id,
                    enrichment_status = 'pending',
                    updated_at = NOW()
                FROM wecom_external_contact_identity_map identity_map
                WHERE queue.id = ?
                  AND identity_map.corp_id = queue.corp_id
                  AND identity_map.external_userid = queue.external_userid
                RETURNING queue.*
                """,
                (int(row.get("id") or 0),),
            ).fetchone()
            row = linked or row
            plan_identity_resolution_effect(
                db,
                dict(row),
                source_route="channel_entry.identity_bridge_resolution.enqueue",
            )
        db.commit()

    def mark_identity_resolution_resolved(self, *, external_userid: str, unionid: str) -> None:
        external = _text(external_userid)
        resolved_unionid = _text(unionid)
        if not external or not resolved_unionid:
            return
        get_db().execute(
            """
            UPDATE crm_user_identity_resolution_queue
            SET status = 'resolved',
                payload_json = payload_json || jsonb_build_object('resolved_unionid', CAST(? AS text)),
                resolved_unionid = ?,
                resolved_at = NOW(),
                last_seen_at = NOW(),
                updated_at = NOW()
            WHERE external_userid = ? AND status = 'pending'
            """,
            (resolved_unionid, resolved_unionid, external),
        )

    def replace_external_contact_follow_users(
        self,
        corp_id: str,
        external_userid: str,
        follow_users: list[dict[str, Any]],
        preferred_userid: str = "",
    ) -> None:
        db = get_db()
        external = _text(external_userid)
        normalized = [dict(item or {}) for item in list(follow_users or []) if _text((item or {}).get("userid"))]
        userids = {_text(item.get("userid")) for item in normalized}
        preferred = _text(preferred_userid)
        resolution = self._resolve_identity(external_userid=external, for_update=True)
        unionid = resolved_unionid(resolution)
        if not unionid or resolution.identity is None:
            raise IdentityBridgeRepositoryError("canonical identity is required")
        old_primary = _text(resolution.identity.owner_userid)
        if preferred and preferred in userids:
            primary = preferred
        elif old_primary and old_primary in userids:
            primary = old_primary
        elif normalized:
            primary = _text(normalized[0].get("userid"))
        else:
            primary = ""

        db.execute(
            """
            UPDATE crm_user_identity
            SET follow_users_json = ?::jsonb,
                primary_owner_userid = COALESCE(NULLIF(?, ''), primary_owner_userid),
                updated_at = NOW()
            WHERE unionid = ?
              AND identity_status = 'active'
            """,
            (_json_dumps(normalized), primary, unionid),
        )
        db.commit()

    def refresh_external_contact_identity_owner(self, corp_id: str, external_userid: str) -> None:
        db = get_db()
        resolution = self._resolve_identity(external_userid=external_userid, for_update=True)
        unionid = resolved_unionid(resolution)
        if not unionid:
            return
        row = db.execute(
            """
            SELECT elem->>'userid' AS user_id
            FROM crm_user_identity,
                 jsonb_array_elements(follow_users_json) AS elem
            WHERE unionid = ?
              AND COALESCE(elem->>'userid', '') <> ''
            ORDER BY elem->>'userid'
            LIMIT 1
            """,
            (unionid,),
        ).fetchone()
        owner = _text((row or {}).get("user_id"))
        if owner:
            db.execute(
                """
                UPDATE crm_user_identity
                SET primary_owner_userid = ?,
                    identity_status = 'active',
                    last_seen_at = NOW(),
                    updated_at = NOW()
                WHERE unionid = ?
                  AND identity_status = 'active'
                """,
                (owner, unionid),
            )
            db.commit()

    def get_contact_binding_status(self, external_userid: str, owner_userid: str = "") -> dict[str, Any]:
        external = _text(external_userid)
        resolution = self._resolve_identity(external_userid=external)
        identity = resolution.identity if resolution.status == "resolved" else None
        if identity and _text(identity.mobile):
            return {
                "is_bound": True,
                "person_id": "",
                "unionid": _text(identity.unionid),
                "external_userid": external,
                "owner_userid": _text(owner_userid) or _text(identity.owner_userid),
                "customer_name": _text(identity.customer_name),
                "remark": _text(identity.remark),
                "display_name": _text(identity.customer_name) or external,
                "mobile": _text(identity.mobile),
                "third_party_user_id": "",
                "first_bound_by_userid": "",
                "first_owner_userid": "",
                "last_owner_userid": _text(owner_userid) or _text(identity.owner_userid),
                "created_at": None,
                "updated_at": None,
            }
        return {
            "is_bound": False,
            "person_id": "",
            "unionid": _text(identity.unionid) if identity else "",
            "external_userid": external,
            "owner_userid": _text(owner_userid) or (_text(identity.owner_userid) if identity else ""),
            "customer_name": _text(identity.customer_name) if identity else "",
            "remark": _text(identity.remark) if identity else "",
            "display_name": (_text(identity.customer_name) or _text(identity.remark) or external) if identity else external,
            "mobile": "",
            "third_party_user_id": "",
            "first_bound_by_userid": "",
            "first_owner_userid": "",
            "last_owner_userid": "",
            "created_at": None,
            "updated_at": None,
        }

    def _identity_sources(self, external_userid: str) -> tuple[list[str], list[str]]:
        resolution = self._resolve_identity(external_userid=external_userid)
        unionid = resolved_unionid(resolution)
        if not unionid:
            return [], []
        identity = get_db().execute(
            "SELECT openids_json FROM crm_user_identity WHERE unionid = ?",
            (unionid,),
        ).fetchone()
        raw_openids = (identity or {}).get("openids_json") or []
        if isinstance(raw_openids, str):
            try:
                raw_openids = json.loads(raw_openids)
            except json.JSONDecodeError:
                raw_openids = []
        openids = sorted({_text(value) for value in raw_openids if _text(value)})
        return openids, [unionid]

    def get_unique_mobile_candidate_from_identity_sources(self, external_userid: str) -> dict[str, Any] | None:
        resolution = self._resolve_identity(external_userid=external_userid)
        identity = resolution.identity if resolution.status == "resolved" else None
        mobile = _normalize_mobile(identity.mobile if identity else "")
        if not mobile:
            return None
        return {
            "mobile": mobile,
            "matched_count": 1,
            "latest_matched_at": None,
            "sources": ["crm_user_identity"],
        }

    def list_unbound_external_userids_with_identity_sources(self, limit: int = 500) -> list[str]:
        rows = get_db().execute(
            """
            SELECT DISTINCT primary_external_userid AS external_userid
            FROM crm_user_identity
            WHERE COALESCE(primary_external_userid, '') <> ''
              AND COALESCE(mobile_normalized, '') = ''
            ORDER BY primary_external_userid
            LIMIT ?
            """,
            (max(1, int(limit or 500)),),
        ).fetchall()
        return [_text(row.get("external_userid")) for row in rows if _text(row.get("external_userid"))]

    def get_or_create_person_for_mobile(self, mobile: str) -> tuple[int, str]:
        normalized = _normalize_mobile(mobile)
        if not normalized:
            raise IdentityBridgeRepositoryError("valid mobile is required")
        return 0, normalized

    def upsert_external_contact_binding_record(
        self,
        *,
        external_userid: str,
        person_id: int,
        mobile: str = "",
        bind_by_userid: str,
        owner_userid: str,
        force_rebind: bool = False,
    ) -> dict[str, Any]:
        db = get_db()
        external = _text(external_userid)
        normalized_mobile = _normalize_mobile(mobile)
        resolution = self._resolve_identity(external_userid=external, for_update=True)
        unionid = resolved_unionid(resolution)
        if not unionid:
            raise IdentityBridgeRepositoryError("canonical identity is required")
        existing_mobile = _normalize_mobile(resolution.identity.mobile if resolution.identity else "")
        if existing_mobile and normalized_mobile and existing_mobile != normalized_mobile and not force_rebind:
            raise IdentityBridgeRepositoryError("canonical mobile rebind blocked")
        mobile_resolution = self._resolve_identity(mobile=normalized_mobile, for_update=True) if normalized_mobile else None
        mobile_unionid = resolved_unionid(mobile_resolution) if mobile_resolution else ""
        if mobile_resolution and (
            mobile_resolution.status in {"pending", "conflict"}
            or (mobile_unionid and mobile_unionid != unionid)
        ):
            self._record_identity_conflict(
                {"unionid": unionid, "external_userid": external, "mobile": normalized_mobile},
                reason="mobile_alias_conflict",
                candidate_unionid=mobile_unionid,
            )
            raise IdentityBridgeRepositoryError("identity alias conflict")
        db.execute(
            """
            UPDATE crm_user_identity
            SET mobile = COALESCE(NULLIF(?, ''), mobile),
                mobile_normalized = COALESCE(NULLIF(?, ''), mobile_normalized),
                mobile_verified = TRUE,
                mobile_source = COALESCE(NULLIF(mobile_source, ''), 'identity_bridge_mobile_bind'),
                primary_owner_userid = COALESCE(NULLIF(?, ''), primary_owner_userid),
                updated_at = NOW()
            WHERE unionid = ?
              AND identity_status = 'active'
            """,
            (
                normalized_mobile,
                normalized_mobile,
                _text(owner_userid),
                unionid,
            ),
        )
        db.commit()
        return self.get_contact_binding_status(external, owner_userid)

    def resolve_binding_owner_userid(self, external_userid: str, owner_userid: str = "") -> str:
        owner = _text(owner_userid)
        if owner:
            return owner
        resolution = self._resolve_identity(external_userid=external_userid)
        owner = _text(resolution.identity.owner_userid) if resolution.status == "resolved" and resolution.identity else ""
        if owner:
            return owner
        return ""

    def table_columns(self, table_name: str) -> set[str]:
        try:
            rows = get_db().execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = ?
                """,
                (_text(table_name),),
            ).fetchall()
            return {_text(row.get("column_name")) for row in rows if _text(row.get("column_name"))}
        except Exception:
            get_db().rollback()
            return set()

    def merge_lead_pool_after_mobile_bind(self, *, external_userid: str, mobile: str, owner_userid: str) -> dict[str, Any]:
        return {
            "status": "skipped",
            "reason": "customer_mobile_bound_event_required",
            "updated_count": 0,
            "action_type": "customer_mobile_bound_event",
        }

    def backfill_questionnaire_submissions_for_mobile_binding(
        self,
        *,
        external_userid: str,
        mobile: str,
        follow_user_userid: str = "",
    ) -> dict[str, Any]:
        external = _text(external_userid)
        normalized = _normalize_mobile(mobile)
        if not external or not normalized:
            return {"status": "skipped", "reason": "invalid_external_userid_or_mobile", "updated_count": 0}
        return {
            "status": "skipped",
            "reason": "questionnaire_submissions_unionid_only",
            "updated_count": 0,
            "external_userid": external,
            "mobile_masked": _mask_mobile(normalized),
        }


def build_identity_bridge_repository() -> IdentityBridgeRepository:
    return PostgresIdentityBridgeRepository()
