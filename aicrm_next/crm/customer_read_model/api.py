from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from aicrm_next.platform.shared.config import get_settings
from aicrm_next.platform.shared.db_session import get_db
from aicrm_next.platform.shared.errors import NotFoundError
from aicrm_next.platform.shared.resource_admission import (
    ResourceCapacityExhausted,
    media_binary_admission,
)
from aicrm_next.platform.shared.runtime import database_mode, production_data_ready
from aicrm_next.platform.shared.runtime_settings import managed_runtime_setting, startup_environment_setting
from aicrm_next.platform.shared.sidebar_access import sidebar_owner_context_from_request as _signed_sidebar_owner_context_from_request
from aicrm_next.crm.identity_contact.repo import FixtureIdentityRepository, PostgresIdentityRepository
from . import application as customer_application
from .extension_port import UpdateServicePeriodMemberRemarkCommand, generate_missing_image_variants
from .application import (
    GetAdminCustomerProfileQuery,
    GetAdminCustomerProfileTagsQuery,
    GetCustomer360ProfileQuery,
    GetCustomerContextQuery,
    GetCustomerDetailQuery,
    GetCustomerTimelineQuery,
    ListCustomersQuery,
    ListRecentMessagesQuery,
)
from .errors import CustomerScopeForbiddenError
from .repo import CustomerReadRepository
from .admin_business_profile import get_customer_business_profile
from .dto import (
    CustomerContextRequest,
    CustomerDetailRequest,
    CustomerTimelineRequest,
    ListCustomersRequest,
    RecentMessagesRequest,
)
from .sidebar_v2 import (
    SidebarCommerceReadModel,
    SidebarMaterialReadModel,
    SidebarOtherStaffMessagesReadModel,
    SidebarQuestionnaireReadModel,
    SidebarV2SqlRepository,
    SidebarWorkbenchReadModel,
    verify_sidebar_identity_snapshot_owner_scope,
)
from .sidebar_timeline import ResolvedSidebarCustomerContext, SidebarCustomerTimelineQuery

router = APIRouter()
_SQL_REPO_BACKENDS = {"sql", "sqlalchemy", "postgres", "postgresql"}


def _sidebar_owner_context_from_request(request: Request, **claims: Any) -> dict[str, Any]:
    context = _signed_sidebar_owner_context_from_request(request, **claims)
    repository = PostgresIdentityRepository() if database_mode() == "postgres" else FixtureIdentityRepository()
    if not repository.has_active_follow_relation(
        corp_id=str(context.get("corp_id") or managed_runtime_setting("WECOM_CORP_ID") or "").strip(),
        user_id=str(context.get("owner_userid") or "").strip(),
        external_userid=str(context.get("external_userid") or "").strip(),
    ):
        raise HTTPException(status_code=403, detail="sidebar customer scope forbidden")
    return context


def _customer_read_model_sql_backend_enabled() -> bool:
    configured_backend = startup_environment_setting(
        "CUSTOMER_READ_MODEL_REPO_BACKEND"
    ).strip().lower()
    backend = configured_backend or get_settings().customer_read_model_repo_backend.strip().lower()
    if not configured_backend and database_mode() == "postgres":
        backend = "sqlalchemy"
    return backend in _SQL_REPO_BACKENDS


def _request_scoped_customer_repositories(db: Session) -> tuple[CustomerReadRepository | None, CustomerReadRepository | None]:
    if not _customer_read_model_sql_backend_enabled():
        return None, None
    return (
        customer_application.build_customer_read_model_repository(session=db),
        customer_application.build_customer_live_source_repository(session=db),
    )


def _request_scoped_customer_context_query(db: Session) -> tuple[GetCustomerContextQuery, CustomerReadRepository | None]:
    customer_repo, live_source_repo = _request_scoped_customer_repositories(db)
    return _sidebar_customer_context_query(customer_repo, live_source_repo), live_source_repo


def _sidebar_customer_context_query(
    customer_repo: CustomerReadRepository | None,
    live_source_repo: CustomerReadRepository | None,
) -> GetCustomerContextQuery:
    return GetCustomerContextQuery(
        customer_repo,
        live_source_repo=live_source_repo,
        owner_scope_verifier=lambda external_userid, owner_userid: verify_sidebar_identity_snapshot_owner_scope(
            external_userid=external_userid,
            owner_userid=owner_userid,
        ),
    )


def _input_error(message: str) -> JSONResponse:
    payload = {"ok": False, "error": message, "source_status": "input_error", "route_owner": "ai_crm_next"}
    return JSONResponse(jsonable_encoder(payload), status_code=400)


def _production_unavailable(exc: Exception) -> JSONResponse:
    payload = {
        "ok": False,
        "degraded": True,
        "source_status": "production_unavailable",
        "error_code": "customer_profile_read_unavailable",
        "page_error": str(exc),
        "route_owner": "ai_crm_next",
    }
    return JSONResponse(jsonable_encoder(payload), status_code=503)


def _sidebar_input_error(message: str) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": message,
            "source_status": "input_error",
            "read_model_status": "input_error",
            "route_owner": "ai_crm_next",
            "fallback_used": False,
            "degraded": False,
        },
        status_code=400,
    )


def _sidebar_lookup_error(message: str) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": message,
            "source_status": "not_found",
            "read_model_status": "not_found",
            "route_owner": "ai_crm_next",
            "fallback_used": False,
            "degraded": False,
        },
        status_code=404,
    )


def _sidebar_forbidden_error(message: str = "sidebar customer scope forbidden") -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": message,
            "source_status": "forbidden",
            "read_model_status": "blocked",
            "route_owner": "ai_crm_next",
            "fallback_used": False,
            "degraded": False,
        },
        status_code=403,
    )


def _sidebar_read_unavailable(exc: Exception) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "degraded": True,
            "source_status": "production_unavailable",
            "read_model_status": "unavailable",
            "error_code": "sidebar_read_unavailable",
            "page_error": str(exc),
            "route_owner": "ai_crm_next",
            "fallback_used": False,
        },
        status_code=503,
    )


def _resolve_external_userid(external_userid: str | None = None, user_id: str | None = None) -> str:
    return str(external_userid or user_id or "").strip()


def _profile_result_status(result: dict) -> int:
    return int(result.get("status_code", 200) or 200)


def _profile_external_userid(profile_result: dict) -> str:
    profile = dict(profile_result.get("profile") or profile_result.get("customer") or {})
    return str(profile.get("external_userid") or profile.get("user_id") or "").strip()


def _profile_unionid(profile_result: dict) -> str:
    profile = dict(profile_result.get("profile") or profile_result.get("customer") or {})
    return str(profile.get("unionid") or "").strip()


def _profile_questionnaire_answers(profile: dict) -> list[dict]:
    candidate_groups = [
        profile.get("matched_questions"),
        dict(profile.get("sidebar_context") or {}).get("matched_questions"),
        dict(profile.get("marketing_summary") or {}).get("matched_questions"),
    ]
    answers: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for group in candidate_groups:
        if not isinstance(group, list):
            continue
        for item in group:
            if not isinstance(item, dict):
                continue
            question = str(item.get("question") or item.get("title") or item.get("question_text") or item.get("label") or "").strip()
            answer = str(item.get("answer") or item.get("answer_text") or item.get("value") or item.get("text") or "").strip()
            if not question and not answer:
                continue
            key = (question, answer)
            if key in seen:
                continue
            seen.add(key)
            answers.append({"question": question or "未命名问题", "answer": answer or "未填写"})
    return answers


def _questionnaire_answer_text(row: dict) -> str:
    selected = row.get("selected_option_texts_snapshot")
    if isinstance(selected, list):
        answer = "、".join(str(item) for item in selected if str(item or "").strip())
    else:
        answer = str(selected or "").strip()
    detail = str(row.get("text_value") or "").strip()
    if answer and detail:
        return f"{answer}（{detail}）"
    return answer or detail


def _profile_questionnaire_answers_from_submissions(*, external_userid: str, mobile: str = "") -> list[dict]:
    if not external_userid and not mobile:
        return []
    try:
        rows = SidebarV2SqlRepository().list_questionnaire_answers(external_userid=external_userid, mobile=mobile)
    except Exception:
        return []
    answers: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        question = str(row.get("question") or "").strip() or "未命名问题"
        answer = _questionnaire_answer_text(row)
        if not answer:
            continue
        item = {
            "questionnaire_id": str(row.get("questionnaire_id") or ""),
            "questionnaire_title": str(row.get("questionnaire_title") or ""),
            "submission_id": str(row.get("submission_id") or ""),
            "submitted_at": str(row.get("submitted_at") or ""),
            "question_id": str(row.get("question_id") or ""),
            "question": question,
            "answer": answer,
        }
        key = (item["submission_id"], item["question_id"], item["question"])
        if key in seen:
            continue
        seen.add(key)
        answers.append(item)
    return answers


def _message_speaker(message: dict, customer: dict) -> str:
    sender = str(message.get("sender") or "").strip()
    external_userid = str(customer.get("external_userid") or customer.get("user_id") or "").strip()
    owner_userid = str(customer.get("owner_userid") or "").strip()
    customer_name = str(customer.get("customer_name") or external_userid or "客户").strip()
    owner_name = str(customer.get("owner_display_name") or owner_userid or "负责人").strip()
    if sender and sender == external_userid:
        return customer_name
    if sender and sender == owner_userid:
        return owner_name
    return sender or customer_name


def _context_for_external_userid(
    external_userid: str,
    *,
    owner_userid: str = "",
    require_owner_scope: bool = False,
    owner_verified: bool = False,
    recent_message_limit: int = 20,
    timeline_limit: int = 20,
    customer_repo: CustomerReadRepository | None = None,
    live_source_repo: CustomerReadRepository | None = None,
) -> dict:
    return _sidebar_customer_context_query(customer_repo, live_source_repo)(
        CustomerContextRequest(
            external_userid=external_userid,
            owner_userid=owner_userid or None,
            require_owner_scope=require_owner_scope,
            owner_verified=owner_verified,
            recent_message_limit=recent_message_limit,
            timeline_limit=timeline_limit,
        )
    )


def _status_code(payload: dict, default: int = 200) -> int:
    return int(payload.pop("status_code", default) or default)


def _sidebar_diagnostics_from_context(context: dict) -> dict:
    return {
        "source_status": context.get("source_status") or "",
        "read_model_status": context.get("read_model_status") or "",
        "route_owner": "ai_crm_next",
        "fallback_used": bool(context.get("fallback_used")),
        "degraded": bool(context.get("degraded")),
    }


def _sidebar_context_or_response(
    external_userid: str,
    *,
    owner_userid: str = "",
    owner_verified: bool = False,
    customer_repo: CustomerReadRepository | None = None,
    live_source_repo: CustomerReadRepository | None = None,
) -> tuple[dict | None, JSONResponse | None]:
    try:
        context = _context_for_external_userid(
            external_userid,
            owner_userid=owner_userid,
            require_owner_scope=True,
            owner_verified=owner_verified,
            customer_repo=customer_repo,
            live_source_repo=live_source_repo,
        )
    except CustomerScopeForbiddenError:
        return None, _sidebar_forbidden_error()
    except NotFoundError:
        return None, _sidebar_lookup_error("customer not found")
    except Exception as exc:
        return None, _sidebar_read_unavailable(exc)
    if not context.get("ok"):
        status_code = 503 if context.get("degraded") else 404 if context.get("source_status") == "not_found" else 400
        payload = dict(context)
        payload.setdefault("route_owner", "ai_crm_next")
        payload.setdefault("fallback_used", False)
        payload.setdefault("read_model_status", "unavailable" if payload.get("degraded") else payload.get("source_status") or "")
        return None, JSONResponse(jsonable_encoder(payload), status_code=status_code)
    return context, None


def _verify_sidebar_owner_scope(
    context_query: GetCustomerContextQuery,
    *,
    external_userid: str,
    owner_userid: str = "",
    corp_id: str = "",
    owner_verified: bool = False,
) -> ResolvedSidebarCustomerContext:
    if not str(owner_userid or "").strip():
        raise ValueError("owner_userid is required")
    try:
        payload = context_query(
            CustomerContextRequest(
                external_userid=external_userid,
                owner_userid=str(owner_userid or "").strip(),
                require_owner_scope=True,
                owner_verified=owner_verified,
                include_activity=False,
                recent_message_limit=1,
                timeline_limit=1,
            )
        )
        if isinstance(payload, dict) and payload.get("ok", True):
            customer = dict(payload.get("customer") or {})
            identity = dict(customer.get("identity") or {})
            canonical_external_userid = str(
                identity.get("external_userid")
                or customer.get("external_userid")
                or payload.get("external_userid")
                or external_userid
            ).strip()
            return ResolvedSidebarCustomerContext(
                external_userid=canonical_external_userid,
                unionid=str(payload.get("unionid") or "").strip(),
                owner_userid=str(owner_userid or "").strip(),
                corp_id=str(corp_id or "").strip(),
                owner_verified=owner_verified,
            )
    except NotFoundError:
        if not production_data_ready():
            raise
    if not production_data_ready():
        raise NotFoundError("customer not found")
    verify_sidebar_identity_snapshot_owner_scope(
        external_userid=external_userid,
        owner_userid=str(owner_userid or "").strip(),
        owner_verified=owner_verified,
    )
    return ResolvedSidebarCustomerContext(
        external_userid=str(external_userid or "").strip(),
        unionid="",
        owner_userid=str(owner_userid or "").strip(),
        corp_id=str(corp_id or "").strip(),
        owner_verified=owner_verified,
    )


async def _sidebar_json_body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("json object body is required")
    return payload


def _class_term_payload(class_user_status: dict, sidebar_context: dict) -> tuple[int | None, str]:
    raw_no = (
        sidebar_context.get("current_class_term_no")
        or sidebar_context.get("class_term_no")
        or class_user_status.get("class_term_no")
    )
    class_term_no: int | None = None
    if raw_no not in {None, ""}:
        try:
            class_term_no = int(raw_no)
        except (TypeError, ValueError):
            class_term_no = None
    label = str(
        sidebar_context.get("current_class_term_label")
        or sidebar_context.get("class_term_label")
        or class_user_status.get("class_term_label")
        or (f"{class_term_no}期" if class_term_no else "")
    )
    return class_term_no, label


def _display_status(value: str) -> str:
    mapping = {
        "activated": "已激活",
        "not_activated": "未激活",
        "pending_input": "待补录",
        "high_intent": "高意向",
        "medium": "中意向",
        "unknown": "未知",
        "trial": "试用",
        "new_user": "新用户",
        "followup": "复访",
        "lead": "线索",
        "converted": "已转化",
    }
    return mapping.get(value, value or "")


@router.get("/api/customers")
def list_customers(
    owner_userid: str | None = None,
    tag: str | None = None,
    status: str | None = None,
    is_bound: str | None = None,
    mobile: str | None = None,
    keyword: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> dict:
    customer_repo, live_source_repo = _request_scoped_customer_repositories(db)
    query = ListCustomersRequest(
        owner_userid=owner_userid,
        tag=tag,
        status=status,
        is_bound=is_bound,
        mobile=mobile,
        keyword=keyword,
        limit=limit,
        offset=offset,
    )
    result = ListCustomersQuery(customer_repo, live_source_repo=live_source_repo)(query)
    status_code = int(result.pop("status_code", 200) or 200)
    return JSONResponse(jsonable_encoder(result), status_code=status_code)


@router.get("/api/customers/{external_userid}")
def get_customer(external_userid: str, db: Session = Depends(get_db)) -> dict:
    customer_repo, live_source_repo = _request_scoped_customer_repositories(db)
    try:
        result = GetCustomerDetailQuery(customer_repo, live_source_repo=live_source_repo)(CustomerDetailRequest(external_userid=external_userid))
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    status_code = int(result.pop("status_code", 200) or 200)
    return JSONResponse(jsonable_encoder(result), status_code=status_code)


@router.get("/api/users/{unionid}")
def get_user_by_unionid(unionid: str, db: Session = Depends(get_db)) -> dict:
    customer_repo, live_source_repo = _request_scoped_customer_repositories(db)
    try:
        result = GetCustomerDetailQuery(customer_repo, live_source_repo=live_source_repo)(CustomerDetailRequest(unionid=unionid))
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    status_code = int(result.pop("status_code", 200) or 200)
    return JSONResponse(jsonable_encoder(result), status_code=status_code)


@router.get("/api/customers/{external_userid}/timeline")
def get_customer_timeline(
    external_userid: str,
    event_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> dict:
    customer_repo, live_source_repo = _request_scoped_customer_repositories(db)
    try:
        query = CustomerTimelineRequest(
            external_userid=external_userid,
            event_type=event_type,
            limit=limit,
            offset=offset,
        )
        result = GetCustomerTimelineQuery(customer_repo, live_source_repo=live_source_repo)(query)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    status_code = int(result.pop("status_code", 200) or 200)
    return JSONResponse(jsonable_encoder(result), status_code=status_code)


@router.get("/api/users/{unionid}/timeline")
def get_user_timeline_by_unionid(
    unionid: str,
    event_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> dict:
    customer_repo, live_source_repo = _request_scoped_customer_repositories(db)
    try:
        query = CustomerTimelineRequest(
            unionid=unionid,
            event_type=event_type,
            limit=limit,
            offset=offset,
        )
        result = GetCustomerTimelineQuery(customer_repo, live_source_repo=live_source_repo)(query)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    status_code = int(result.pop("status_code", 200) or 200)
    return JSONResponse(jsonable_encoder(result), status_code=status_code)


@router.get("/api/messages/{external_userid}/recent")
def get_recent_messages(external_userid: str, limit: int = 20, db: Session = Depends(get_db)) -> dict:
    customer_repo, live_source_repo = _request_scoped_customer_repositories(db)
    try:
        query = RecentMessagesRequest(external_userid=external_userid, limit=limit)
        result = ListRecentMessagesQuery(customer_repo, live_source_repo=live_source_repo)(query)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    status_code = int(result.pop("status_code", 200) or 200)
    return JSONResponse(jsonable_encoder(result), status_code=status_code)


@router.get("/api/users/{unionid}/messages/recent")
def get_user_recent_messages_by_unionid(unionid: str, limit: int = 20, db: Session = Depends(get_db)) -> dict:
    customer_repo, live_source_repo = _request_scoped_customer_repositories(db)
    try:
        query = RecentMessagesRequest(unionid=unionid, limit=limit)
        result = ListRecentMessagesQuery(customer_repo, live_source_repo=live_source_repo)(query)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    status_code = int(result.pop("status_code", 200) or 200)
    return JSONResponse(jsonable_encoder(result), status_code=status_code)


@router.get("/api/sidebar/customer-context")
def get_sidebar_customer_context(
    request: Request,
    external_userid: str | None = None,
    user_id: str | None = None,
    owner_userid: str | None = None,
    db: Session = Depends(get_db),
):
    customer_repo, live_source_repo = _request_scoped_customer_repositories(db)
    resolved_external_userid = _resolve_external_userid(external_userid, user_id)
    if not resolved_external_userid:
        return _input_error("external_userid is required")
    owner_context = _sidebar_owner_context_from_request(
        request,
        external_userid=resolved_external_userid,
        owner_userid=owner_userid,
    )
    try:
        context = _context_for_external_userid(
            resolved_external_userid,
            owner_userid=str(owner_context.get("owner_userid") or "").strip(),
            require_owner_scope=True,
            owner_verified=bool(owner_context.get("owner_verified")),
            customer_repo=customer_repo,
            live_source_repo=live_source_repo,
        )
        if not context.get("ok"):
            return JSONResponse(jsonable_encoder(context), status_code=503 if context.get("degraded") else 400)
        if not context.get("customer"):
            return _input_error("customer not found")
        return {
            "ok": True,
            "context": {
                "external_userid": context["external_userid"],
                "customer": context["customer"],
                "binding": context.get("binding") or {},
                "identity": context.get("identity") or {},
                "identity_binding_summary": context.get("identity_binding_summary") or {},
                "recent_messages": context.get("recent_messages") or [],
                "recent_timeline_events": context.get("recent_timeline_events") or [],
                "timeline": context.get("timeline") or {},
                "sidebar_context": context.get("customer", {}).get("sidebar_context") or {},
            },
            "source_status": context.get("source_status"),
            "read_model_status": context.get("read_model_status"),
            "degraded": bool(context.get("degraded")),
            "fallback_used": bool(context.get("fallback_used")),
            "page_error": context.get("page_error") or "",
            "route_owner": "ai_crm_next",
        }
    except CustomerScopeForbiddenError:
        return _sidebar_forbidden_error()
    except NotFoundError:
        return _sidebar_lookup_error("customer not found")
    except Exception as exc:
        return _production_unavailable(exc)


@router.get("/api/sidebar/profile")
def get_sidebar_profile(
    request: Request,
    external_userid: str | None = None,
    user_id: str | None = None,
    owner_userid: str | None = None,
    db: Session = Depends(get_db),
):
    customer_repo, live_source_repo = _request_scoped_customer_repositories(db)
    resolved_external_userid = _resolve_external_userid(external_userid, user_id)
    owner_context = _sidebar_owner_context_from_request(
        request,
        external_userid=resolved_external_userid,
        owner_userid=owner_userid,
    )
    try:
        result = GetAdminCustomerProfileQuery(_sidebar_customer_context_query(customer_repo, live_source_repo))(
            external_userid=resolved_external_userid,
            owner_userid=str(owner_context.get("owner_userid") or "").strip(),
            require_owner_scope=True,
            owner_verified=bool(owner_context.get("owner_verified")),
        )
    except CustomerScopeForbiddenError:
        return _sidebar_forbidden_error()
    return JSONResponse(jsonable_encoder(result), status_code=_status_code(result))


@router.get("/api/sidebar/tags")
def get_sidebar_tags(
    request: Request,
    external_userid: str | None = None,
    user_id: str | None = None,
    owner_userid: str | None = None,
    db: Session = Depends(get_db),
):
    customer_repo, live_source_repo = _request_scoped_customer_repositories(db)
    resolved_external_userid = _resolve_external_userid(external_userid, user_id)
    owner_context = _sidebar_owner_context_from_request(
        request,
        external_userid=resolved_external_userid,
        owner_userid=owner_userid,
    )
    try:
        result = GetAdminCustomerProfileTagsQuery(_sidebar_customer_context_query(customer_repo, live_source_repo))(
            external_userid=resolved_external_userid,
            owner_userid=str(owner_context.get("owner_userid") or "").strip(),
            require_owner_scope=True,
            owner_verified=bool(owner_context.get("owner_verified")),
        )
    except CustomerScopeForbiddenError:
        return _sidebar_forbidden_error()
    return JSONResponse(jsonable_encoder(result), status_code=_status_code(result))


@router.get("/api/sidebar/lead-pool/status")
def get_sidebar_lead_pool_status(
    request: Request,
    external_userid: str | None = None,
    owner_userid: str | None = None,
    db: Session = Depends(get_db),
):
    customer_repo, live_source_repo = _request_scoped_customer_repositories(db)
    resolved_external_userid = str(external_userid or "").strip()
    if not resolved_external_userid:
        return _sidebar_input_error("external_userid is required")
    owner_context = _sidebar_owner_context_from_request(
        request,
        external_userid=resolved_external_userid,
        owner_userid=owner_userid,
    )
    resolved_owner_userid = str(owner_context.get("owner_userid") or "").strip()
    context, response = _sidebar_context_or_response(
        resolved_external_userid,
        owner_userid=resolved_owner_userid,
        owner_verified=bool(owner_context.get("owner_verified")),
        customer_repo=customer_repo,
        live_source_repo=live_source_repo,
    )
    if response is not None:
        return response
    customer = dict((context or {}).get("customer") or {})
    binding = dict(customer.get("binding") or {})
    class_user_status = dict(customer.get("class_user_status") or {})
    sidebar_context = dict(customer.get("sidebar_context") or {})
    owner = resolved_owner_userid or str(customer.get("owner_userid") or "")
    class_term_no, class_term_label = _class_term_payload(class_user_status, sidebar_context)
    member = {
        "external_userid": resolved_external_userid,
        "customer_name": customer.get("customer_name") or "",
        "owner_userid": owner,
        "mobile": customer.get("mobile") or "",
        "class_term_no": class_term_no,
        "class_term_label": class_term_label,
    } if customer else {}
    payload = {
        "ok": True,
        "external_userid": resolved_external_userid,
        "owner_userid": owner,
        "member": member,
        "current_class_term_no": class_term_no,
        "current_class_term_label": class_term_label,
        "class_term_options": sidebar_context.get("class_term_options") or [],
        "is_wecom_added": bool(customer.get("external_userid")),
        "is_mobile_bound": bool(binding.get("is_bound") or customer.get("mobile")),
        **_sidebar_diagnostics_from_context(context or {}),
    }
    return JSONResponse(jsonable_encoder(payload), status_code=200)


@router.get("/api/sidebar/signup-tags/status")
def get_sidebar_signup_tag_status(
    request: Request,
    external_userid: str | None = None,
    owner_userid: str | None = None,
    db: Session = Depends(get_db),
):
    customer_repo, live_source_repo = _request_scoped_customer_repositories(db)
    resolved_external_userid = str(external_userid or "").strip()
    if not resolved_external_userid:
        return _sidebar_input_error("external_userid is required")
    owner_context = _sidebar_owner_context_from_request(
        request,
        external_userid=resolved_external_userid,
        owner_userid=owner_userid,
    )
    resolved_owner_userid = str(owner_context.get("owner_userid") or "").strip()
    context, response = _sidebar_context_or_response(
        resolved_external_userid,
        owner_userid=resolved_owner_userid,
        owner_verified=bool(owner_context.get("owner_verified")),
        customer_repo=customer_repo,
        live_source_repo=live_source_repo,
    )
    if response is not None:
        return response
    customer = dict((context or {}).get("customer") or {})
    class_user_status = dict(customer.get("class_user_status") or {})
    payload = {
        "ok": True,
        "external_userid": resolved_external_userid,
        "definitions": [],
        "initialized": False,
        "missing_statuses": [],
        "current_signup_status": str(class_user_status.get("signup_status") or class_user_status.get("current_status") or ""),
        "current_tag": str(class_user_status.get("signup_label_name") or ""),
        "wecom_tag_sync_status": str(class_user_status.get("wecom_tag_sync_status") or ""),
        "wecom_tag_sync_error": str(class_user_status.get("wecom_tag_sync_error") or ""),
        "marketing_profile": dict(customer.get("marketing_profile") or {}),
        **_sidebar_diagnostics_from_context(context or {}),
    }
    return JSONResponse(jsonable_encoder(payload), status_code=200)


@router.get("/api/sidebar/marketing-status")
def get_sidebar_marketing_status(
    request: Request,
    external_userid: str | None = None,
    owner_userid: str | None = None,
    db: Session = Depends(get_db),
):
    customer_repo, live_source_repo = _request_scoped_customer_repositories(db)
    resolved_external_userid = str(external_userid or "").strip()
    if not resolved_external_userid:
        return _sidebar_input_error("external_userid is required")
    owner_context = _sidebar_owner_context_from_request(
        request,
        external_userid=resolved_external_userid,
        owner_userid=owner_userid,
    )
    resolved_owner_userid = str(owner_context.get("owner_userid") or "").strip()
    context, response = _sidebar_context_or_response(
        resolved_external_userid,
        owner_userid=resolved_owner_userid,
        owner_verified=bool(owner_context.get("owner_verified")),
        customer_repo=customer_repo,
        live_source_repo=live_source_repo,
    )
    if response is not None:
        return response
    customer = dict((context or {}).get("customer") or {})
    marketing_summary = dict(customer.get("marketing_summary") or {})
    marketing_profile = dict(customer.get("marketing_profile") or {})
    class_user_status = dict(customer.get("class_user_status") or {})
    main_stage = str(marketing_summary.get("main_stage") or class_user_status.get("current_status") or "")
    sub_stage = str(marketing_summary.get("sub_stage") or class_user_status.get("activation_bucket") or "")
    segment = str(marketing_summary.get("value_segment") or marketing_profile.get("value_segment") or "")
    activated = str(class_user_status.get("activation_bucket") or "") == "activated"
    eligible = main_stage not in {"converted"} and bool(customer)
    marketing_status = {
        "external_userid": resolved_external_userid,
        "main_stage": main_stage,
        "sub_stage": sub_stage,
        "segment": segment,
        "stage_display": _display_status(sub_stage or main_stage),
        "segment_display": _display_status(segment),
        "pool_display": _display_status(sub_stage or main_stage),
        "activated": activated,
        "current_followup_type": segment,
        "current_followup_type_display": _display_status(segment),
        "questionnaire_segment_display": _display_status(segment),
        "followup_segment_source": "next_read_model",
        "followup_segment_source_display": "Next read model",
        "manual_override_active": False,
        "eligibility_display": "会" if eligible else "不会",
        "hit_count": len(marketing_profile.get("matched_question_ids") or []),
        "matched_question_ids": list(marketing_profile.get("matched_question_ids") or []),
        "eligible_for_conversion": eligible,
        "last_activation_at": class_user_status.get("updated_at") if activated else "",
        "last_conversion_marked_at": "",
        "recommended_action": marketing_profile.get("recommended_action") or "",
        "signals": list(marketing_profile.get("signals") or []),
    }
    payload = {
        "ok": True,
        "external_userid": resolved_external_userid,
        "marketing_status": marketing_status,
        **_sidebar_diagnostics_from_context(context or {}),
    }
    return JSONResponse(jsonable_encoder(payload), status_code=200)


@router.get("/api/sidebar/v2/workbench")
def get_sidebar_v2_workbench(
    request: Request,
    external_userid: str | None = None,
    owner_userid: str | None = None,
    db: Session = Depends(get_db, scope="function"),
):
    if not str(external_userid or "").strip():
        return _sidebar_input_error("external_userid is required")
    normalized_external_userid = str(external_userid or "").strip()
    owner_context = _sidebar_owner_context_from_request(
        request,
        external_userid=normalized_external_userid,
        owner_userid=owner_userid,
    )
    normalized_owner_userid = str(owner_context.get("owner_userid") or "").strip()
    context_query, live_source_repo = _request_scoped_customer_context_query(db)
    try:
        payload = SidebarWorkbenchReadModel(
            context_query=context_query,
            live_source_repo=live_source_repo,
        )(
            external_userid=normalized_external_userid,
            owner_userid=normalized_owner_userid,
            owner_verified=bool(owner_context.get("owner_verified")),
        )
    except CustomerScopeForbiddenError:
        return _sidebar_forbidden_error()
    except NotFoundError as exc:
        return _sidebar_lookup_error(str(exc) or "customer not found")
    except ValueError as exc:
        return _sidebar_input_error(str(exc))
    except Exception as exc:
        return _sidebar_read_unavailable(exc)
    return {"ok": True, **payload, "route_owner": "ai_crm_next"}


@router.get("/api/sidebar/v2/questionnaires")
def get_sidebar_v2_questionnaires(
    request: Request,
    external_userid: str | None = None,
    owner_userid: str | None = None,
    db: Session = Depends(get_db, scope="function"),
):
    if not str(external_userid or "").strip():
        return _sidebar_input_error("external_userid is required")
    owner_context = _sidebar_owner_context_from_request(
        request,
        external_userid=str(external_userid or "").strip(),
        owner_userid=owner_userid,
    )
    scoped_owner_userid = str(owner_context.get("owner_userid") or "").strip()
    context_query, live_source_repo = _request_scoped_customer_context_query(db)
    try:
        payload = SidebarQuestionnaireReadModel(
            context_query=context_query,
            live_source_repo=live_source_repo,
        )(
            external_userid=str(external_userid or "").strip(),
            owner_userid=scoped_owner_userid,
            owner_verified=bool(owner_context.get("owner_verified")),
        )
    except CustomerScopeForbiddenError:
        return _sidebar_forbidden_error()
    except NotFoundError as exc:
        return _sidebar_lookup_error(str(exc) or "customer not found")
    except ValueError as exc:
        return _sidebar_input_error(str(exc))
    except Exception as exc:
        return _sidebar_read_unavailable(exc)
    return {**payload, "route_owner": "ai_crm_next"}


@router.get("/api/sidebar/v2/timeline")
def get_sidebar_v2_timeline(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db, scope="function"),
):
    owner_context = _sidebar_owner_context_from_request(request)
    external_userid = str(owner_context.get("external_userid") or "").strip()
    owner_userid = str(owner_context.get("owner_userid") or "").strip()
    if not external_userid:
        return _sidebar_forbidden_error("sidebar customer scope required")
    customer_repo, live_source_repo = _request_scoped_customer_repositories(db)
    try:
        resolved_context = _verify_sidebar_owner_scope(
            _sidebar_customer_context_query(customer_repo, live_source_repo),
            external_userid=external_userid,
            owner_userid=owner_userid,
            corp_id=str(owner_context.get("corp_id") or "").strip(),
            owner_verified=bool(owner_context.get("owner_verified")),
        )
        payload = SidebarCustomerTimelineQuery(customer_repo)(
            external_userid=external_userid,
            context=resolved_context,
            limit=limit,
            offset=offset,
        )
    except CustomerScopeForbiddenError:
        return _sidebar_forbidden_error()
    except NotFoundError as exc:
        return _sidebar_lookup_error(str(exc) or "customer not found")
    except ValueError as exc:
        return _sidebar_input_error(str(exc))
    except Exception as exc:
        return _sidebar_read_unavailable(exc)
    return {**payload, "route_owner": "ai_crm_next", "fallback_used": False}


@router.get("/api/sidebar/v2/materials")
def get_sidebar_v2_materials(request: Request, type: str = "", limit: int = 50, offset: int = 0, q: str = ""):
    _sidebar_owner_context_from_request(request)
    try:
        payload = SidebarMaterialReadModel()(material_type=type, limit=limit, offset=offset, q=q)
    except ValueError as exc:
        return _sidebar_input_error(str(exc))
    except Exception as exc:
        return _sidebar_read_unavailable(exc)
    return {**payload, "route_owner": "ai_crm_next"}


@router.get("/api/sidebar/v2/materials/image/{image_id}/thumbnail")
def get_sidebar_v2_image_thumbnail(request: Request, background_tasks: BackgroundTasks, image_id: int):
    try:
        with media_binary_admission(rollout_key=f"sidebar-thumbnail:{image_id}"):
            payload = SidebarMaterialReadModel().thumbnail(image_id)
    except ResourceCapacityExhausted as exc:
        return JSONResponse(
            status_code=429,
            content={
                "ok": False,
                "error": "图片加载繁忙，请稍后重试",
                "error_code": "media_capacity_exhausted",
                "retryable": True,
                "route_owner": "ai_crm_next",
            },
            headers={"Retry-After": str(exc.policy.retry_after_seconds)},
        )
    except LookupError as exc:
        return _sidebar_lookup_error(str(exc) or "image not found")
    except ValueError as exc:
        return _sidebar_input_error(str(exc))
    except Exception as exc:
        return _sidebar_read_unavailable(exc)
    if payload.get("generation_required"):
        background_tasks.add_task(generate_missing_image_variants, str(image_id))
    versioned = bool(str(request.query_params.get("v") or "").strip())
    cache_control = (
        "no-store"
        if payload.get("generation_required")
        else "public, max-age=31536000, immutable" if versioned else "public, max-age=86400"
    )
    redirect_url = str(payload.get("redirect_url") or "").strip()
    if redirect_url:
        return RedirectResponse(redirect_url, status_code=302, headers={"Cache-Control": cache_control})
    headers = {"Cache-Control": cache_control}
    etag = str(payload.get("etag") or "").strip()
    if etag:
        headers["ETag"] = etag
    if payload.get("generation_required"):
        headers["X-AICRM-Media-Generation"] = "pending"
        headers["Retry-After"] = "1"
    if etag and str(request.headers.get("if-none-match") or "").strip() == etag:
        return Response(status_code=304, headers=headers)
    response = Response(payload.get("body") or b"", media_type=str(payload.get("mime_type") or "image/png"), headers=headers)
    return response


@router.get("/api/sidebar/v2/other-staff-messages")
def get_sidebar_v2_other_staff_messages(
    request: Request,
    external_userid: str | None = None,
    current_userid: str | None = None,
    owner_userid: str | None = None,
    limit: int = 20,
    db: Session = Depends(get_db, scope="function"),
):
    if not str(external_userid or "").strip():
        return _sidebar_input_error("external_userid is required")
    owner_context = _sidebar_owner_context_from_request(
        request,
        external_userid=str(external_userid or "").strip(),
        owner_userid=owner_userid,
        current_userid=current_userid,
    )
    scoped_userid = str(owner_context.get("owner_userid") or "").strip()
    try:
        context_query, _live_source_repo = _request_scoped_customer_context_query(db)
        _verify_sidebar_owner_scope(
            context_query,
            external_userid=str(external_userid or "").strip(),
            owner_userid=scoped_userid,
            owner_verified=bool(owner_context.get("owner_verified")),
        )
        payload = SidebarOtherStaffMessagesReadModel()(
            external_userid=str(external_userid or "").strip(),
            current_userid=scoped_userid,
            limit=limit,
        )
    except ValueError as exc:
        return _sidebar_input_error(str(exc))
    except CustomerScopeForbiddenError:
        return _sidebar_forbidden_error()
    except NotFoundError as exc:
        return _sidebar_lookup_error(str(exc) or "customer not found")
    except Exception as exc:
        return _sidebar_read_unavailable(exc)
    return {**payload, "route_owner": "ai_crm_next"}


@router.get("/api/sidebar/v2/products")
def get_sidebar_v2_products(
    request: Request,
    external_userid: str | None = None,
    owner_userid: str | None = None,
    bind_by_userid: str | None = None,
    db: Session = Depends(get_db, scope="function"),
):
    if not str(external_userid or "").strip():
        return _sidebar_input_error("external_userid is required")
    owner_context = _sidebar_owner_context_from_request(
        request,
        external_userid=str(external_userid or "").strip(),
        owner_userid=owner_userid,
        bind_by_userid=bind_by_userid,
    )
    scoped_owner_userid = str(owner_context.get("owner_userid") or "").strip()
    try:
        context_query, live_source_repo = _request_scoped_customer_context_query(db)
        _verify_sidebar_owner_scope(
            context_query,
            external_userid=str(external_userid or "").strip(),
            owner_userid=scoped_owner_userid,
            owner_verified=bool(owner_context.get("owner_verified")),
        )
        payload = SidebarCommerceReadModel(context_query=context_query, live_source_repo=live_source_repo).products(
            external_userid=str(external_userid or "").strip(),
            owner_userid=scoped_owner_userid,
            bind_by_userid=str(owner_context.get("bind_by_userid") or "").strip(),
        )
    except CustomerScopeForbiddenError:
        return _sidebar_forbidden_error()
    except NotFoundError as exc:
        return _sidebar_lookup_error(str(exc) or "customer not found")
    except ValueError as exc:
        return _sidebar_input_error(str(exc))
    except Exception as exc:
        return _sidebar_read_unavailable(exc)
    return {**payload, "route_owner": "ai_crm_next"}


@router.get("/api/sidebar/v2/orders")
def get_sidebar_v2_orders(
    request: Request,
    external_userid: str | None = None,
    owner_userid: str | None = None,
    db: Session = Depends(get_db, scope="function"),
):
    if not str(external_userid or "").strip():
        return _sidebar_input_error("external_userid is required")
    normalized_external_userid = str(external_userid or "").strip()
    owner_context = _sidebar_owner_context_from_request(
        request,
        external_userid=normalized_external_userid,
        owner_userid=owner_userid,
    )
    scoped_owner_userid = str(owner_context.get("owner_userid") or "").strip()
    context_query, live_source_repo = _request_scoped_customer_context_query(db)
    try:
        payload = SidebarCommerceReadModel(
            context_query=context_query,
            live_source_repo=live_source_repo,
        ).orders(
            external_userid=normalized_external_userid,
            owner_userid=scoped_owner_userid,
            owner_verified=bool(owner_context.get("owner_verified")),
        )
    except CustomerScopeForbiddenError:
        return _sidebar_forbidden_error()
    except NotFoundError as exc:
        return _sidebar_lookup_error(str(exc) or "customer not found")
    except ValueError as exc:
        return _sidebar_input_error(str(exc))
    except Exception as exc:
        return _sidebar_read_unavailable(exc)
    return {"ok": True, **payload, "route_owner": "ai_crm_next"}


@router.get("/api/sidebar/v2/periodic-orders")
def get_sidebar_v2_periodic_orders(
    request: Request,
    external_userid: str | None = None,
    owner_userid: str | None = None,
    db: Session = Depends(get_db, scope="function"),
):
    if not str(external_userid or "").strip():
        return _sidebar_input_error("external_userid is required")
    normalized_external_userid = str(external_userid or "").strip()
    owner_context = _sidebar_owner_context_from_request(
        request,
        external_userid=normalized_external_userid,
        owner_userid=owner_userid,
    )
    scoped_owner_userid = str(owner_context.get("owner_userid") or "").strip()
    context_query, live_source_repo = _request_scoped_customer_context_query(db)
    try:
        payload = SidebarCommerceReadModel(
            context_query=context_query,
            live_source_repo=live_source_repo,
        ).periodic_orders(
            external_userid=normalized_external_userid,
            owner_userid=scoped_owner_userid,
            owner_verified=bool(owner_context.get("owner_verified")),
        )
    except CustomerScopeForbiddenError:
        return _sidebar_forbidden_error()
    except NotFoundError as exc:
        return _sidebar_lookup_error(str(exc) or "customer not found")
    except ValueError as exc:
        return _sidebar_input_error(str(exc))
    except Exception as exc:
        return _sidebar_read_unavailable(exc)
    return {"ok": True, **payload, "route_owner": "ai_crm_next"}


@router.put("/api/sidebar/v2/periodic-orders/{entitlement_id}/remark")
async def update_sidebar_v2_periodic_order_remark(
    request: Request,
    entitlement_id: str,
    owner_userid: str | None = None,
    current_userid: str | None = None,
    db: Session = Depends(get_db, scope="function"),
):
    try:
        payload = await _sidebar_json_body(request)
        external_userid = str(payload.get("external_userid") or request.query_params.get("external_userid") or "").strip()
        if not external_userid:
            return _sidebar_input_error("external_userid is required")
        owner_context = _sidebar_owner_context_from_request(
            request,
            external_userid=external_userid,
            owner_userid=str(payload.get("owner_userid") or owner_userid or "").strip() or None,
            current_userid=current_userid,
        )
        scoped_owner_userid = str(owner_context.get("owner_userid") or "").strip()
        context_query, live_source_repo = _request_scoped_customer_context_query(db)
        _verify_sidebar_owner_scope(
            context_query,
            external_userid=external_userid,
            owner_userid=scoped_owner_userid,
            # This is a mutation. Keep its operation-level owner admission
            # separate from the trusted-context read boundary.
            owner_verified=False,
        )
        target = SidebarCommerceReadModel(context_query=context_query, live_source_repo=live_source_repo).periodic_order_remark_target(
            external_userid=external_userid,
            owner_userid=scoped_owner_userid,
            owner_verified=False,
            entitlement_id=entitlement_id,
        )
        result = UpdateServicePeriodMemberRemarkCommand()(
            target["service_product_id"],
            target["unionid"],
            remark=str(payload.get("remark") or ""),
        )
        member = dict(result.get("member") or {})
    except CustomerScopeForbiddenError:
        return _sidebar_forbidden_error()
    except NotFoundError as exc:
        return _sidebar_lookup_error(str(exc) or "periodic order not found")
    except ValueError as exc:
        return _sidebar_input_error(str(exc))
    except Exception as exc:
        return _sidebar_read_unavailable(exc)
    periodic_order = dict(target.get("periodic_order") or {})
    periodic_order["remark"] = str(member.get("remark") or "")
    return {
        "ok": True,
        "periodic_order": periodic_order,
        "source_status": "next_command",
        "write_model_status": "updated",
        "route_owner": "ai_crm_next",
        "fallback_used": False,
        "real_external_call_executed": False,
        "degraded": False,
    }


@router.get("/api/admin/customers/profile")
def get_admin_customer_profile(
    unionid: str | None = None,
    external_userid: str | None = None,
    mobile: str | None = None,
    user_id: str | None = None,
    db: Session = Depends(get_db),
):
    customer_repo, live_source_repo = _request_scoped_customer_repositories(db)
    result = GetAdminCustomerProfileQuery(GetCustomerContextQuery(customer_repo, live_source_repo=live_source_repo))(
        unionid=unionid,
        external_userid=external_userid,
        mobile=mobile,
        user_id=user_id,
    )
    status_code = int(result.pop("status_code", 200) or 200)
    return JSONResponse(jsonable_encoder(result), status_code=status_code)


@router.get("/api/admin/customer-360/{unionid}")
def get_admin_customer_360_profile(unionid: str, db: Session = Depends(get_db)) -> JSONResponse:
    customer_repo, live_source_repo = _request_scoped_customer_repositories(db)
    result = GetCustomer360ProfileQuery(GetCustomerContextQuery(customer_repo, live_source_repo=live_source_repo))(unionid)
    status_code = int(result.pop("status_code", 200) or 200)
    return JSONResponse(jsonable_encoder(result), status_code=status_code)


@router.get(
    "/api/admin/customers/{unionid}/business-profile",
    summary="客户商业档案",
    description="Session Cookie 后台接口，只聚合客户标签、最近聊天记录和问卷问题答案三类核心信息；订单和商业摘要请调用独立接口。",
)
def get_admin_customer_business_profile(
    unionid: str = Path(..., description="客户 unionid"),
    limit: int = Query(20, description="最近聊天记录条数，默认 20，最大 20"),
    db: Session = Depends(get_db),
) -> JSONResponse:
    customer_repo, live_source_repo = _request_scoped_customer_repositories(db)
    result = get_customer_business_profile(
        unionid=unionid,
        limit=limit,
        customer_repo=customer_repo,
        live_source_repo=live_source_repo,
    )
    status_code = int(result.pop("status_code", 200) or 200)
    return JSONResponse(jsonable_encoder(result), status_code=status_code)


@router.get("/api/admin/customers/profile/tags")
def get_admin_customer_profile_tags(
    unionid: str | None = None,
    external_userid: str | None = None,
    user_id: str | None = None,
    db: Session = Depends(get_db),
):
    customer_repo, live_source_repo = _request_scoped_customer_repositories(db)
    result = GetAdminCustomerProfileTagsQuery(GetCustomerContextQuery(customer_repo, live_source_repo=live_source_repo))(
        unionid=unionid,
        external_userid=external_userid,
        user_id=user_id,
    )
    status_code = int(result.pop("status_code", 200) or 200)
    return JSONResponse(jsonable_encoder(result), status_code=status_code)


@router.get("/api/admin/customers/profile/questionnaire-answers")
def get_admin_customer_profile_questionnaire_answers(
    unionid: str | None = None,
    external_userid: str | None = None,
    mobile: str | None = None,
    user_id: str | None = None,
    db: Session = Depends(get_db),
):
    customer_repo, live_source_repo = _request_scoped_customer_repositories(db)
    profile_result = GetAdminCustomerProfileQuery(GetCustomerContextQuery(customer_repo, live_source_repo=live_source_repo))(
        unionid=unionid,
        external_userid=external_userid,
        mobile=mobile,
        user_id=user_id,
    )
    if not profile_result.get("ok"):
        return JSONResponse(jsonable_encoder(profile_result), status_code=_profile_result_status(profile_result))
    profile = dict(profile_result.get("profile") or profile_result.get("customer") or {})
    answers = _profile_questionnaire_answers(profile)
    if not answers:
        answers = _profile_questionnaire_answers_from_submissions(
            external_userid=_profile_external_userid(profile_result),
            mobile=str(profile.get("mobile") or mobile or "").strip(),
        )
    latest_assessment_result = (
        dict(profile.get("latest_assessment_result") or {})
        or dict(dict(profile.get("marketing_profile") or {}).get("latest_assessment_result") or {})
        or dict(dict(profile.get("sidebar_context") or {}).get("latest_assessment_result") or {})
    )
    payload = {
        "ok": True,
        "unionid": _profile_unionid(profile_result),
        "external_userid": _profile_external_userid(profile_result),
        "answers": answers,
        "count": len(answers),
        "latest_assessment_result": latest_assessment_result or None,
        "source_status": profile_result.get("source_status"),
        "route_owner": "ai_crm_next",
    }
    return JSONResponse(jsonable_encoder(payload), status_code=200)


@router.get("/api/admin/customers/profile/messages")
def get_admin_customer_profile_messages(
    unionid: str | None = None,
    external_userid: str | None = None,
    mobile: str | None = None,
    user_id: str | None = None,
    fetch_all: str | None = None,
    limit: int | None = None,
    db: Session = Depends(get_db),
):
    customer_repo, live_source_repo = _request_scoped_customer_repositories(db)
    profile_result = GetAdminCustomerProfileQuery(GetCustomerContextQuery(customer_repo, live_source_repo=live_source_repo))(
        unionid=unionid,
        external_userid=external_userid,
        mobile=mobile,
        user_id=user_id,
    )
    if not profile_result.get("ok"):
        return JSONResponse(jsonable_encoder(profile_result), status_code=_profile_result_status(profile_result))
    customer = dict(profile_result.get("profile") or profile_result.get("customer") or {})
    resolved_external_userid = _profile_external_userid(profile_result)
    resolved_unionid = str(customer.get("unionid") or unionid or "").strip()
    if not resolved_unionid and not resolved_external_userid:
        return _input_error("unionid is required")
    requested_limit = int(limit or (100 if str(fetch_all or "").strip().lower() in {"1", "true", "yes", "on"} else 30))
    requested_limit = max(1, min(requested_limit, 100))
    result = ListRecentMessagesQuery(customer_repo, live_source_repo=live_source_repo)(
        RecentMessagesRequest(unionid=resolved_unionid or None, external_userid=resolved_external_userid or None, limit=requested_limit)
    )
    status_code = int(result.pop("status_code", 200) or 200)
    if not result.get("ok", True):
        return JSONResponse(jsonable_encoder(result), status_code=status_code)
    messages = list(result.get("messages") or result.get("items") or [])
    normalized_messages = [
        {
            **dict(message),
            "speaker": str(dict(message).get("speaker") or _message_speaker(dict(message), customer)),
            "send_time": dict(message).get("send_time") or dict(message).get("created_at") or dict(message).get("updated_at") or "",
        }
        for message in messages
    ]
    payload = {
        "ok": True,
        "unionid": resolved_unionid,
        "external_userid": resolved_external_userid,
        "messages": normalized_messages,
        "count": len(normalized_messages),
        "limit": requested_limit,
        "source_status": result.get("source_status") or profile_result.get("source_status"),
        "route_owner": "ai_crm_next",
    }
    return JSONResponse(jsonable_encoder(payload), status_code=status_code)
