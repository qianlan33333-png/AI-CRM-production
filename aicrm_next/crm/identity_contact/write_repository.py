from __future__ import annotations

import json
from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


class PostgresIdentityWriteRepository:
    """Canonical identity mutations used by bounded cross-capability ingress."""

    def bind_sidebar_mobile(
        self,
        conn: Any,
        *,
        unionid: str,
        external_userid: str,
        mobile: str,
        owner_userid: str,
        bind_by_userid: str,
    ) -> dict[str, Any]:
        updated = conn.execute(
            """
            UPDATE crm_user_identity
            SET mobile = %s,
                mobile_normalized = %s,
                mobile_verified = TRUE,
                mobile_source = 'sidebar_bind',
                primary_owner_userid = COALESCE(NULLIF(%s, ''), primary_owner_userid),
                profile_json = profile_json || jsonb_build_object(
                    'sidebar_bind_by_userid', %s::text,
                    'sidebar_external_userid', %s::text
                ),
                last_seen_at = NOW(),
                updated_at = NOW()
            WHERE unionid = %s
              AND identity_status = 'active'
            RETURNING
                unionid,
                primary_external_userid,
                external_userids_json,
                mobile,
                mobile_normalized,
                mobile_source,
                primary_owner_userid,
                customer_name,
                remark,
                created_at,
                updated_at
            """,
            (
                mobile,
                mobile,
                owner_userid,
                bind_by_userid,
                external_userid,
                unionid,
            ),
        ).fetchone()
        if not updated:
            raise RuntimeError("crm_user_identity mobile bind failed")
        return dict(updated)

    def update_sidebar_contact_profile(
        self,
        conn: Any,
        *,
        unionid: str,
        display_name: str,
        remark: str,
        description: str,
        updated_by: str,
    ) -> dict[str, Any] | None:
        if not any((_text(display_name), _text(remark), _text(description))):
            return None
        row = conn.execute(
            """
            UPDATE crm_user_identity
            SET customer_name = CASE WHEN %s <> '' THEN %s ELSE customer_name END,
                remark = CASE WHEN %s <> '' THEN %s ELSE remark END,
                profile_json = COALESCE(profile_json, '{}'::jsonb) || jsonb_strip_nulls(jsonb_build_object(
                    'description', NULLIF(%s::text, ''),
                    'sidebar_profile_updated_by', NULLIF(%s::text, '')
                )),
                updated_at = NOW()
            WHERE unionid = %s
            RETURNING customer_name, remark, profile_json, updated_at
            """,
            (
                _text(display_name),
                _text(display_name),
                _text(remark),
                _text(remark),
                _text(description),
                _text(updated_by),
                _text(unionid),
            ),
        ).fetchone()
        return dict(row) if row else None

    def record_sidebar_material_send_plan(
        self,
        conn: Any,
        *,
        unionid: str,
        plan: dict[str, Any],
    ) -> dict[str, Any] | None:
        row = conn.execute(
            """
            UPDATE crm_user_identity
            SET profile_json = COALESCE(profile_json, '{}'::jsonb) || jsonb_build_object(
                    'last_material_send_plan', CAST(%s AS jsonb)
                ),
                updated_at = NOW()
            WHERE unionid = %s
            RETURNING updated_at
            """,
            (json.dumps(plan, ensure_ascii=True, sort_keys=True), _text(unionid)),
        ).fetchone()
        return dict(row) if row else None

    def enqueue_sidebar_identity_resolution(
        self,
        conn: Any,
        *,
        command_id: str,
        external_userid: str,
        mobile: str,
        owner_userid: str,
        bind_by_userid: str,
    ) -> dict[str, Any]:
        source_key = f"sidebar_bind_mobile:{external_userid}:{command_id}"
        identity_context = conn.execute(
            """
            SELECT customer_id, identity_id
            FROM wecom_external_contact_identity_map
            WHERE external_userid = %s
              AND status = 'active'
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (external_userid,),
        ).fetchone()
        customer_id = (identity_context or {}).get("customer_id")
        identity_id = (identity_context or {}).get("identity_id")
        cursor = conn.execute(
            """
            INSERT INTO crm_user_identity_resolution_queue (
                source_type,
                source_key,
                external_userid,
                mobile,
                customer_id,
                identity_id,
                enrichment_status,
                payload_json,
                reason,
                status,
                next_attempt_at,
                first_seen_at,
                last_seen_at,
                created_at,
                updated_at
            )
            VALUES ('sidebar_bind_mobile', %s, %s, %s, %s, %s, 'pending', CAST(%s AS jsonb), 'missing_unionid', 'pending', NOW(), NOW(), NOW(), NOW(), NOW())
            ON CONFLICT (source_type, source_key) WHERE status = 'pending' AND source_type <> '' AND source_key <> ''
            DO UPDATE SET
                external_userid = COALESCE(NULLIF(EXCLUDED.external_userid, ''), crm_user_identity_resolution_queue.external_userid),
                mobile = COALESCE(NULLIF(EXCLUDED.mobile, ''), crm_user_identity_resolution_queue.mobile),
                customer_id = COALESCE(EXCLUDED.customer_id, crm_user_identity_resolution_queue.customer_id),
                identity_id = COALESCE(EXCLUDED.identity_id, crm_user_identity_resolution_queue.identity_id),
                enrichment_status = 'pending',
                payload_json = crm_user_identity_resolution_queue.payload_json || EXCLUDED.payload_json,
                reason = EXCLUDED.reason,
                last_seen_at = NOW(),
                updated_at = NOW()
            RETURNING *
            """,
            (
                source_key,
                external_userid,
                mobile,
                customer_id,
                identity_id,
                json.dumps(
                    {
                        "external_userid": external_userid,
                        "mobile": mobile,
                        "owner_userid": owner_userid,
                        "bind_by_userid": bind_by_userid,
                        "source": "sidebar_bind_mobile",
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            ),
        )
        row = cursor.fetchone()
        planned: dict[str, Any] = {}
        if row:
            from .resolution_effects import plan_identity_resolution_effect

            planned = plan_identity_resolution_effect(
                conn,
                dict(row),
                parent_execution_id=command_id,
                source_route="sidebar_write.identity_resolution.enqueue",
            )
        return {
            "status": "pending" if planned.get("external_effect_job_id") else "held",
            "reason": "identity_pending_unionid",
            "source_key": source_key,
            **planned,
        }


__all__ = ["PostgresIdentityWriteRepository"]
