from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from aicrm_next.extensions.commerce.commerce.domain import has_product_page_material
from aicrm_next.platform.shared.product_code_aliases import canonical_product_code
from aicrm_next.platform.navigation_target.resolver import url_link_resolver_response
from aicrm_next.platform.shared.errors import NotFoundError
from aicrm_next.platform.shared.sync_request import read_request_body
from aicrm_next.platform.shared.runtime import production_environment
from aicrm_next.platform.shared.signed_context import (
    SIDEBAR_PRODUCT_CONTEXT_COOKIE,
    load_sidebar_product_context_token,
    sidebar_product_context_ttl_seconds,
)

from .h5_wechat_pay import (
    checkout_page_state,
    create_jsapi_order_response,
    notify_response,
    order_status_response,
    payment_oauth_callback,
    payment_oauth_start,
    sidebar_product_context_status,
)
from .service import (
    blocked_action_payload,
    diagnostics_payload,
    get_public_product,
    list_public_products,
    normalize_public_path,
    payment_action_detected,
    product_not_found_payload,
    public_product_image_variant,
    public_product_payload,
    render_not_found_page,
    render_pay_landing,
    render_product_page,
    route_headers,
)


router = APIRouter()


def _public_product_alias_redirect(request: Request, path: str) -> Response | None:
    canonical = canonical_product_code(path)
    if not canonical or canonical == path:
        return None
    query = _noncredential_query(request)
    return RedirectResponse(
        url=f"/p/{quote(canonical)}{query}",
        status_code=302,
        headers={**route_headers(), "X-AICRM-Compatibility-Facade": "product_code_alias_redirect"},
    )


def _public_product_checkout_redirect(request: Request, product: dict) -> RedirectResponse:
    product_code = quote(str(product.get("product_code") or "").strip())
    query = _noncredential_query(request)
    return RedirectResponse(url=f"/pay/{product_code}{query}", status_code=302, headers=route_headers())


@router.options("/p/{path:path}", name="api.public_product_page_options")
def public_product_page_options(path: str) -> JSONResponse:
    return JSONResponse(diagnostics_payload(f"/p/{path}", allowed_methods=["GET", "HEAD", "OPTIONS"]), headers=route_headers())


@router.api_route("/p/{path:path}", methods=["GET", "HEAD"], response_class=HTMLResponse, name="api.public_product_page")
def public_product_page(request: Request, path: str) -> Response:
    redirect = _public_product_alias_redirect(request, path)
    if redirect is not None:
        return redirect
    try:
        product = get_public_product(path)
    except NotFoundError:
        return HTMLResponse(render_not_found_page(path), status_code=404, headers=route_headers())
    if not has_product_page_material(product):
        return _public_product_checkout_redirect(request, product)
    context_token = str(request.cookies.get(SIDEBAR_PRODUCT_CONTEXT_COOKIE) or "").strip()
    return HTMLResponse(
        render_product_page(product, context_status=sidebar_product_context_status(context_token)),
        headers=route_headers(),
    )


@router.post("/api/h5/product-context/session", name="api.h5_product_context_session")
async def h5_product_context_session(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    token = str(payload.get("context_token") or "").strip() if isinstance(payload, dict) else ""
    result = load_sidebar_product_context_token(token)
    if not result.get("ok"):
        return JSONResponse(
            {"ok": False, "error": "invalid_product_context"},
            status_code=400,
            headers=route_headers(),
        )
    response = JSONResponse({"ok": True, "context_status": "valid"}, headers=route_headers())
    forwarded_proto = str(request.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip().lower()
    response.set_cookie(
        SIDEBAR_PRODUCT_CONTEXT_COOKIE,
        token,
        max_age=sidebar_product_context_ttl_seconds(),
        httponly=True,
        secure=production_environment() or request.url.scheme == "https" or forwarded_proto == "https",
        samesite="lax",
        path="/",
    )
    return response


@router.options("/pay/{path:path}", name="api.public_pay_landing_options")
def public_pay_landing_options(path: str) -> JSONResponse:
    return JSONResponse(diagnostics_payload(f"/pay/{path}", allowed_methods=["GET", "HEAD", "OPTIONS"]), headers=route_headers())


@router.api_route("/pay/{path:path}", methods=["GET", "HEAD"], response_class=HTMLResponse, name="api.public_pay_landing")
def public_pay_landing(request: Request, path: str) -> Response:
    try:
        product = get_public_product(path)
    except NotFoundError:
        return HTMLResponse(render_not_found_page(path), status_code=404, headers=route_headers())
    page_state = checkout_page_state(product, request)
    if page_state.get("identity_error") in {"unionid_oauth_required", "wechat_oauth_required"} and page_state.get("oauth_start_url"):
        return RedirectResponse(str(page_state["oauth_start_url"]), status_code=302, headers=route_headers())
    return HTMLResponse(render_pay_landing(product, page_state), headers=route_headers())


@router.get("/api/h5/wechat-pay/oauth/start", name="api.h5_wechat_pay_oauth_start")
def h5_wechat_pay_oauth_start(request: Request):
    return payment_oauth_start(request)


@router.get("/api/h5/wechat-pay/oauth/callback", name="api.h5_wechat_pay_oauth_callback")
def h5_wechat_pay_oauth_callback(request: Request):
    return payment_oauth_callback(request)


@router.get("/api/h5/wechat-pay/products/{path:path}", name="api.h5_wechat_pay_product")
def h5_wechat_pay_product(path: str) -> JSONResponse:
    try:
        product = get_public_product(path)
    except NotFoundError:
        return JSONResponse(product_not_found_payload(path), status_code=404, headers=route_headers())
    return JSONResponse({"ok": True, "product": product}, headers=route_headers())


@router.post("/api/h5/wechat-pay/jsapi/orders", name="api.h5_wechat_pay_create_jsapi_order")
def h5_wechat_pay_create_jsapi_order(request: Request) -> JSONResponse:
    payload: Any = {}
    body = read_request_body(request)
    if request.headers.get("content-type", "").startswith("application/json") and body:
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            payload = {}
    return create_jsapi_order_response(request, payload if isinstance(payload, dict) else {})


@router.get("/api/h5/wechat-pay/orders/{out_trade_no}", name="api.h5_wechat_pay_order_status")
def h5_wechat_pay_order_status(out_trade_no: str, request: Request) -> JSONResponse:
    return order_status_response(out_trade_no, request)


@router.get("/api/h5/navigation-target/url-link/resolve", name="api.h5_navigation_target_url_link_resolve")
def h5_navigation_target_url_link_resolve(request: Request) -> Response:
    return url_link_resolver_response(
        source_url=request.query_params.get("source_url"),
        response_url_key=request.query_params.get("response_url_key") or "url_link",
        fallback_url=request.query_params.get("fallback_url") or "",
    )


@router.get("/api/h5/product-images/{product_code}/{image_id}/variants/{variant_key}", name="api.h5_public_product_image_variant")
def h5_public_product_image_variant(product_code: str, image_id: str, variant_key: str, request: Request) -> Response:
    try:
        variant = public_product_image_variant(product_code, image_id, variant_key)
    except NotFoundError:
        return JSONResponse(product_not_found_payload(product_code), status_code=404, headers=route_headers())
    etag = str(variant.get("etag") or "").strip()
    headers = {
        **route_headers(),
        "Cache-Control": "public, max-age=31536000, immutable",
    }
    if etag:
        headers["ETag"] = etag
    if etag and str(request.headers.get("If-None-Match") or "").strip() == etag:
        return Response(status_code=304, headers=headers)
    return Response(
        content=variant.get("bytes") or b"",
        media_type=str(variant.get("mime_type") or "image/png"),
        headers=headers,
    )


@router.post("/api/h5/wechat-pay/notify", name="api.h5_wechat_pay_notify")
def h5_wechat_pay_notify(request: Request) -> JSONResponse:
    body = read_request_body(request)
    return notify_response(request, body)


@router.options("/api/products/{path:path}", name="api.public_product_api_options")
def public_product_api_options(path: str) -> JSONResponse:
    return JSONResponse(diagnostics_payload(f"/api/products/{path}", allowed_methods=["GET", "HEAD", "OPTIONS"]), headers=route_headers())


@router.api_route("/api/products/{path:path}", methods=["GET", "HEAD"], name="api.public_product_api")
def public_product_api(path: str) -> JSONResponse:
    try:
        normalized = normalize_public_path(path)
        if normalized == "list":
            return JSONResponse(list_public_products(), headers=route_headers())
        if payment_action_detected(normalized):
            return JSONResponse(blocked_action_payload(normalized, method="GET"), status_code=410, headers=route_headers())
        return JSONResponse(public_product_payload(normalized), headers=route_headers())
    except NotFoundError:
        return JSONResponse(product_not_found_payload(path), status_code=404, headers=route_headers())


@router.api_route("/api/products/{path:path}", methods=["POST", "PUT", "PATCH", "DELETE"], name="api.public_product_api_blocked_write")
async def public_product_api_blocked_write(request: Request, path: str) -> JSONResponse:
    return JSONResponse(blocked_action_payload(path, method=request.method), status_code=410, headers=route_headers())


def _noncredential_query(request: Request) -> str:
    values = [
        (key, value)
        for key, value in parse_qsl(str(request.url.query or ""), keep_blank_values=True)
        if key.lower() not in {"ctx", "context_token", "token", "access_token"}
    ]
    return f"?{urlencode(values)}" if values else ""
