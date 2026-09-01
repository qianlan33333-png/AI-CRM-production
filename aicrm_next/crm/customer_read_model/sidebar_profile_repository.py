from __future__ import annotations

from typing import Any

from .sidebar_profile_port import SaveSidebarProfileFieldsRequest


def _text(value: Any) -> str:
    return str(value or "").strip()


class PostgresSidebarCustomerProfileProjectionRepository:
    """Canonical DBAPI writer for sidebar customer profile fields."""

    def save_fields_dbapi(
        self,
        executor: Any,
        *,
        request: SaveSidebarProfileFieldsRequest,
    ) -> dict[str, Any]:
        unionid = _text(request.unionid)
        customer_id = int(request.customer_id or 0)
        if customer_id <= 0 and not unionid:
            raise ValueError("customer_id or unionid is required")
        if customer_id > 0:
            row = executor.execute(
                """
                INSERT INTO sidebar_customer_profile_fields (
                    customer_id, unionid, source, industry, industry_description,
                    needs_blockers_followup, updated_by, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (customer_id) WHERE customer_id IS NOT NULL DO UPDATE SET
                    unionid = COALESCE(NULLIF(EXCLUDED.unionid, ''), sidebar_customer_profile_fields.unionid),
                    source = EXCLUDED.source,
                    industry = EXCLUDED.industry,
                    industry_description = EXCLUDED.industry_description,
                    needs_blockers_followup = EXCLUDED.needs_blockers_followup,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = NOW()
                RETURNING source, industry, industry_description,
                          needs_blockers_followup, updated_by, updated_at
                """,
                (
                    customer_id,
                    unionid,
                    _text(request.source),
                    _text(request.industry),
                    _text(request.industry_description),
                    _text(request.needs_blockers_followup),
                    _text(request.updated_by),
                ),
            ).fetchone()
            if not row:
                raise RuntimeError("sidebar customer profile projection write returned no row")
            return dict(row)
        row = executor.execute(
            """
            INSERT INTO sidebar_customer_profile_fields (
                unionid,
                source,
                industry,
                industry_description,
                needs_blockers_followup,
                updated_by,
                created_at,
                updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (unionid) WHERE unionid <> '' DO UPDATE SET
                source = EXCLUDED.source,
                industry = EXCLUDED.industry,
                industry_description = EXCLUDED.industry_description,
                needs_blockers_followup = EXCLUDED.needs_blockers_followup,
                updated_by = EXCLUDED.updated_by,
                updated_at = NOW()
            RETURNING
                source,
                industry,
                industry_description,
                needs_blockers_followup,
                updated_by,
                updated_at
            """,
            (
                unionid,
                _text(request.source),
                _text(request.industry),
                _text(request.industry_description),
                _text(request.needs_blockers_followup),
                _text(request.updated_by),
            ),
        ).fetchone()
        if not row:
            raise RuntimeError("sidebar customer profile projection write returned no row")
        return dict(row)


__all__ = ["PostgresSidebarCustomerProfileProjectionRepository"]
