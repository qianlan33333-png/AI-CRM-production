from __future__ import annotations

from typing import Any, Type
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response

from aicrm_next.platform.shared.signed_context import (
    SIDEBAR_VIEWER_SESSION_COOKIE,
    validate_sidebar_owner_context,
)
from aicrm_next.platform.shared.runtime_settings import managed_runtime_setting
from aicrm_next.platform.shared.runtime import database_mode
from aicrm_next.crm.identity_contact.repo import FixtureIdentityRepository, PostgresIdentityRepository

from .application import (
    SidebarWriteConflictError,
    SidebarWriteForbiddenError,
    SidebarWriteInputError,
    SidebarWriteNotFoundError,
    SidebarWriteProductionUnavailableError,
    execute_sidebar_write,
)
from .commands import (
    BindMobileCommand,
    MarkEnrolledCommand,
    MarkSignupTagCommand,
    PlanMaterialSendCommand,
    SetFollowupSegmentCommand,
    SidebarWriteCommand,
    UnmarkEnrolledCommand,
    UpdateSidebarProfileCommand,
    UpsertLeadPoolClassTermCommand,
)

router = APIRouter()


@router.api_route("/api/sidebar/bind-mobile", methods=["POST", "OPTIONS"])
async def bind_mobile(request: Request):
    return await _execute(request, BindMobileCommand)


@router.api_route("/api/sidebar/lead-pool/upsert-class-term", methods=["POST", "OPTIONS"])
async def upsert_lead_pool_class_term(request: Request):
    return await _execute(request, UpsertLeadPoolClassTermCommand)


@router.api_route("/api/sidebar/signup-tags/mark", methods=["POST", "OPTIONS"])
async def mark_signup_tag(request: Request):
    return await _execute(request, MarkSignupTagCommand)


@router.api_route("/api/sidebar/marketing-status/set-followup-segment", methods=["POST", "OPTIONS"])
async def set_followup_segment(request: Request):
    return await _execute(request, SetFollowupSegmentCommand)


@router.api_route("/api/sidebar/marketing-status/mark-enrolled", methods=["POST", "OPTIONS"])
async def mark_enrolled(request: Request):
    return await _execute(request, MarkEnrolledCommand)


@router.api_route("/api/sidebar/marketing-status/unmark-enrolled", methods=["POST", "OPTIONS"])
async def unmark_enrolled(request: Request):
    return await _execute(request, UnmarkEnrolledCommand)


@router.api_route("/api/sidebar/v2/profile", methods=["PUT", "OPTIONS"])
async def update_sidebar_v2_profile(request: Request):
    return await _execute(request, UpdateSidebarProfileCommand)


@router.api_route("/api/sidebar/v2/materials/send", methods=["POST", "OPTIONS"])
async def plan_material_send(request: Request):
    return await _execute(request, PlanMaterialSendCommand)


async def _execute(request: Request, command_type: Type[SidebarWriteCommand]) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204)
    try:
        body = await _json_body(request)
        external_userid = str(body.get("external_userid") or "").strip()
        trusted_context = _trusted_sidebar_context(request, external_userid=external_userid)
        trusted_owner_userid = str(trusted_context.get("owner_userid") or "").strip()
        claimed_values = {
            str(body.get(key) or "").strip()
            for key in ("owner_userid", "bind_by_userid", "actor_id")
            if str(body.get(key) or "").strip()
        }
        if any(value != trusted_owner_userid for value in claimed_values):
            raise SidebarWriteForbiddenError("sidebar owner scope forbidden")
        body["owner_userid"] = trusted_owner_userid
        body["bind_by_userid"] = trusted_owner_userid
        command = command_type(
            idempotency_key=_idempotency_key(request, body),
            actor_id=trusted_owner_userid,
            actor_type="sidebar_owner",
            external_userid=external_userid,
            payload={key: value for key, value in body.items() if key not in {"actor_id", "actor_type", "external_userid", "idempotency_key", "dry_run", "trace_id"}},
            dry_run=_as_bool(body.get("dry_run")),
            source_route=request.url.path,
            trace_id=str(body.get("trace_id") or request.headers.get("X-Request-Id") or uuid4().hex),
        )
        payload = execute_sidebar_write(command)
        return JSONResponse(jsonable_encoder(payload), status_code=200)
    except SidebarWriteInputError as exc:
        return _error(str(exc), status_code=400, source_status="input_error", write_model_status="input_error")
    except SidebarWriteConflictError as exc:
        return _error(str(exc), status_code=409, source_status="conflict", write_model_status="conflict")
    except SidebarWriteForbiddenError as exc:
        return _error(str(exc), status_code=403, source_status="forbidden", write_model_status="blocked")
    except SidebarWriteNotFoundError as exc:
        return _error(str(exc), status_code=404, source_status="not_found", write_model_status="not_found")
    except SidebarWriteProductionUnavailableError as exc:
        return _error(str(exc), status_code=503, source_status="production_unavailable", write_model_status="unavailable", degraded=True)


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise SidebarWriteInputError("json object body is required")
    return payload


def _trusted_sidebar_context(request: Request, *, external_userid: str) -> dict[str, Any]:
    context = dict(getattr(request.state, "sidebar_context", {}) or {})
    if context:
        if str(context.get("external_userid") or "").strip() != str(external_userid or "").strip():
            raise SidebarWriteForbiddenError("sidebar customer scope forbidden")
        _require_active_relationship(context)
        return context
    result = validate_sidebar_owner_context(
        token=str(request.headers.get("X-AICRM-Sidebar-Owner-Token") or "").strip(),
        viewer_session_cookie=str(request.cookies.get(SIDEBAR_VIEWER_SESSION_COOKIE) or "").strip(),
        external_userid=external_userid,
        expected_corp_id=str(managed_runtime_setting("WECOM_CORP_ID") or "").strip(),
    )
    if not result.get("ok"):
        raise SidebarWriteForbiddenError("sidebar context required")
    context = dict(result.get("context") or {})
    _require_active_relationship(context)
    return context


def _require_active_relationship(context: dict[str, Any]) -> None:
    repository = PostgresIdentityRepository() if database_mode() == "postgres" else FixtureIdentityRepository()
    if not repository.has_active_follow_relation(
        corp_id=str(context.get("corp_id") or managed_runtime_setting("WECOM_CORP_ID") or "").strip(),
        user_id=str(context.get("owner_userid") or context.get("viewer_userid") or "").strip(),
        external_userid=str(context.get("external_userid") or "").strip(),
    ):
        raise SidebarWriteForbiddenError("sidebar customer scope forbidden")


def _idempotency_key(request: Request, body: dict[str, Any]) -> str:
    return str(request.headers.get("Idempotency-Key") or body.get("idempotency_key") or "").strip()


def _as_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _error(
    message: str,
    *,
    status_code: int,
    source_status: str,
    write_model_status: str,
    degraded: bool = False,
) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": message,
            "source_status": source_status,
            "write_model_status": write_model_status,
            "route_owner": "ai_crm_next",
            "fallback_used": False,
            "real_external_call_executed": False,
            "degraded": degraded,
        },
        status_code=status_code,
    )
