from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from aicrm_next.crm.identity_contact.dto import ResolvePersonIdentityRequest
from aicrm_next.crm.identity_contact.resolver import SQLAlchemyIdentityResolver, resolved_unionid
from aicrm_next.platform.shared.db_session import get_engine
from aicrm_next.platform.shared.runtime import database_mode

from .read_model import TagCatalogUnavailable, build_tag_catalog_repository


Json = dict[str, Any]

_fixture_rows: list[Json] = []


def reset_customer_tag_local_projection_fixture_state() -> None:
    _fixture_rows.clear()


def get_customer_tag_local_projection_fixture_rows() -> list[Json]:
    return [dict(row) for row in _fixture_rows]


def project_questionnaire_tags(
    *,
    unionid: str = "",
    external_userid: str = "",
    owner_userid: str = "",
    tag_ids: list[str] | None = None,
    source: str = "questionnaire_h5_submit",
    questionnaire_id: int | str | None = None,
    submission_id: str | None = None,
    idempotency_key: str = "",
    engine: Engine | None = None,
) -> Json:
    normalized_tags = _normalize_tags(tag_ids or [])
    normalized_unionid = _text(unionid)
    normalized_external_userid = _text(external_userid)
    normalized_owner_userid = _text(owner_userid)
    if not normalized_tags:
        return _projection_result(
            local_projection_updated=False,
            skipped=True,
            reason="missing_tags",
            unionid=normalized_unionid,
            external_userid=normalized_external_userid,
            owner_userid=normalized_owner_userid,
            tag_ids=[],
            source=source,
            questionnaire_id=questionnaire_id,
            submission_id=submission_id,
            idempotency_key=idempotency_key,
        )

    validation = _validate_tag_catalog(normalized_tags)
    if validation.get("ok") is False:
        return _projection_result(
            ok=False,
            local_projection_updated=False,
            skipped=False,
            reason="invalid_tag_ids",
            unionid=normalized_unionid,
            external_userid=normalized_external_userid,
            owner_userid=normalized_owner_userid,
            tag_ids=normalized_tags,
            tag_names={},
            source=source,
            questionnaire_id=questionnaire_id,
            submission_id=submission_id,
            idempotency_key=idempotency_key,
            extra=validation,
        )

    if database_mode() != "postgres":
        return _project_fixture(
            unionid=normalized_unionid,
            external_userid=normalized_external_userid,
            owner_userid=normalized_owner_userid,
            tag_ids=normalized_tags,
            tag_names=dict(validation.get("tag_names") or {}),
            source=source,
            questionnaire_id=questionnaire_id,
            submission_id=submission_id,
            idempotency_key=idempotency_key,
            validation=validation,
        )

    return _project_postgres(
        engine=engine or get_engine(),
        unionid=normalized_unionid,
        external_userid=normalized_external_userid,
        owner_userid=normalized_owner_userid,
        tag_ids=normalized_tags,
        tag_names=dict(validation.get("tag_names") or {}),
        source=source,
        questionnaire_id=questionnaire_id,
        submission_id=submission_id,
        idempotency_key=idempotency_key,
        validation=validation,
    )


def validate_questionnaire_tag_ids(tag_ids: list[str] | None) -> Json:
    normalized_tags = _normalize_tags(tag_ids or [])
    if not normalized_tags:
        return {
            "ok": False,
            "reason": "tag_ids_missing",
            "tag_ids": [],
            "invalid_tag_ids": [],
            "tag_names": {},
        }
    validation = _validate_tag_catalog(normalized_tags)
    if validation.get("ok") is False:
        return {
            **validation,
            "ok": False,
            "reason": "tag_ids_missing",
            "tag_ids": normalized_tags,
        }
    return {
        **validation,
        "ok": True,
        "reason": "",
        "tag_ids": normalized_tags,
    }


def _project_fixture(
    *,
    unionid: str,
    external_userid: str,
    owner_userid: str,
    tag_ids: list[str],
    tag_names: dict[str, str],
    source: str,
    questionnaire_id: int | str | None,
    submission_id: str | None,
    idempotency_key: str,
    validation: Json,
) -> Json:
    if not unionid and not external_userid:
        return _projection_result(
            local_projection_updated=False,
            skipped=True,
            reason="identity_missing",
            unionid=unionid,
            external_userid=external_userid,
            owner_userid=owner_userid,
            tag_ids=tag_ids,
            tag_names=tag_names,
            source=source,
            questionnaire_id=questionnaire_id,
            submission_id=submission_id,
            idempotency_key=idempotency_key,
            extra=validation,
        )
    effective_unionid = unionid or f"external:{external_userid}"
    now = _timestamp()
    updated_count = 0
    inserted_count = 0
    for tag_id in tag_ids:
        existing = next(
            (row for row in _fixture_rows if row.get("unionid") == effective_unionid and row.get("userid") == owner_userid and row.get("tag_id") == tag_id),
            None,
        )
        if existing:
            existing.update(
                {
                    "tag_name": tag_names.get(tag_id) or tag_id,
                    "source": source,
                    "questionnaire_id": _text(questionnaire_id),
                    "submission_id": _text(submission_id),
                    "idempotency_key": idempotency_key,
                    "updated_at": now,
                }
            )
            updated_count += 1
        else:
            _fixture_rows.append(
                {
                    "unionid": effective_unionid,
                    "external_userid": external_userid,
                    "userid": owner_userid,
                    "tag_id": tag_id,
                    "tag_name": tag_names.get(tag_id) or tag_id,
                    "created_at": now,
                    "updated_at": now,
                    "source": source,
                    "questionnaire_id": _text(questionnaire_id),
                    "submission_id": _text(submission_id),
                    "idempotency_key": idempotency_key,
                }
            )
            inserted_count += 1
    return _projection_result(
        local_projection_updated=bool(inserted_count or updated_count),
        skipped=False,
        reason="",
        unionid=effective_unionid,
        external_userid=external_userid,
        owner_userid=owner_userid,
        tag_ids=tag_ids,
        tag_names=tag_names,
        source=source,
        questionnaire_id=questionnaire_id,
        submission_id=submission_id,
        idempotency_key=idempotency_key,
        updated_count=updated_count,
        inserted_count=inserted_count,
        extra=validation,
    )


def _project_postgres(
    *,
    engine: Engine,
    unionid: str,
    external_userid: str,
    owner_userid: str,
    tag_ids: list[str],
    tag_names: dict[str, str],
    source: str,
    questionnaire_id: int | str | None,
    submission_id: str | None,
    idempotency_key: str,
    validation: Json,
) -> Json:
    with engine.begin() as connection:
        effective_unionid = resolved_unionid(
            SQLAlchemyIdentityResolver(connection).resolve(
                ResolvePersonIdentityRequest(
                    unionid=unionid or None,
                    external_userid=external_userid or None,
                )
            )
        )
        customer_row = connection.execute(
            text(
                """
                SELECT COALESCE(root.id, customer.id) AS customer_id
                FROM wecom_external_contact_identity_map identity_map
                JOIN customers customer ON customer.id = identity_map.customer_id
                LEFT JOIN customers root ON root.id = customer.merged_into_customer_id
                WHERE identity_map.external_userid = :external_userid
                  AND identity_map.status = 'active'
                ORDER BY identity_map.updated_at DESC, identity_map.id DESC
                LIMIT 1
                """
            ),
            {"external_userid": external_userid},
        ).mappings().fetchone()
        customer_id = int((customer_row or {}).get("customer_id") or 0)
        if not effective_unionid and customer_id <= 0:
            return _projection_result(
                local_projection_updated=False,
                skipped=True,
                reason="identity_missing",
                unionid="",
                external_userid=external_userid,
                owner_userid=owner_userid,
                tag_ids=tag_ids,
                tag_names=tag_names,
                source=source,
                questionnaire_id=questionnaire_id,
                submission_id=submission_id,
                idempotency_key=idempotency_key,
                extra=validation,
            )
        updated_count = 0
        inserted_count = 0
        for tag_id in tag_ids:
            result = connection.execute(
                text(
                    """
                    UPDATE contact_tags
                    SET tag_name = :tag_name,
                        source = :source,
                        questionnaire_id = :questionnaire_id,
                        submission_id = :submission_id,
                        idempotency_key = :idempotency_key,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE (customer_id = :customer_id OR (:customer_id = 0 AND unionid = :unionid))
                      AND userid = :userid
                      AND tag_id = :tag_id
                    """
                ),
                {
                    "tag_name": tag_names.get(tag_id) or tag_id,
                    "source": source,
                    "questionnaire_id": _text(questionnaire_id),
                    "submission_id": _text(submission_id),
                    "idempotency_key": idempotency_key,
                    "unionid": effective_unionid,
                    "customer_id": customer_id,
                    "userid": owner_userid,
                    "tag_id": tag_id,
                },
            )
            if int(result.rowcount or 0) > 0:
                updated_count += int(result.rowcount or 0)
                continue
            connection.execute(
                text(
                    """
                    INSERT INTO contact_tags (
                        customer_id, unionid, userid, tag_id, tag_name, source,
                        questionnaire_id, submission_id, idempotency_key,
                        created_at, updated_at
                    )
                    VALUES (
                        NULLIF(:customer_id, 0), :unionid, :userid, :tag_id, :tag_name, :source,
                        :questionnaire_id, :submission_id, :idempotency_key,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "unionid": effective_unionid,
                    "customer_id": customer_id,
                    "userid": owner_userid,
                    "tag_id": tag_id,
                    "tag_name": tag_names.get(tag_id) or tag_id,
                    "source": source,
                    "questionnaire_id": _text(questionnaire_id),
                    "submission_id": _text(submission_id),
                    "idempotency_key": idempotency_key,
                },
            )
            inserted_count += 1
    return _projection_result(
        local_projection_updated=bool(inserted_count or updated_count),
        skipped=False,
        reason="",
        unionid=effective_unionid,
        external_userid=external_userid,
        owner_userid=owner_userid,
        tag_ids=tag_ids,
        tag_names=tag_names,
        source=source,
        questionnaire_id=questionnaire_id,
        submission_id=submission_id,
        idempotency_key=idempotency_key,
        updated_count=updated_count,
        inserted_count=inserted_count,
        extra=validation,
    )


def _validate_tag_catalog(tag_ids: list[str]) -> Json:
    if database_mode() != "postgres":
        return {"ok": True, "tag_catalog_validation": "unavailable_or_fixture", "invalid_tag_ids": [], "tag_names": {}}
    try:
        catalog = build_tag_catalog_repository().list_catalog().to_payload()
    except TagCatalogUnavailable:
        return {"ok": True, "tag_catalog_validation": "unavailable_or_fixture", "invalid_tag_ids": [], "tag_names": {}}
    tag_names = {str(tag.get("tag_id") or "").strip(): str(tag.get("tag_name") or "").strip() for tag in catalog.get("tags") or []}
    invalid = [tag_id for tag_id in tag_ids if tag_id not in tag_names]
    return {
        "ok": not invalid,
        "tag_catalog_validation": "validated",
        "invalid_tag_ids": invalid,
        "tag_names": tag_names,
    }


def _projection_result(
    *,
    local_projection_updated: bool,
    skipped: bool,
    reason: str,
    unionid: str,
    external_userid: str,
    owner_userid: str,
    tag_ids: list[str],
    source: str,
    questionnaire_id: int | str | None,
    submission_id: str | None,
    idempotency_key: str,
    ok: bool = True,
    tag_names: dict[str, str] | None = None,
    updated_count: int = 0,
    inserted_count: int = 0,
    extra: Json | None = None,
) -> Json:
    return {
        "ok": ok,
        "source_status": "customer_tag_local_projection",
        "route_owner": "ai_crm_next",
        "fallback_used": False,
        "real_external_call_executed": False,
        "local_projection_supported": True,
        "local_projection_updated": local_projection_updated,
        "local_projection_status": "updated" if local_projection_updated else ("skipped" if skipped else "blocked"),
        "skipped": skipped,
        "reason": reason,
        "unionid": unionid,
        "external_userid": external_userid,
        "owner_userid": owner_userid,
        "tag_ids": list(tag_ids),
        "tag_names": dict(tag_names or {}),
        "source": source,
        "questionnaire_id": _text(questionnaire_id),
        "submission_id": _text(submission_id),
        "idempotency_key": idempotency_key,
        "updated_count": updated_count,
        "inserted_count": inserted_count,
        **dict(extra or {}),
    }


def _normalize_tags(tag_ids: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in tag_ids:
        tag_id = _text(raw)
        if not tag_id or tag_id in seen:
            continue
        seen.add(tag_id)
        normalized.append(tag_id)
    return normalized


def _text(value: Any) -> str:
    return str(value or "").strip()


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
