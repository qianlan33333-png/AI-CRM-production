from __future__ import annotations

import base64
import csv
from datetime import datetime, timezone
import json
import logging
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from aicrm_next.platform.navigation_target import safe_completion_url
from aicrm_next.crm.identity_contact.wechat_unionid_guard import evaluate_wechat_unionid_access
from aicrm_next.platform.shared.errors import ContractError, NotFoundError
from aicrm_next.platform.shared.pii_audit import infer_pii_result_count, set_pii_audit_result_count
from aicrm_next.platform.shared.runtime import production_data_ready
from aicrm_next.platform.shared.safe_logging import safe_log_fields
from aicrm_next.platform.shared.signed_session import session_cookie_secure
from aicrm_next.platform.shared.wechat_h5_session import payment_identity_from_request
from aicrm_next.platform.shared.wechat_identity_page import wechat_identity_failure_response

from .admin_write import (
    QuestionnaireAdminWriteCommand,
    QuestionnaireAdminWriteInputError,
    QuestionnaireAdminWriteNotFoundError,
    QuestionnaireAdminWriteProductionUnavailableError,
    execute_questionnaire_admin_write,
)
from .h5_write import (
    QuestionnaireH5AlreadySubmittedError,
    QuestionnaireClientDiagnosticsCommand,
    QuestionnaireH5SubmitCommand,
    QuestionnaireH5WriteInputError,
    QuestionnaireH5WriteNotFoundError,
    QuestionnaireH5WriteProductionUnavailableError,
    execute_questionnaire_client_diagnostics,
    execute_questionnaire_h5_submit,
)
from .application import (
    CompleteWechatOAuthCallbackCommand,
    GetPublicQuestionnaireQuery,
    GetPublicQuestionnaireSubmissionStatusQuery,
    GetQuestionnaireDetailQuery,
    GetQuestionnairePreflightQuery,
    GetQuestionnaireShareQuery,
    GetQuestionnaireResultsSummaryQuery,
    GetSubmissionResultQuery,
    LatestSubmitDebugQuery,
    ListExternalQuestionnaireSubmissionsQuery,
    ListQuestionnaireQuestionsQuery,
    ListQuestionnaireSubmissionsQuery,
    ListQuestionnairesQuery,
    StartWechatOAuthQuery,
)
from .dto import OAuthCallbackRequest, OAuthStartRequest
from .oauth import COOKIE_NAME, questionnaire_h5_identity_from_cookies, questionnaire_oauth_state_context
from .public_access import QuestionnaireRespondentIdentityService
from .result_access import issue_questionnaire_result_grant
from .operations import (
    QuestionnaireOperationsConflictError,
    QuestionnaireOperationsInputError,
    QuestionnaireOperationsNotFoundError,
    QuestionnaireOperationsService,
    QuestionnaireOperationsUnavailableError,
)

router = APIRouter()
logger = logging.getLogger(__name__)
_TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "app" / "admin_console" / "templates"
templates = Jinja2Templates(directory=_TEMPLATES_DIR)
_EXTERNAL_SOURCE_STATUS = "external_questionnaire_submissions"

_QUESTIONNAIRE_SOURCE_PARAM_FIELDS = (
    "source_channel",
    "campaign_id",
    "staff_id",
)
_QUESTIONNAIRE_META_FIELDS = _QUESTIONNAIRE_SOURCE_PARAM_FIELDS
_IDENTITY_CONFLICT_PUBLIC_MESSAGE = "该手机号已绑定其他账号，不能提交"


def _completion_target_redirect_url(slug: str, completion_target: Any, *, fallback_url: Any = "") -> str:
    if not isinstance(completion_target, dict) or not completion_target.get("enabled"):
        return ""
    if str(completion_target.get("target_type") or "").strip() != "url_link":
        return ""
    link = completion_target.get("url_link") if isinstance(completion_target.get("url_link"), dict) else {}
    source_url = str(link.get("source_url") or "").strip()
    if source_url:
        query = {
            "source_url": source_url,
            "response_url_key": str(link.get("response_url_key") or "url_link") or "url_link",
        }
        safe_fallback = _safe_completion_fallback(slug, fallback_url)
        if safe_fallback:
            query["fallback_url"] = safe_fallback
        return f"/api/h5/navigation-target/url-link/resolve?{urlencode(query)}"
    return safe_completion_url(link.get("url"))


def _safe_completion_fallback(slug: str, fallback_url: Any) -> str:
    safe_url = safe_completion_url(fallback_url)
    if safe_url in {"", f"/s/{slug}/submitted"}:
        return ""
    return safe_url


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, NotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ContractError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


def _read_response(payload: dict[str, Any]) -> dict[str, Any] | JSONResponse:
    if payload.get("source_status") == "production_unavailable":
        return JSONResponse(jsonable_encoder(payload), status_code=503)
    return payload


def _operations_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, QuestionnaireOperationsNotFoundError):
        status_code = 404
        source_status = "not_found"
    elif isinstance(exc, QuestionnaireOperationsInputError):
        status_code = 422
        source_status = "input_error"
    elif isinstance(exc, QuestionnaireOperationsConflictError):
        status_code = 409
        source_status = "conflict"
    else:
        status_code = 503
        source_status = "production_unavailable"
    return JSONResponse(
        {
            "ok": False,
            "detail": str(exc),
            "message": str(exc),
            "source_status": source_status,
            "route_owner": "ai_crm_next",
            "fallback_used": False,
            "real_external_call_executed": False,
        },
        status_code=status_code,
    )


async def _operations_body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise QuestionnaireOperationsInputError("valid json object body is required") from exc
    if not isinstance(payload, dict):
        raise QuestionnaireOperationsInputError("json object body is required")
    return payload


def _external_text(value: Any) -> str:
    return str(value or "").strip()


def _external_error(*, error_code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error_code": error_code,
            "message": message,
            "route_owner": "ai_crm_next",
            "source_status": _EXTERNAL_SOURCE_STATUS,
            "fallback_used": False,
        },
        status_code=status_code,
    )


def _external_encode_cursor(offset: int | None) -> str:
    if offset is None:
        return ""
    payload = json.dumps({"offset": max(0, int(offset))}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _external_decode_cursor(cursor: str | None) -> int:
    token = _external_text(cursor)
    if not token:
        return 0
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        return max(0, int(payload.get("offset") or 0))
    except Exception as exc:
        raise ValueError("cursor is invalid") from exc


def _external_timestamp_filter(value: int | None, name: str) -> str | None:
    if value is None:
        return None
    if value < 0:
        raise ValueError(f"{name} must be a Unix timestamp in seconds")
    if value > 9_999_999_999:
        raise ValueError(f"{name} must be a Unix timestamp in seconds, not milliseconds")
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _external_filters(**kwargs: Any) -> dict[str, Any]:
    return {key: value for key, value in kwargs.items() if value not in {None, ""}}


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if value is None:
        return ""
    return value


def _csv_download_response(payload: dict[str, Any], *, request: Request) -> Response:
    export_payload = dict(payload.get("export_download") or {})
    fields = [str(field) for field in export_payload.get("fields") or []]
    rows = export_payload.get("rows") if isinstance(export_payload.get("rows"), list) else []
    set_pii_audit_result_count(request, len(rows))
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        row_payload = row if isinstance(row, dict) else {}
        writer.writerow({field: _csv_value(row_payload.get(field)) for field in fields})
    filename = str(export_payload.get("filename") or "questionnaire-submissions.csv").replace('"', "")
    content = "\ufeff" + buffer.getvalue()
    return Response(
        content=content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-AICRM-Route-Owner": "ai_crm_next",
            "X-AICRM-Source-Status": str(payload.get("source_status") or "next_command"),
            "X-AICRM-Fallback-Used": "false",
        },
    )


async def _execute_admin_write(
    request: Request,
    command_name: str,
    *,
    questionnaire_id: int | None = None,
    body: dict[str, Any] | None = None,
) -> Response:
    try:
        payload = body if body is not None else await _json_body(request)
        command = QuestionnaireAdminWriteCommand(
            command_name=command_name,
            questionnaire_id=questionnaire_id,
            payload={
                key: value for key, value in payload.items() if key not in {"actor_id", "actor_type", "idempotency_key", "dry_run", "trace_id", "command_id"}
            },
            command_id=str(payload.get("command_id") or uuid4().hex),
            idempotency_key=str(request.headers.get("Idempotency-Key") or payload.get("idempotency_key") or "").strip(),
            actor_id=str(payload.get("actor_id") or request.headers.get("X-AICRM-Actor-Id") or "questionnaire_admin"),
            actor_type=str(payload.get("actor_type") or request.headers.get("X-AICRM-Actor-Type") or "user"),
            dry_run=_as_bool(payload.get("dry_run")),
            source_route=request.url.path,
            trace_id=str(payload.get("trace_id") or request.headers.get("X-Request-Id") or uuid4().hex),
        )
        response = execute_questionnaire_admin_write(command)
        if command_name in {"questionnaire.admin.export_download", "questionnaire.admin.export_preview"}:
            set_pii_audit_result_count(request, infer_pii_result_count(response))
        return JSONResponse(jsonable_encoder(response), status_code=200)
    except QuestionnaireAdminWriteInputError as exc:
        return _write_error(str(exc), status_code=400, source_status="input_error", write_model_status="input_error")
    except QuestionnaireAdminWriteNotFoundError as exc:
        return _write_error(str(exc), status_code=404, source_status="not_found", write_model_status="not_found")
    except QuestionnaireAdminWriteProductionUnavailableError as exc:
        return _write_error(
            str(exc),
            status_code=503,
            source_status="production_unavailable",
            write_model_status="unavailable",
            degraded=True,
        )


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise QuestionnaireAdminWriteInputError("json object body is required")
    return payload


async def _h5_json_body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise QuestionnaireH5WriteInputError("json object body is required")
    return payload


def _as_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@router.get("/api/external/questionnaire-submissions")
def list_external_questionnaire_submissions(
    request: Request,
    mobile: str | None = Query(None, description="手机号"),
    unionid: str | None = Query(None, description="微信 unionid"),
    external_userid: str | None = Query(None, description="企业微信 external_userid"),
    questionnaire_id: int | None = Query(None, ge=1, description="问卷 ID"),
    submitted_from: int | None = Query(None, description="提交开始秒级 Unix 时间戳"),
    submitted_to: int | None = Query(None, description="提交结束秒级 Unix 时间戳"),
    limit: int = Query(100, ge=1, le=500, description="分页条数，最大 500"),
    cursor: str | None = Query(None, description="下一页游标"),
) -> JSONResponse:
    if not any(_external_text(value) for value in (mobile, unionid, external_userid)):
        return _external_error(
            error_code="invalid_request",
            message="one of mobile, unionid, or external_userid is required",
            status_code=400,
        )
    try:
        offset = _external_decode_cursor(cursor)
        submitted_from_filter = _external_timestamp_filter(submitted_from, "submitted_from")
        submitted_to_filter = _external_timestamp_filter(submitted_to, "submitted_to")
        if submitted_from_filter and submitted_to_filter and submitted_from_filter > submitted_to_filter:
            raise ValueError("submitted_from must be earlier than or equal to submitted_to")
        filters = _external_filters(
            mobile=mobile,
            unionid=unionid,
            external_userid=external_userid,
            questionnaire_id=questionnaire_id,
            submitted_from=submitted_from_filter,
            submitted_to=submitted_to_filter,
        )
        payload = ListExternalQuestionnaireSubmissionsQuery()(filters=filters, limit=limit, offset=offset)
    except ValueError as exc:
        return _external_error(error_code="invalid_request", message=str(exc), status_code=400)

    if payload.get("source_status") == "production_unavailable":
        return JSONResponse(jsonable_encoder(payload), status_code=503)
    items = list(payload.get("items") or [])
    total = int(payload.get("total") or 0)
    next_offset = offset + len(items) if offset + len(items) < total else None
    response_payload = {
        "ok": True,
        "items": items,
        "total": total,
        "limit": int(payload.get("limit") or limit),
        "next_cursor": _external_encode_cursor(next_offset),
        "has_more": next_offset is not None,
        "filters": payload.get("filters") or {},
        "route_owner": "ai_crm_next",
        "source_status": _EXTERNAL_SOURCE_STATUS,
        "read_model_status": payload.get("read_model_status") or "",
        "fallback_used": False,
    }
    return JSONResponse(jsonable_encoder(response_payload))


def _is_redirect_response_mode(response_mode: str | None, browser_redirect: Any = None) -> bool:
    mode = str(response_mode or "").strip().lower()
    return mode == "redirect" or _as_bool(browser_redirect)


def _safe_questionnaire_return_url(value: str | None, slug: str | None) -> str:
    target = str(value or "").strip()
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    slug_value = str(slug or "").strip()
    return f"/s/{slug_value}" if slug_value else "/"


def _safe_oauth_success_redirect_url(value: str | None, slug: str | None) -> str:
    target = str(value or "").strip()
    if target.startswith(("http://", "https://")):
        return target
    return _safe_questionnaire_return_url(target, slug)


def _oauth_html_error(
    *,
    title: str,
    message: str,
    return_url: str | None,
    slug: str | None = None,
    status_code: int = 400,
) -> HTMLResponse:
    from html import escape

    safe_return_url = _safe_questionnaire_return_url(return_url, slug)
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px; background: #f2f3f5; color: #1f2329; font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Helvetica Neue", sans-serif; }}
    main {{ width: min(100%, 420px); padding: 28px 22px; border: 1px solid #dee0e3; border-radius: 20px; background: #fff; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
    h1 {{ margin: 0 0 12px; font-size: 22px; line-height: 1.35; }}
    p {{ margin: 0; color: #646a73; font-size: 15px; line-height: 1.7; }}
    a {{ display: inline-flex; align-items: center; justify-content: center; margin-top: 22px; min-height: 44px; padding: 0 20px; border-radius: 999px; background: #3370ff; color: #fff; font-weight: 700; text-decoration: none; }}
  </style>
</head>
<body>
  <main>
    <h1>{escape(title)}</h1>
    <p>{escape(message)}</p>
    <a href="{escape(safe_return_url, quote=True)}">返回问卷</a>
  </main>
</body>
</html>"""
    return HTMLResponse(html, status_code=status_code)


def _write_error(
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


def _h5_write_error(
    message: str,
    *,
    status_code: int,
    source_status: str,
    write_model_status: str,
    degraded: bool = False,
    error_code: str | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": message,
            "error_code": error_code or source_status,
            "source_status": source_status,
            "write_model_status": write_model_status,
            "route_owner": "ai_crm_next",
            "fallback_used": False,
            "real_external_call_executed": False,
            "degraded": degraded,
            **(extra or {}),
        },
        status_code=status_code,
    )


def _h5_identity_payload(payload: dict[str, Any]) -> dict[str, Any]:
    identity: dict[str, Any] = {}
    for key in ["respondent_identity", "identity"]:
        value = payload.get(key)
        if isinstance(value, dict):
            identity.update(value)
    for key in ["external_userid", "openid", "unionid", "mobile", "respondent_key"]:
        if payload.get(key) not in (None, ""):
            identity[key] = payload.get(key)
    return identity


def _h5_submit_answers_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for field_name in ("answers", "answer_items", "responses"):
        if field_name not in payload:
            continue
        raw = payload.get(field_name)
        if isinstance(raw, dict):
            if not raw:
                raise QuestionnaireH5WriteInputError("answers is required")
            return dict(raw)
        if isinstance(raw, list):
            answers = _h5_answer_items_to_answers(raw)
            if not answers:
                raise QuestionnaireH5WriteInputError("answers is required")
            return answers
        raise QuestionnaireH5WriteInputError("answers must be an object")
    raise QuestionnaireH5WriteInputError("answers is required")


def _h5_answer_items_to_answers(items: list[Any]) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise QuestionnaireH5WriteInputError(f"answer item {index} must be an object")
        question_id = str(item.get("question_id") or item.get("question_key") or item.get("id") or item.get("key") or "").strip()
        if not question_id:
            raise QuestionnaireH5WriteInputError(f"answer item {index} question_id is required")
        value_missing = object()
        value = value_missing
        for value_key in ("value", "answer", "selected_option_ids", "option_ids", "text_value"):
            if value_key in item:
                value = item.get(value_key)
                break
        if value is value_missing:
            raise QuestionnaireH5WriteInputError(f"answer item {index} value is required")
        answers[question_id] = value
    return answers


def _h5_source_payload(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    source = dict(payload.get("source") or {}) if isinstance(payload.get("source"), dict) else {}
    for key in ["source_channel", "campaign_id", "staff_id"]:
        value = request.query_params.get(key) or payload.get(key)
        if value not in (None, ""):
            source[key] = value
    source.setdefault("route", request.url.path)
    return source


async def _execute_h5_submit(request: Request, slug: str) -> Response:
    try:
        payload = await _h5_json_body(request)
        access = _questionnaire_access_decision(request, slug)
        if not access.allowed:
            return JSONResponse(
                {
                    **access.payload(),
                    "redirect_url": access.oauth_start_url,
                    "source_status": "oauth_required" if access.oauth_start_url else "wechat_browser_required",
                    "write_model_status": "blocked",
                    "route_owner": "ai_crm_next",
                    "fallback_used": False,
                    "real_external_call_executed": False,
                    "degraded": False,
                },
                status_code=access.status_code,
            )
        identity_result = _questionnaire_identity_result_from_request(request)
        request_identity = dict(identity_result.get("identity") or {})
        submit_identity = dict(request_identity)
        submission_identity_result = QuestionnaireRespondentIdentityService().resolve(
            cookies=request.cookies,
            request_identity=submit_identity,
            slug=slug,
        )
        command = QuestionnaireH5SubmitCommand(
            questionnaire_slug=slug,
            answers=_h5_submit_answers_payload(payload),
            identity=submit_identity,
            source=_h5_source_payload(payload, request),
            command_id=str(payload.get("command_id") or uuid4().hex),
            idempotency_key=str(request.headers.get("Idempotency-Key") or payload.get("idempotency_key") or "").strip(),
            actor_id=str(payload.get("actor_id") or request.headers.get("X-AICRM-Actor-Id") or "anonymous"),
            actor_type=str(payload.get("actor_type") or request.headers.get("X-AICRM-Actor-Type") or "h5_client"),
            dry_run=_as_bool(payload.get("dry_run")),
            source_route=request.url.path,
            trace_id=str(payload.get("trace_id") or request.headers.get("X-Request-Id") or uuid4().hex),
        )
        payload_response = execute_questionnaire_h5_submit(command)
        public_payload = dict(payload_response)
        public_payload.pop("result_access_token", None)
        response = JSONResponse(jsonable_encoder(public_payload), status_code=200)
        result_access_token = str(payload_response.get("result_access_token") or "").strip()
        if result_access_token:
            grant = issue_questionnaire_result_grant(
                slug=slug,
                result_access_token=result_access_token,
            )
            response.set_cookie(
                grant.cookie_name,
                grant.cookie_value,
                httponly=True,
                secure=session_cookie_secure(),
                samesite="lax",
                max_age=grant.max_age_seconds,
                path=grant.cookie_path,
            )
        return _with_identity_cookie(response, submission_identity_result)
    except QuestionnaireH5WriteInputError as exc:
        if str(exc).strip() == "identity_conflict":
            return _h5_write_error(
                _IDENTITY_CONFLICT_PUBLIC_MESSAGE,
                status_code=409,
                source_status="conflict",
                write_model_status="blocked",
                error_code="identity_conflict",
            )
        return _h5_write_error(
            str(exc),
            status_code=400,
            source_status="input_error",
            write_model_status="input_error",
            error_code="invalid_questionnaire_submission",
        )
    except QuestionnaireH5AlreadySubmittedError as exc:
        status_payload: dict[str, Any] = {}
        try:
            identity_result = _questionnaire_identity_result_from_request(request)
            status_payload = GetPublicQuestionnaireSubmissionStatusQuery()(slug, identity=identity_result["identity"])
        except Exception:
            status_payload = {}
        return _h5_write_error(
            str(exc),
            status_code=409,
            source_status="already_submitted",
            write_model_status="already_submitted",
            extra={
                "redirect_url": status_payload.get("redirect_url") or status_payload.get("submitted_url"),
                "completion_target": status_payload.get("completion_target"),
                "completion_target_enabled": status_payload.get("completion_target_enabled"),
                "completion_target_type": status_payload.get("completion_target_type"),
                "completion_action": status_payload.get("completion_action"),
                "lead_qr": status_payload.get("lead_qr"),
            },
        )
    except QuestionnaireH5WriteNotFoundError as exc:
        return _h5_write_error(str(exc), status_code=404, source_status="not_found", write_model_status="not_found")
    except QuestionnaireH5WriteProductionUnavailableError as exc:
        return _h5_write_error(
            str(exc),
            status_code=503,
            source_status="production_unavailable",
            write_model_status="unavailable",
            degraded=True,
        )


async def _execute_h5_diagnostics(request: Request, slug: str) -> Response:
    try:
        payload = await _h5_json_body(request)
        diagnostics = dict(payload.get("diagnostics") or payload)
        for key in ["identity", "respondent_identity", "source", "command_id", "idempotency_key", "actor_id", "actor_type", "dry_run", "trace_id"]:
            diagnostics.pop(key, None)
        command = QuestionnaireClientDiagnosticsCommand(
            questionnaire_slug=slug,
            diagnostics=diagnostics,
            identity=_h5_identity_payload(payload),
            source=_h5_source_payload(payload, request),
            command_id=str(payload.get("command_id") or uuid4().hex),
            idempotency_key=str(request.headers.get("Idempotency-Key") or payload.get("idempotency_key") or "").strip(),
            actor_id=str(payload.get("actor_id") or request.headers.get("X-AICRM-Actor-Id") or "anonymous"),
            actor_type=str(payload.get("actor_type") or request.headers.get("X-AICRM-Actor-Type") or "h5_client"),
            dry_run=_as_bool(payload.get("dry_run")),
            source_route=request.url.path,
            trace_id=str(payload.get("trace_id") or request.headers.get("X-Request-Id") or uuid4().hex),
        )
        response = execute_questionnaire_client_diagnostics(command)
        return JSONResponse(jsonable_encoder(response), status_code=200)
    except QuestionnaireH5WriteInputError as exc:
        return _h5_write_error(str(exc), status_code=400, source_status="input_error", write_model_status="input_error")
    except QuestionnaireH5WriteProductionUnavailableError as exc:
        return _h5_write_error(
            str(exc),
            status_code=503,
            source_status="production_unavailable",
            write_model_status="unavailable",
            degraded=True,
        )


def _h5_options_response() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "source_status": "next_command",
            "route_owner": "ai_crm_next",
            "fallback_used": False,
            "real_external_call_executed": False,
        }
    )


def _oauth_options_response() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "source_status": "next_oauth_adapter",
            "adapter_mode": "real_blocked" if production_data_ready() else "fake",
            "route_owner": "ai_crm_next",
            "fallback_used": False,
            "real_external_call_executed": False,
        }
    )


def _questionnaire_payload_with_nested_questions(payload: dict[str, Any]) -> dict[str, Any]:
    questionnaire = payload.get("questionnaire")
    questions = payload.get("questions")
    if not isinstance(questionnaire, dict) or not isinstance(questions, list):
        return payload
    if isinstance(questionnaire.get("questions"), list):
        return payload
    return {**payload, "questionnaire": {**questionnaire, "questions": questions}}


def _public_questionnaire_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = _questionnaire_payload_with_nested_questions(payload)
    questions = normalized.get("questions")
    if not isinstance(questions, list):
        return normalized
    public_questions = [
        {key: value for key, value in question.items() if key != "sidebar_profile_field"} if isinstance(question, dict) else question for question in questions
    ]
    questionnaire = normalized.get("questionnaire")
    if isinstance(questionnaire, dict):
        questionnaire = {
            **questionnaire,
            "questions": [
                {key: value for key, value in question.items() if key != "sidebar_profile_field"} if isinstance(question, dict) else question
                for question in questionnaire.get("questions", [])
            ],
        }
    return {**normalized, "questionnaire": questionnaire, "questions": public_questions}


def _is_wechat_browser(request: Request) -> bool:
    return "micromessenger" in str(request.headers.get("user-agent") or "").lower()


def _request_values(request: Request, fields: tuple[str, ...]) -> dict[str, str]:
    payload: dict[str, str] = {}
    for key in fields:
        value = str(request.query_params.get(key) or "").strip()
        if value:
            payload[key] = value
    return payload


def _questionnaire_oauth_start_url(slug: str, source_params: dict[str, str]) -> str:
    slug_value = str(slug or "").strip()
    return_url = f"/s/{slug_value}"
    if source_params:
        return_url = f"{return_url}?{urlencode(source_params)}"
    return "/api/h5/wechat/oauth/start?" + urlencode(
        {
            "slug": slug_value,
            "redirect": return_url,
            "response_mode": "redirect",
            **source_params,
        }
    )


def _questionnaire_access_decision(request: Request, slug: str):
    identity = questionnaire_h5_identity_from_cookies(request.cookies) or payment_identity_from_request(request)
    return evaluate_wechat_unionid_access(
        identity,
        is_wechat_browser=_is_wechat_browser(request),
        oauth_start_url=_questionnaire_oauth_start_url(
            slug,
            _request_values(request, _QUESTIONNAIRE_SOURCE_PARAM_FIELDS),
        ),
    )


def _questionnaire_identity_result_from_request(request: Request) -> dict[str, Any]:
    return QuestionnaireRespondentIdentityService().resolve(
        cookies=request.cookies,
        request_identity=payment_identity_from_request(request),
        slug=str(request.path_params.get("slug") or request.query_params.get("slug") or ""),
    )


def _with_identity_cookie(response: Response, identity_result: dict[str, Any]) -> Response:
    cookie_value = str(identity_result.get("cookie_value") or "")
    if cookie_value:
        response.set_cookie(
            str(identity_result.get("cookie_name") or COOKIE_NAME),
            cookie_value,
            httponly=True,
            secure=session_cookie_secure(),
            samesite="lax",
            max_age=60 * 60 * 24 * 365,
            path="/",
        )
    return response


def _questionnaire_share_url(request: Request, questionnaire: dict[str, Any]) -> str:
    public_url = str(questionnaire.get("public_url") or "").strip()
    if public_url.startswith(("http://", "https://")):
        return public_url
    public_path = str(questionnaire.get("public_path") or public_url or "").strip()
    if not public_path:
        slug = str(questionnaire.get("slug") or "").strip()
        public_path = f"/s/{slug}" if slug else ""
    if not public_path:
        return ""
    if not public_path.startswith("/"):
        public_path = f"/{public_path}"
    return f"{str(request.base_url).rstrip('/')}{public_path}"


@router.get("/api/admin/questionnaires", response_model=None)
def list_questionnaires(limit: int = 50, offset: int = 0) -> Any:
    return _read_response(ListQuestionnairesQuery()(limit=limit, offset=offset))


@router.get("/api/admin/questionnaires/preflight", response_model=None)
async def questionnaire_preflight(request: Request) -> dict | Response:
    return GetQuestionnairePreflightQuery()()


@router.post("/api/admin/questionnaires")
async def create_questionnaire(request: Request) -> Response:
    return await _execute_admin_write(request, "questionnaire.admin.create")


@router.get("/api/admin/questionnaires/{questionnaire_id}/operations", response_model=None)
def get_questionnaire_operations(questionnaire_id: int) -> Response | dict[str, Any]:
    try:
        return QuestionnaireOperationsService().get_operations(questionnaire_id)
    except (
        QuestionnaireOperationsConflictError,
        QuestionnaireOperationsInputError,
        QuestionnaireOperationsNotFoundError,
        QuestionnaireOperationsUnavailableError,
    ) as exc:
        return _operations_error(exc)


@router.put("/api/admin/questionnaires/{questionnaire_id}/operations/completion", response_model=None)
async def save_questionnaire_completion_operations(questionnaire_id: int, request: Request) -> Response | dict[str, Any]:
    try:
        return QuestionnaireOperationsService().save_completion(questionnaire_id, await _operations_body(request))
    except (
        QuestionnaireOperationsConflictError,
        QuestionnaireOperationsInputError,
        QuestionnaireOperationsNotFoundError,
        QuestionnaireOperationsUnavailableError,
    ) as exc:
        return _operations_error(exc)


@router.put("/api/admin/questionnaires/{questionnaire_id}/operations/external-push", response_model=None)
async def save_questionnaire_external_push_operations(questionnaire_id: int, request: Request) -> Response | dict[str, Any]:
    try:
        return QuestionnaireOperationsService().save_external_push(questionnaire_id, await _operations_body(request))
    except (
        QuestionnaireOperationsConflictError,
        QuestionnaireOperationsInputError,
        QuestionnaireOperationsNotFoundError,
        QuestionnaireOperationsUnavailableError,
    ) as exc:
        return _operations_error(exc)


@router.post("/api/admin/questionnaires/{questionnaire_id}/operations/external-push/test", response_model=None)
def test_questionnaire_external_push_operations(questionnaire_id: int) -> Response | dict[str, Any]:
    try:
        return QuestionnaireOperationsService().queue_external_push_test(questionnaire_id)
    except (
        QuestionnaireOperationsConflictError,
        QuestionnaireOperationsInputError,
        QuestionnaireOperationsNotFoundError,
        QuestionnaireOperationsUnavailableError,
    ) as exc:
        return _operations_error(exc)


@router.get("/api/admin/questionnaires/{questionnaire_id}", response_model=None)
def get_questionnaire(questionnaire_id: int) -> Any:
    try:
        return _read_response(_questionnaire_payload_with_nested_questions(GetQuestionnaireDetailQuery()(questionnaire_id)))
    except Exception as exc:
        _raise_http(exc)


@router.get("/api/admin/questionnaires/{questionnaire_id}/questions", response_model=None)
def get_questionnaire_questions(questionnaire_id: int) -> Any:
    try:
        return _read_response(ListQuestionnaireQuestionsQuery()(questionnaire_id))
    except Exception as exc:
        _raise_http(exc)


@router.get("/api/admin/questionnaires/{questionnaire_id}/results", response_model=None)
def get_questionnaire_results(questionnaire_id: int) -> Any:
    try:
        return _read_response(GetQuestionnaireResultsSummaryQuery()(questionnaire_id))
    except Exception as exc:
        _raise_http(exc)


@router.get("/api/admin/questionnaires/{questionnaire_id}/submissions", response_model=None)
def get_questionnaire_submissions(questionnaire_id: int, limit: int = 20, offset: int = 0) -> Any:
    try:
        return _read_response(ListQuestionnaireSubmissionsQuery()(questionnaire_id, limit=limit, offset=offset))
    except Exception as exc:
        _raise_http(exc)


@router.get("/api/admin/questionnaires/{questionnaire_id}/share")
def get_questionnaire_share(questionnaire_id: int, request: Request) -> dict:
    try:
        detail = GetQuestionnaireDetailQuery()(questionnaire_id)
        questionnaire = detail["questionnaire"]
        return GetQuestionnaireShareQuery()(
            questionnaire_id,
            share_url=_questionnaire_share_url(request, questionnaire),
        )
    except Exception as exc:
        _raise_http(exc)


@router.put("/api/admin/questionnaires/{questionnaire_id}")
@router.patch("/api/admin/questionnaires/{questionnaire_id}")
async def update_questionnaire(questionnaire_id: int, request: Request) -> Response:
    return await _execute_admin_write(request, "questionnaire.admin.update", questionnaire_id=questionnaire_id)


@router.post("/api/admin/questionnaires/{questionnaire_id}/duplicate")
async def duplicate_questionnaire(questionnaire_id: int, request: Request) -> Response:
    return await _execute_admin_write(request, "questionnaire.admin.duplicate", questionnaire_id=questionnaire_id)


@router.post("/api/admin/questionnaires/{questionnaire_id}/publish")
async def publish_questionnaire(questionnaire_id: int, request: Request) -> Response:
    return await _execute_admin_write(request, "questionnaire.admin.publish", questionnaire_id=questionnaire_id)


@router.post("/api/admin/questionnaires/{questionnaire_id}/disable")
async def disable_questionnaire(questionnaire_id: int, request: Request) -> Response:
    body = await _json_body(request)
    enabled = not bool(body.get("is_disabled", True))
    command_name = "questionnaire.admin.enable" if enabled else "questionnaire.admin.disable"
    return await _execute_admin_write(request, command_name, questionnaire_id=questionnaire_id, body=body)


@router.post("/api/admin/questionnaires/{questionnaire_id}/enable")
async def enable_questionnaire(questionnaire_id: int, request: Request) -> Response:
    return await _execute_admin_write(request, "questionnaire.admin.enable", questionnaire_id=questionnaire_id)


@router.delete("/api/admin/questionnaires/{questionnaire_id}")
async def delete_questionnaire(questionnaire_id: int, request: Request) -> Response:
    return await _execute_admin_write(request, "questionnaire.admin.delete", questionnaire_id=questionnaire_id)


@router.get("/api/admin/questionnaires/{questionnaire_id}/export")
async def export_questionnaire(questionnaire_id: int, request: Request) -> Response:
    try:
        command = QuestionnaireAdminWriteCommand(
            command_name="questionnaire.admin.export_download",
            questionnaire_id=questionnaire_id,
            payload={"limit": 10000},
            idempotency_key=str(request.headers.get("Idempotency-Key") or request.query_params.get("idempotency_key") or "").strip(),
            actor_id=str(request.headers.get("X-AICRM-Actor-Id") or "questionnaire_admin"),
            actor_type=str(request.headers.get("X-AICRM-Actor-Type") or "user"),
            source_route=request.url.path,
            trace_id=str(request.headers.get("X-Request-Id") or uuid4().hex),
        )
        response = execute_questionnaire_admin_write(command)
        return _csv_download_response(response, request=request)
    except QuestionnaireAdminWriteInputError as exc:
        return _write_error(str(exc), status_code=400, source_status="input_error", write_model_status="input_error")
    except QuestionnaireAdminWriteNotFoundError as exc:
        return _write_error(str(exc), status_code=404, source_status="not_found", write_model_status="not_found")
    except QuestionnaireAdminWriteProductionUnavailableError as exc:
        return _write_error(
            str(exc),
            status_code=503,
            source_status="production_unavailable",
            write_model_status="unavailable",
            degraded=True,
        )


@router.post("/api/admin/questionnaires/{questionnaire_id}/export/preview")
async def export_questionnaire_preview(questionnaire_id: int, request: Request) -> Response:
    return await _execute_admin_write(request, "questionnaire.admin.export_preview", questionnaire_id=questionnaire_id)


@router.get("/api/admin/questionnaires/{questionnaire_id}/latest-submit-debug")
def latest_submit_debug(questionnaire_id: int) -> dict:
    try:
        return LatestSubmitDebugQuery()(questionnaire_id)
    except Exception as exc:
        _raise_http(exc)


@router.get("/api/h5/questionnaires/{slug}")
def public_get_questionnaire(request: Request, slug: str) -> Response:
    try:
        access = _questionnaire_access_decision(request, slug)
        if not access.allowed:
            return JSONResponse(
                {**access.payload(), "redirect_url": access.oauth_start_url},
                status_code=access.status_code,
            )
        identity_result = _questionnaire_identity_result_from_request(request)
        submission_status = GetPublicQuestionnaireSubmissionStatusQuery()(slug, identity=identity_result["identity"])
        if submission_status.get("submitted"):
            return _with_identity_cookie(
                JSONResponse(
                    {
                        "ok": False,
                        "error": "already_submitted",
                        "message": "已经提交过该问卷",
                        "redirect_url": submission_status.get("redirect_url") or submission_status.get("submitted_url"),
                        "completion_target": submission_status.get("completion_target"),
                        "completion_target_enabled": submission_status.get("completion_target_enabled"),
                        "completion_target_type": submission_status.get("completion_target_type"),
                        "completion_action": submission_status.get("completion_action"),
                        "lead_qr": submission_status.get("lead_qr"),
                    },
                    status_code=409,
                ),
                identity_result,
            )
        return _with_identity_cookie(JSONResponse(jsonable_encoder(GetPublicQuestionnaireQuery()(slug))), identity_result)
    except Exception as exc:
        _raise_http(exc)


@router.post("/api/h5/questionnaires/{slug}/submit")
async def public_submit_questionnaire(slug: str, request: Request) -> Response:
    return await _execute_h5_submit(request, slug)


@router.options("/api/h5/questionnaires/{slug}/submit")
def public_submit_questionnaire_options(slug: str) -> Response:
    return _h5_options_response()


@router.post("/api/h5/questionnaires/{slug}/client-diagnostics")
async def public_questionnaire_client_diagnostics(slug: str, request: Request) -> Response:
    return await _execute_h5_diagnostics(request, slug)


@router.options("/api/h5/questionnaires/{slug}/client-diagnostics")
def public_questionnaire_client_diagnostics_options(slug: str) -> Response:
    return _h5_options_response()


@router.get("/api/h5/questionnaires/{slug}/result")
def public_submission_result(request: Request, slug: str) -> dict:
    access = _questionnaire_access_decision(request, slug)
    if not access.allowed:
        return JSONResponse(
            {**access.payload(), "redirect_url": access.oauth_start_url},
            status_code=access.status_code,
        )
    submission_id = str(getattr(request.state, "questionnaire_result_access_token", "") or "").strip()
    if not submission_id:
        set_pii_audit_result_count(request, 0)
        return JSONResponse(
            {
                "ok": False,
                "error_code": "questionnaire_result_access_forbidden",
                "message": "questionnaire result access forbidden",
                "route_owner": "ai_crm_next",
                "source_status": "access_forbidden",
                "fallback_used": False,
            },
            status_code=403,
        )
    try:
        return GetSubmissionResultQuery()(slug, submission_id)
    except Exception as exc:
        _raise_http(exc)


@router.get("/api/h5/wechat/oauth/start", response_model=None)
def wechat_oauth_start(
    slug: str | None = None,
    state: str | None = None,
    redirect: str | None = None,
    scene: str | None = None,
    response_mode: str | None = None,
    browser_redirect: str | None = None,
    source_channel: str | None = None,
    campaign_id: str | None = None,
    staff_id: str | None = None,
    openid: str | None = None,
    unionid: str | None = None,
    external_userid: str | None = None,
) -> Any:
    browser_mode = _is_redirect_response_mode(response_mode, browser_redirect)
    payload = StartWechatOAuthQuery()(
        OAuthStartRequest(
            slug=slug,
            state=state,
            redirect=redirect,
            scene=scene,
            browser_redirect=browser_mode,
            source_channel=source_channel,
            campaign_id=campaign_id,
            staff_id=staff_id,
            openid=openid,
            unionid=unionid,
            external_userid=external_userid,
        )
    )
    if not browser_mode:
        return payload
    if payload.get("ok") and payload.get("redirect_url") and payload.get("adapter_mode") != "real_blocked" and not payload.get("external_call_blocked"):
        return RedirectResponse(str(payload["redirect_url"]), status_code=302)
    logger.warning(
        "questionnaire oauth start browser redirect unavailable",
        extra=safe_log_fields(
            slug=slug,
            adapter_mode=payload.get("adapter_mode"),
            error=payload.get("error"),
            source_status=payload.get("source_status"),
        ),
    )
    return _oauth_html_error(
        title="当前微信授权配置未完成",
        message="当前微信授权配置未完成，请联系管理员。",
        return_url=redirect,
        slug=slug or state,
        status_code=503,
    )


@router.options("/api/h5/wechat/oauth/start")
def wechat_oauth_start_options() -> Response:
    return _oauth_options_response()


@router.get("/api/h5/wechat/oauth/callback")
def wechat_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    redirect: str | None = None,
    response_mode: str | None = None,
    browser_redirect: str | None = None,
    error: str | None = None,
    errcode: str | None = None,
    openid: str | None = None,
    unionid: str | None = None,
    external_userid: str | None = None,
) -> Response:
    state_context = questionnaire_oauth_state_context(state)
    browser_mode = _is_redirect_response_mode(response_mode, browser_redirect) or bool(state_context.get("browser_redirect"))
    payload = CompleteWechatOAuthCallbackCommand()(
        OAuthCallbackRequest(
            code=code,
            state=state,
            redirect=redirect,
            error=error,
            errcode=errcode,
            openid=openid,
            unionid=unionid,
            external_userid=external_userid,
        )
    )
    signed_cookie = str(payload.pop("session_cookie", "") or "")
    status_code = int(payload.pop("status_code", 200 if payload.get("ok") else 400) or 400)
    if browser_mode:
        if payload.get("ok"):
            redirect_target = str(payload.get("redirect_url") or state_context.get("redirect_url") or redirect or "")
            redirect_response = RedirectResponse(
                _safe_oauth_success_redirect_url(redirect_target, str(payload.get("slug") or state_context.get("slug") or "")),
                status_code=302,
            )
            if signed_cookie:
                redirect_response.set_cookie(
                    COOKIE_NAME,
                    signed_cookie,
                    httponly=True,
                    secure=session_cookie_secure(),
                    samesite="lax",
                    max_age=3600,
                    path="/",
                )
            return redirect_response
        logger.warning(
            "questionnaire oauth callback browser redirect failed",
            extra=safe_log_fields(
                slug=payload.get("slug") or state_context.get("slug"),
                adapter_mode=payload.get("adapter_mode"),
                error=payload.get("error"),
                source_status=payload.get("source_status"),
            ),
        )
        return _oauth_html_error(
            title="授权未完成",
            message="授权未完成，请重新进入问卷。",
            return_url=str(state_context.get("redirect_url") or redirect or ""),
            slug=str(payload.get("slug") or state_context.get("slug") or ""),
            status_code=status_code,
        )
    json_response = JSONResponse(jsonable_encoder(payload), status_code=status_code)
    if payload.get("ok") and signed_cookie:
        json_response.set_cookie(
            COOKIE_NAME,
            signed_cookie,
            httponly=True,
            secure=session_cookie_secure(),
            samesite="lax",
            max_age=3600,
            path="/",
        )
    return json_response


@router.options("/api/h5/wechat/oauth/callback")
def wechat_oauth_callback_options() -> Response:
    return _oauth_options_response()


@router.get("/s/{slug}", response_class=HTMLResponse)
def public_questionnaire_h5_page(request: Request, slug: str):
    try:
        payload = GetPublicQuestionnaireQuery()(slug)
    except Exception as exc:
        _raise_http(exc)
    questionnaire = jsonable_encoder(payload["questionnaire"])
    questions = jsonable_encoder(payload.get("questions") or [])
    request_hints = _request_values(request, _QUESTIONNAIRE_META_FIELDS)
    identity_result = _questionnaire_identity_result_from_request(request)
    identity = dict(identity_result.get("identity") or {})
    is_wechat_browser = _is_wechat_browser(request)
    access = _questionnaire_access_decision(request, slug)
    is_authorized = access.allowed
    should_require_oauth = not access.allowed
    if is_wechat_browser and should_require_oauth and access.oauth_start_url:
        return RedirectResponse(access.oauth_start_url, status_code=302)
    submission_status = GetPublicQuestionnaireSubmissionStatusQuery()(slug, identity=identity)
    if submission_status.get("submitted") and not should_require_oauth:
        redirect_url = (
            _completion_target_redirect_url(
                slug,
                submission_status.get("completion_target"),
                fallback_url=submission_status.get("redirect_url") or "",
            )
            or str(submission_status.get("redirect_url") or submission_status.get("submitted_url") or f"/s/{slug}/submitted").strip()
        )
        return _with_identity_cookie(RedirectResponse(redirect_url, status_code=302), identity_result)
    page_mode = "auth_gate" if should_require_oauth else "questionnaire"
    env_notice = ""
    if page_mode == "auth_gate":
        env_notice = access.message
    page_state = {
        "mode": page_mode,
        "slug": slug,
        "title": questionnaire["title"],
        "description": questionnaire.get("description") or "",
        "env_notice": env_notice,
        "oauth_start_url": access.oauth_start_url,
        "submit_url": f"/api/h5/questionnaires/{slug}/submit",
        "api_url": f"/api/h5/questionnaires/{slug}",
        "diagnostics_url": f"/api/h5/questionnaires/{slug}/client-diagnostics",
        "submitted_url": f"/s/{slug}/submitted",
        "completion_target": questionnaire.get("completion_target") or {},
        "completion_target_enabled": bool(questionnaire.get("completion_target_enabled")),
        "completion_target_type": questionnaire.get("completion_target_type") or "h5",
        "request_hints": request_hints,
        "initial_questionnaire": {**questionnaire, "questions": questions} if page_mode == "questionnaire" else None,
        "answer_display_mode": questionnaire.get("answer_display_mode") or "all_in_one",
        "prefill_fields": {},
        "form_error": "",
        "is_wechat_browser": is_wechat_browser,
        "is_authorized": is_authorized,
    }
    response = templates.TemplateResponse(
        request,
        "questionnaire_h5_page.html",
        {"request": request, "page_state": page_state},
    )
    return _with_identity_cookie(response, identity_result)


@router.get("/s/{slug}/submitted", response_class=HTMLResponse)
def public_questionnaire_submitted(request: Request, slug: str):
    try:
        payload = GetPublicQuestionnaireQuery()(slug)
    except Exception as exc:
        _raise_http(exc)
    access = _questionnaire_access_decision(request, slug)
    if not access.allowed:
        if access.oauth_start_url:
            return RedirectResponse(access.oauth_start_url, status_code=302)
        return wechat_identity_failure_response(
            title="请在微信中打开",
            message=access.message,
            return_url=f"/s/{slug}",
            status_code=access.status_code,
        )
    questionnaire = jsonable_encoder(payload["questionnaire"])
    identity_result = _questionnaire_identity_result_from_request(request)
    submission_status = GetPublicQuestionnaireSubmissionStatusQuery()(
        slug,
        identity=dict(identity_result.get("identity") or {}),
    )
    completion_source = submission_status if submission_status.get("submitted") else questionnaire
    redirect_url = _completion_target_redirect_url(
        slug,
        completion_source.get("completion_target"),
        fallback_url=completion_source.get("redirect_url") or "",
    )
    if redirect_url:
        return _with_identity_cookie(RedirectResponse(redirect_url, status_code=302), identity_result)
    lead_qr = submission_status.get("lead_qr") if submission_status.get("submitted") else None
    response = templates.TemplateResponse(
        request,
        "questionnaire_h5_submitted.html",
        {
            "request": request,
            "lead_qr": lead_qr,
            "submitted": bool(submission_status.get("submitted")),
        },
    )
    return _with_identity_cookie(response, identity_result)
