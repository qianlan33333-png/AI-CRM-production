from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from aicrm_next.crm.customer_read_model.repo import FixtureCustomerReadRepository
from aicrm_next.crm.customer_read_model.sidebar_profile_port import (
    SaveSidebarProfileFieldsRequest,
    build_sidebar_customer_profile_projection_port,
)
from aicrm_next.crm.identity_contact.dto import ResolvePersonIdentityRequest
from aicrm_next.crm.identity_contact.resolver import resolve_identity_with_dbapi, resolved_unionid
from aicrm_next.crm.identity_contact.write_port import IdentityWritePort
from aicrm_next.platform.platform_foundation.command_bus.models import utcnow_iso
from aicrm_next.platform.shared.repository_provider import RepositoryProviderError
from aicrm_next.platform.shared.runtime import raw_database_url
from aicrm_next.platform.shared.typing import JsonDict

from .models import SidebarWriteProjection


def _psycopg_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://") :]
    return url


def normalize_mobile(value: str) -> str:
    digits = re.sub(r"\D+", "", str(value or "").strip())
    if digits.startswith("86") and len(digits) == 13:
        digits = digits[2:]
    if not re.fullmatch(r"1\d{10}", digits):
        raise ValueError("mobile must be a valid mainland China mobile number")
    return digits


def _text(value: Any) -> str:
    return str(value or "").strip()


def _usable_wecom_media_id(value: Any) -> str:
    media_id = _text(value)
    lowered = media_id.lower()
    if lowered.startswith(("fake_", "staging_", "fake://")):
        return ""
    return media_id


class SidebarWriteRepository:
    def __init__(self) -> None:
        fixture = FixtureCustomerReadRepository()
        self._customers: dict[str, JsonDict] = {
            str(item.get("external_userid") or ""): deepcopy(item)
            for item in fixture.list_customers()
            if str(item.get("external_userid") or "").strip()
        }
        self._writes: list[SidebarWriteProjection] = []

    def get_customer(self, external_userid: str) -> JsonDict | None:
        customer = self._customers.get(external_userid)
        return deepcopy(customer) if customer else None

    def list_writes(self) -> list[SidebarWriteProjection]:
        return list(self._writes)

    def bind_mobile(self, *, command_id: str, external_userid: str, mobile: str) -> JsonDict:
        return self._update_customer(
            command_id=command_id,
            external_userid=external_userid,
            write_type="binding_update",
            changes={"mobile": mobile, "binding": {"is_bound": True, "mobile": mobile, "binding_status": "bound"}},
        )

    def upsert_lead_pool_class_term(
        self,
        *,
        command_id: str,
        external_userid: str,
        class_term: str,
        status: str,
    ) -> JsonDict:
        return self._update_nested_status(
            command_id=command_id,
            external_userid=external_userid,
            write_type="lead_pool_local_status",
            status_changes={"class_term": class_term, "lead_pool_status": status},
        )

    def mark_signup_tag(
        self,
        *,
        command_id: str,
        external_userid: str,
        tag_id: str,
        tag_name: str,
        marked: bool,
        source: str,
    ) -> JsonDict:
        customer = self._require_customer(external_userid)
        tags = list(customer.get("tags") or [])
        if marked and tag_name and tag_name not in tags:
            tags.append(tag_name)
        if not marked and tag_name in tags:
            tags.remove(tag_name)
        return self._update_nested_status(
            command_id=command_id,
            external_userid=external_userid,
            write_type="signup_tag_local_marker",
            status_changes={
                "signup_tag_id": tag_id,
                "signup_label_name": tag_name,
                "signup_tag_marked": marked,
                "signup_tag_source": source,
            },
            top_level_changes={"tags": tags},
        )

    def set_followup_segment(self, *, command_id: str, external_userid: str, segment: str) -> JsonDict:
        customer = self._require_customer(external_userid)
        summary = dict(customer.get("marketing_summary") or {})
        summary["value_segment"] = segment
        profile = dict(customer.get("marketing_profile") or {})
        profile["followup_segment"] = segment
        return self._update_customer(
            command_id=command_id,
            external_userid=external_userid,
            write_type="marketing_status_local_marker",
            changes={"marketing_summary": summary, "marketing_profile": profile},
        )

    def mark_enrolled(self, *, command_id: str, external_userid: str, enrolled: bool) -> JsonDict:
        customer = self._require_customer(external_userid)
        summary = dict(customer.get("marketing_summary") or {})
        summary["enrolled"] = enrolled
        summary["main_stage"] = "converted" if enrolled else summary.get("main_stage") or "trial"
        return self._update_customer(
            command_id=command_id,
            external_userid=external_userid,
            write_type="marketing_status_local_marker",
            changes={"marketing_summary": summary},
        )

    def update_profile(
        self,
        *,
        command_id: str,
        external_userid: str,
        remark: str,
        description: str,
        display_name: str,
        source: str = "",
        industry: str = "",
        industry_description: str = "",
        needs_blockers_followup: str = "",
        updated_by: str = "",
        owner_userid: str = "",
        profile_fields_present: bool = False,
    ) -> JsonDict:
        customer = self._require_customer(external_userid)
        changes: dict[str, Any] = {}
        contact = dict(customer.get("contact") or {})
        if remark:
            changes["remark"] = remark
            contact["remark"] = remark
        if description:
            changes["description"] = description
            contact["description"] = description
        if display_name:
            changes["customer_name"] = display_name
            contact["name"] = display_name
        if contact:
            changes["contact"] = contact
        profile_fields = {
            "source": source,
            "industry": industry,
            "industry_description": industry_description,
            "needs_blockers_followup": needs_blockers_followup,
            "updated_by": updated_by,
        }
        if profile_fields_present:
            sidebar_context = dict(customer.get("sidebar_context") or {})
            sidebar_context.update(profile_fields)
            changes["sidebar_context"] = sidebar_context
            changes["profile_fields"] = profile_fields
        return self._update_customer(
            command_id=command_id,
            external_userid=external_userid,
            write_type="profile_update",
            changes=changes,
        )

    def record_material_send_plan(
        self,
        *,
        command_id: str,
        external_userid: str,
        material_id: str,
        material_type: str = "",
        operator: str = "",
        delivery_mode: str = "",
        owner_userid: str = "",
    ) -> JsonDict:
        return self._update_customer(
            command_id=command_id,
            external_userid=external_userid,
            write_type="material_send_planned",
            changes={
                "last_material_send_plan": {
                    "material_id": material_id,
                    "type": material_type,
                    "operator": operator,
                    "delivery_mode": delivery_mode,
                }
            },
        )

    def _require_customer(self, external_userid: str) -> JsonDict:
        customer = self._customers.get(external_userid)
        if not customer:
            raise KeyError("customer not found")
        return customer

    def _update_nested_status(
        self,
        *,
        command_id: str,
        external_userid: str,
        write_type: str,
        status_changes: dict[str, Any],
        top_level_changes: dict[str, Any] | None = None,
    ) -> JsonDict:
        customer = self._require_customer(external_userid)
        class_user_status = dict(customer.get("class_user_status") or {})
        class_user_status.update(status_changes)
        return self._update_customer(
            command_id=command_id,
            external_userid=external_userid,
            write_type=write_type,
            changes={"class_user_status": class_user_status, **(top_level_changes or {})},
        )

    def _update_customer(
        self,
        *,
        command_id: str,
        external_userid: str,
        write_type: str,
        changes: dict[str, Any],
    ) -> JsonDict:
        customer = self._require_customer(external_userid)
        now = utcnow_iso()
        for key, value in changes.items():
            customer[key] = deepcopy(value)
        customer["updated_at"] = now
        self._writes.append(
            SidebarWriteProjection(
                command_id=command_id,
                external_userid=external_userid,
                write_type=write_type,
                payload=deepcopy(changes),
                updated_at=now,
            )
        )
        return {"external_userid": external_userid, "write_type": write_type, "updated_at": now, "changes": deepcopy(changes)}


class PostgresSidebarWriteRepository:
    def __init__(self, *, identity_write_port: IdentityWritePort, database_url: str | None = None) -> None:
        self._identity_write_port = identity_write_port
        self._database_url = _psycopg_url(str(database_url or raw_database_url()).strip())
        if not self._database_url:
            raise RepositoryProviderError("sidebar_write production repository unavailable: DATABASE_URL is required")

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row

            return psycopg.connect(self._database_url, row_factory=dict_row)
        except Exception as exc:
            raise RepositoryProviderError(f"sidebar_write production repository unavailable: {exc}") from exc

    def bind_mobile(
        self,
        *,
        command_id: str,
        external_userid: str,
        mobile: str,
        owner_userid: str = "",
        bind_by_userid: str = "",
        force_rebind: bool = False,
    ) -> JsonDict:
        normalized_external_userid = str(external_userid or "").strip()
        normalized_mobile = normalize_mobile(mobile)
        if not normalized_external_userid:
            raise ValueError("external_userid is required")

        with self._connect() as conn:
            identity = self._identity_row(conn, normalized_external_userid)
            requested_owner_userid = str(owner_userid or "").strip()
            normalized_owner_userid = requested_owner_userid or str((identity or {}).get("primary_owner_userid") or "").strip()
            normalized_bind_by_userid = str(bind_by_userid or "").strip() or normalized_owner_userid or "sidebar_bind"
            identity_owner_userid = str((identity or {}).get("primary_owner_userid") or "").strip()
            owner_candidates = self._contact_owner_userids(
                conn,
                normalized_external_userid,
                identity_owner_userid=identity_owner_userid,
            )
            if identity and requested_owner_userid and owner_candidates and requested_owner_userid not in owner_candidates:
                raise KeyError("customer not found")

            if not identity or not _text(identity.get("unionid")):
                resolution = self._identity_write_port.enqueue_sidebar_identity_resolution(
                    conn,
                    command_id=command_id,
                    external_userid=normalized_external_userid,
                    mobile=normalized_mobile,
                    owner_userid=normalized_owner_userid,
                    bind_by_userid=normalized_bind_by_userid,
                )
                conn.commit()
                return {
                    "external_userid": normalized_external_userid,
                    "write_type": "identity_pending",
                    "write_model_status": "pending_identity",
                    "updated_at": "",
                    "changes": {},
                    "binding": {},
                    "identity_resolution": resolution,
                }

            mobile_resolution = resolve_identity_with_dbapi(
                conn,
                ResolvePersonIdentityRequest(mobile=normalized_mobile),
                for_update=True,
            )
            mobile_unionid = resolved_unionid(mobile_resolution)
            if mobile_resolution.status in {"pending", "conflict"} or (
                mobile_unionid and mobile_unionid != _text(identity.get("unionid"))
            ):
                raise ValueError("mobile already bound to another unionid")

            existing_mobile = str(identity.get("mobile") or "").strip()
            if existing_mobile == normalized_mobile:
                binding = self._binding_response(identity, owner_userid=normalized_owner_userid)
                return {
                    "external_userid": normalized_external_userid,
                    "unionid": str(identity.get("unionid") or "").strip(),
                    "write_type": "binding_noop",
                    "write_model_status": "updated",
                    "updated_at": str(identity.get("updated_at") or ""),
                    "changes": {},
                    "binding": binding,
                    "lead_pool_merge": {"ok": True, "merge_applied": False, "action_type": "customer_mobile_bound_event"},
                }
            if existing_mobile and existing_mobile != normalized_mobile and not force_rebind:
                raise ValueError("unionid already bound to another mobile")

            updated = self._identity_write_port.bind_sidebar_mobile(
                conn,
                unionid=_text(identity.get("unionid")),
                external_userid=normalized_external_userid,
                mobile=normalized_mobile,
                owner_userid=normalized_owner_userid,
                bind_by_userid=normalized_bind_by_userid,
            )
            conn.commit()

            binding = self._binding_response(dict(updated), owner_userid=normalized_owner_userid)
            return {
                "external_userid": normalized_external_userid,
                "unionid": str(updated.get("unionid") or "").strip(),
                "write_type": "binding_update",
                "write_model_status": "updated",
                "updated_at": str(updated.get("updated_at") or ""),
                "changes": {
                    "mobile": normalized_mobile,
                    "binding": {
                        "is_bound": True,
                        "mobile": normalized_mobile,
                        "binding_status": "bound",
                        "unionid": str(updated.get("unionid") or "").strip(),
                    },
                },
                "binding": binding,
                "lead_pool_merge": {"ok": True, "merge_applied": False, "action_type": "customer_mobile_bound_event"},
            }

    def update_profile(
        self,
        *,
        command_id: str,
        external_userid: str,
        remark: str,
        description: str,
        display_name: str,
        source: str = "",
        industry: str = "",
        industry_description: str = "",
        needs_blockers_followup: str = "",
        updated_by: str = "",
        owner_userid: str = "",
        profile_fields_present: bool = False,
    ) -> JsonDict:
        normalized_external_userid = _text(external_userid)
        normalized_updated_by = _text(updated_by) or _text(owner_userid) or "sidebar_profile"
        profile_fields = {
            "source": _text(source),
            "industry": _text(industry),
            "industry_description": _text(industry_description),
            "needs_blockers_followup": _text(needs_blockers_followup),
            "updated_by": normalized_updated_by,
        }
        contact_changes = {
            "remark": _text(remark),
            "description": _text(description),
            "display_name": _text(display_name),
        }
        with self._connect() as conn:
            identity = self._require_identity_for_write(conn, normalized_external_userid, owner_userid=owner_userid)
            unionid = _text(identity.get("unionid"))
            changes: dict[str, Any] = {}
            updated_at = ""
            if profile_fields_present:
                profile_row = build_sidebar_customer_profile_projection_port().save_fields_dbapi(
                    conn,
                    request=SaveSidebarProfileFieldsRequest(
                        unionid=unionid,
                        source=profile_fields["source"],
                        industry=profile_fields["industry"],
                        industry_description=profile_fields["industry_description"],
                        needs_blockers_followup=profile_fields["needs_blockers_followup"],
                        updated_by=profile_fields["updated_by"],
                        customer_id=int(identity.get("customer_id") or 0),
                    ),
                )
                if profile_row:
                    changes["profile_fields"] = {
                        "source": _text(profile_row.get("source")),
                        "industry": _text(profile_row.get("industry")),
                        "industry_description": _text(profile_row.get("industry_description")),
                        "needs_blockers_followup": _text(profile_row.get("needs_blockers_followup")),
                        "updated_by": _text(profile_row.get("updated_by")),
                    }
                    updated_at = _text(profile_row.get("updated_at"))
            if any(contact_changes.values()) and unionid:
                identity_row = self._identity_write_port.update_sidebar_contact_profile(
                    conn,
                    unionid=unionid,
                    display_name=contact_changes["display_name"],
                    remark=contact_changes["remark"],
                    description=contact_changes["description"],
                    updated_by=normalized_updated_by,
                )
                if identity_row:
                    changes["contact"] = {
                        "customer_name": _text(identity_row.get("customer_name")),
                        "remark": _text(identity_row.get("remark")),
                        "description": _text((identity_row.get("profile_json") or {}).get("description")),
                    }
                    updated_at = _text(identity_row.get("updated_at")) or updated_at
            elif any(contact_changes.values()):
                # The local OneID customer is already usable without UnionID.
                # Provider-owned remark/name fields remain an External Effect;
                # do not bypass identity_contact by mutating its mirror here.
                changes["contact"] = {
                    "customer_name": contact_changes["display_name"],
                    "remark": contact_changes["remark"],
                    "description": contact_changes["description"],
                }
            conn.commit()
            return {
                "external_userid": normalized_external_userid,
                "unionid": unionid,
                "write_type": "profile_update",
                "write_model_status": "updated",
                "updated_at": updated_at,
                "changes": changes,
            }

    def record_material_send_plan(
        self,
        *,
        command_id: str,
        external_userid: str,
        material_id: str,
        material_type: str = "",
        operator: str = "",
        delivery_mode: str = "",
        owner_userid: str = "",
    ) -> JsonDict:
        normalized_external_userid = _text(external_userid)
        normalized_type = _text(material_type) or "image"
        normalized_material_id = _text(material_id)
        with self._connect() as conn:
            identity = self._require_identity_for_write(conn, normalized_external_userid, owner_userid=owner_userid)
            material = self._material_reference(conn, material_type=normalized_type, material_id=normalized_material_id)
            if not material:
                raise KeyError("material not found")
            media_id = _usable_wecom_media_id(material.get("media_id"))
            if normalized_type == "image" and not media_id:
                raise ValueError("image material media_id is required before sending")
            plan = {
                "command_id": command_id,
                "external_userid": normalized_external_userid,
                "material_id": normalized_material_id,
                "type": normalized_type,
                "operator": _text(operator),
                "delivery_mode": _text(delivery_mode),
                "media_id": media_id,
                "title": _text(material.get("title")),
            }
            updated = self._identity_write_port.record_sidebar_material_send_plan(
                conn,
                unionid=_text(identity.get("unionid")),
                plan=plan,
            )
            conn.commit()
            return {
                "external_userid": normalized_external_userid,
                "unionid": _text(identity.get("unionid")),
                "write_type": "material_send_planned",
                "write_model_status": "planned",
                "updated_at": _text((updated or {}).get("updated_at")),
                "media_id": media_id,
                "changes": {"last_material_send_plan": plan},
            }

    def _identity_row(self, conn, external_userid: str) -> JsonDict | None:
        oneid = conn.execute(
            """
            SELECT COALESCE(root.id, customer.id) AS customer_id,
                   COALESCE(identity.unionid, '') AS unionid,
                   COALESCE(NULLIF(identity.primary_external_userid, ''), identity_map.external_userid) AS primary_external_userid,
                   identity.mobile,
                   identity.mobile_normalized,
                   identity.mobile_source,
                   COALESCE(NULLIF(follow_user.user_id, ''), NULLIF(identity_map.follow_user_userid, ''), identity.primary_owner_userid, '') AS primary_owner_userid,
                   COALESCE(NULLIF(identity_map.name, ''), identity.customer_name, '') AS customer_name,
                   COALESCE(NULLIF(follow_user.remark, ''), identity.remark, '') AS remark
            FROM wecom_external_contact_identity_map identity_map
            JOIN customers customer ON customer.id = identity_map.customer_id
            LEFT JOIN customers root ON root.id = customer.merged_into_customer_id
            LEFT JOIN crm_user_identity identity ON identity.customer_id = COALESCE(root.id, customer.id)
            LEFT JOIN wecom_external_contact_follow_users follow_user
              ON follow_user.corp_id = identity_map.corp_id
             AND follow_user.external_userid = identity_map.external_userid
             AND follow_user.relation_status = 'active'
            WHERE identity_map.external_userid = %s
              AND identity_map.status = 'active'
              AND COALESCE(root.status, customer.status) = 'active'
            ORDER BY follow_user.is_primary DESC NULLS LAST, follow_user.updated_at DESC NULLS LAST
            LIMIT 1
            FOR UPDATE OF identity_map, customer
            """,
            (external_userid,),
        ).fetchone()
        if oneid:
            return dict(oneid)
        resolution = resolve_identity_with_dbapi(
            conn,
            ResolvePersonIdentityRequest(external_userid=external_userid),
            for_update=True,
        )
        if resolution.status == "conflict":
            raise ValueError("external identity already bound to multiple unionids")
        if resolution.status != "resolved" or resolution.identity is None:
            return None
        identity = resolution.identity
        return {
            "unionid": resolved_unionid(resolution),
            "primary_external_userid": _text(identity.external_userid),
            "mobile": _text(identity.mobile),
            "mobile_normalized": _text(identity.mobile),
            "mobile_source": _text(identity.mobile_source),
            "primary_owner_userid": _text(identity.owner_userid),
            "customer_name": _text(identity.customer_name),
            "remark": _text(identity.remark),
        }

    def _require_identity_for_write(self, conn, external_userid: str, *, owner_userid: str = "") -> JsonDict:
        identity = self._identity_row(conn, external_userid)
        if not identity:
            raise KeyError("customer not found")
        requested_owner_userid = _text(owner_userid)
        identity_owner_userid = _text(identity.get("primary_owner_userid"))
        owner_candidates = self._contact_owner_userids(
            conn,
            external_userid,
            identity_owner_userid=identity_owner_userid,
        )
        if requested_owner_userid and owner_candidates and requested_owner_userid not in owner_candidates:
            raise KeyError("customer not found")
        return identity

    def _material_reference(self, conn, *, material_type: str, material_id: str) -> JsonDict | None:
        try:
            item_id = int(material_id)
        except (TypeError, ValueError):
            raise ValueError("material_id must be numeric") from None
        normalized_type = _text(material_type) or "image"
        if normalized_type == "image":
            row = conn.execute(
                """
                SELECT id, COALESCE(NULLIF(name, ''), NULLIF(file_name, '')) AS title, thumb_media_id AS media_id
                FROM image_library
                WHERE id = %s AND COALESCE(enabled, TRUE) IS TRUE
                LIMIT 1
                """,
                (item_id,),
            ).fetchone()
        elif normalized_type == "mini":
            row = conn.execute(
                """
                SELECT id, COALESCE(NULLIF(title, ''), NULLIF(name, '')) AS title, thumb_media_id AS media_id
                FROM miniprogram_library
                WHERE id = %s AND COALESCE(enabled, TRUE) IS TRUE
                LIMIT 1
                """,
                (item_id,),
            ).fetchone()
        elif normalized_type in {"pdf", "attachment"}:
            row = conn.execute(
                """
                SELECT id, COALESCE(NULLIF(name, ''), NULLIF(file_name, '')) AS title, media_id
                FROM attachment_library
                WHERE id = %s AND COALESCE(enabled, TRUE) IS TRUE
                LIMIT 1
                """,
                (item_id,),
            ).fetchone()
        else:
            raise ValueError("type must be image, mini, or pdf")
        return dict(row) if row else None

    def _contact_owner_userids(
        self,
        conn,
        external_userid: str,
        *,
        identity_owner_userid: str = "",
    ) -> set[str]:
        candidates = {str(identity_owner_userid or "").strip()} if str(identity_owner_userid or "").strip() else set()
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT owner_userid
                FROM (
                    SELECT COALESCE(NULLIF(user_id, ''), NULLIF(raw_follow_user ->> 'userid', '')) AS owner_userid
                    FROM wecom_external_contact_follow_users
                    WHERE external_userid = %s
                      AND COALESCE(relation_status, 'active') = 'active'
                    UNION ALL
                    SELECT NULLIF(follow_user_userid, '') AS owner_userid
                    FROM wecom_external_contact_identity_map
                    WHERE external_userid = %s
                ) owners
                WHERE COALESCE(owner_userid, '') <> ''
                """,
                (external_userid, external_userid),
            ).fetchall()
        except Exception:
            return candidates
        for row in rows or []:
            if isinstance(row, dict):
                owner = str(row.get("owner_userid") or "").strip()
            else:
                owner = str(row[0] if row else "").strip()
            if owner:
                candidates.add(owner)
        return candidates

    def _binding_response(self, row: JsonDict, *, owner_userid: str) -> JsonDict:
        unionid = str(row.get("unionid") or "").strip()
        external_userid = str(row.get("primary_external_userid") or "").strip()
        display_name = str(row.get("customer_name") or row.get("remark") or "").strip() or f"客户 {unionid[-6:]}"
        return {
            "is_bound": True,
            "unionid": unionid,
            "external_userid": external_userid,
            "owner_userid": owner_userid or str(row.get("primary_owner_userid") or "").strip(),
            "customer_name": str(row.get("customer_name") or "").strip(),
            "remark": str(row.get("remark") or "").strip(),
            "display_name": display_name,
            "mobile": str(row.get("mobile") or "").strip(),
            "mobile_source": str(row.get("mobile_source") or "sidebar_bind").strip(),
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            "detail_url": f"/admin/customers/{unionid}",
        }
