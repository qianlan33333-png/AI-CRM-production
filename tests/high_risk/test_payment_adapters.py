from __future__ import annotations

import pytest
from fastapi import Request

from aicrm_next.channels.integration_gateway.audit import reset_audit_events
from aicrm_next.channels.integration_gateway.idempotency import reset_idempotency_store
from aicrm_next.channels.integration_gateway.payment_adapters import ProductWriteGateway, WeChatPayAdapter
from aicrm_next.extensions.commerce.commerce.payment_tagging import product_paid_wecom_tag_consumer
from aicrm_next.extensions.commerce.commerce.wechat_shop_service import _upsert_order
from aicrm_next.extensions.commerce.public_product import h5_wechat_pay
from aicrm_next.platform.shared.wechat_h5_session import WECHAT_PAYMENT_IDENTITY_COOKIE, sign_payment_session_payload
from aicrm_next.platform.platform_foundation.external_effects import (
    ExternalEffectService,
    InMemoryExternalEffectRepository,
    WECOM_CONTACT_TAG_MARK,
)
from aicrm_next.platform.platform_foundation.internal_events.models import InternalEvent, InternalEventConsumerRun


pytestmark = pytest.mark.high_risk


def setup_function() -> None:
    reset_audit_events()
    reset_idempotency_store()


def _h5_request(*, user_agent: str, identity: dict | None = None, path: str = "/pay/current") -> Request:
    headers = [(b"user-agent", user_agent.encode("utf-8"))]
    if identity is not None:
        cookie = sign_payment_session_payload(identity)
        headers.append((b"cookie", f"{WECHAT_PAYMENT_IDENTITY_COOKIE}={cookie}".encode("utf-8")))
    return Request({"type": "http", "method": "GET", "path": path, "query_string": b"", "headers": headers})


def test_wechat_pay_checkout_requires_wechat_browser_and_unionid() -> None:
    product = {"id": 7, "product_code": "current", "name": "Current", "amount_total": 990}
    outside_wechat = h5_wechat_pay.checkout_page_state(product, _h5_request(user_agent="Safari"))
    assert outside_wechat["identity_ready"] is False
    assert outside_wechat["identity_error"] == "wechat_browser_required"

    openid_only = h5_wechat_pay.checkout_page_state(
        product,
        _h5_request(
            user_agent="MicroMessenger",
            identity={"openid": "openid-current", "app_id": "wx-current", "unionid": ""},
        ),
    )
    assert openid_only["identity_ready"] is False
    assert openid_only["identity_error"] == "unionid_oauth_required"
    assert openid_only["oauth_start_url"].startswith("/api/h5/wechat-pay/oauth/start?")


def test_wechat_pay_oauth_start_does_not_accept_openid_only_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(h5_wechat_pay, "_oauth_configured", lambda: True)
    monkeypatch.setattr(h5_wechat_pay, "_external_base_url", lambda _request: "https://crm.example.com")
    monkeypatch.setattr(h5_wechat_pay, "_payment_oauth_app_id", lambda: "wx-current")
    response = h5_wechat_pay.payment_oauth_start(
        _h5_request(
            user_agent="MicroMessenger",
            identity={"openid": "openid-current", "app_id": "wx-current", "unionid": ""},
            path="/api/h5/wechat-pay/oauth/start",
        )
    )
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://open.weixin.qq.com/connect/oauth2/authorize?")


def test_wechat_shop_order_without_unionid_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="wechat_shop_unionid_required"):
        _upsert_order({"order_id": "shop-order-without-unionid", "openid": "openid-current"})


def test_fake_payment_is_idempotent_and_never_calls_a_provider() -> None:
    adapter = WeChatPayAdapter("fake")
    first = adapter.create_jsapi_order(
        order_id="order-current-1",
        product_id="product-current",
        openid="openid-fixture",
        amount=990,
        idempotency_key="checkout-current-1",
    )
    second = adapter.create_jsapi_order(
        order_id="order-current-1",
        product_id="product-current",
        openid="openid-fixture",
        amount=990,
        idempotency_key="checkout-current-1",
    )
    assert first["result"] == second["result"]
    assert first["result"]["provider_called"] is False
    assert first["side_effect_executed"] is False


def test_payment_and_product_guards_fail_closed_and_scrub_secrets() -> None:
    blocked = WeChatPayAdapter("production").query_order(order_id="order-current-2")
    preview = ProductWriteGateway("fake").create_product(
        product_code="service-current",
        amount=19900,
        payload_summary={"api_key": "must-not-survive", "display_name": "Current product"},
    )
    assert blocked["ok"] is False
    assert blocked["error_code"] == "production_guard_failed"
    assert blocked["side_effect_executed"] is False
    assert preview["target"]["payload_summary"]["payload_keys"] == ["display_name"]


def _paid_product_event() -> InternalEvent:
    return InternalEvent(
        event_id="payment-tag-current",
        aggregate_id="42",
        payload_json={
            "order": {
                "id": 42,
                "out_trade_no": "WXP_TAG_CURRENT",
                "product_code": "tagged_product",
                "status": "paid",
                "trade_state": "SUCCESS",
                "unionid": "union-current",
            }
        },
    )


def test_paid_product_tagging_is_configurable_and_idempotent() -> None:
    effects = ExternalEffectService(InMemoryExternalEffectRepository())
    config = {"enabled": True, "tag_ids": ["tag_paid", "tag_registered"], "owner_userid": ""}
    identity = {
        "ok": True,
        "external_userid": "external-current",
        "follow_user_userid": "owner-current",
    }
    run = InternalEventConsumerRun(consumer_name="product_paid_wecom_tag_consumer")
    first = product_paid_wecom_tag_consumer(
        _paid_product_event(),
        run,
        config_resolver=lambda _code: config,
        identity_resolver=lambda _order, _owner: identity,
        external_effects=effects,
    )
    second = product_paid_wecom_tag_consumer(
        _paid_product_event(),
        run,
        config_resolver=lambda _code: config,
        identity_resolver=lambda _order, _owner: identity,
        external_effects=effects,
    )
    jobs, total = effects.list_jobs({"effect_type": WECOM_CONTACT_TAG_MARK})
    created_key = "external_effect_job_" + "created"
    assert first.status == "succeeded" and first.response_summary[created_key] is True
    assert second.status == "succeeded" and second.response_summary["external_effect_job_reused"] is True
    assert total == 1
    assert jobs[0].payload_json["tag_ids"] == ["tag_paid", "tag_registered"]


@pytest.mark.parametrize("reason", ["wecom_contact_not_found", "wecom_external_userid_missing"])
def test_paid_product_without_wecom_identity_is_terminal_skip(reason: str) -> None:
    effects = ExternalEffectService(InMemoryExternalEffectRepository())
    result = product_paid_wecom_tag_consumer(
        _paid_product_event(),
        InternalEventConsumerRun(consumer_name="product_paid_wecom_tag_consumer"),
        config_resolver=lambda _code: {"enabled": True, "tag_ids": ["tag_paid"]},
        identity_resolver=lambda _order, _owner: {"ok": False, "reason": reason},
        external_effects=effects,
    )
    jobs, total = effects.list_jobs({"effect_type": WECOM_CONTACT_TAG_MARK})
    assert result.status == "skipped"
    assert result.response_summary["retry_scheduled"] is False
    assert result.result_summary["reason"] == reason
    assert jobs == [] and total == 0
