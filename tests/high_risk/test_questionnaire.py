from __future__ import annotations

import pytest
from fastapi import Request

from aicrm_next.channels.integration_gateway.questionnaire_adapters import WeChatOAuthAdapter
from aicrm_next.extensions.forms.questionnaire import event_consumers
from aicrm_next.extensions.forms.questionnaire.api import _questionnaire_access_decision
from aicrm_next.extensions.forms.questionnaire.dto import OAuthCallbackRequest, OAuthStartRequest
from aicrm_next.extensions.forms.questionnaire.operations import QuestionnaireOperationsService
from aicrm_next.extensions.forms.questionnaire.oauth import QuestionnaireOAuthAdapter, build_questionnaire_h5_identity_cookie
from aicrm_next.extensions.forms.questionnaire.repo_memory import InMemoryQuestionnaireRepository
from aicrm_next.platform.platform_foundation.internal_events.models import InternalEvent, InternalEventConsumerRun


pytestmark = pytest.mark.high_risk


def _request(*, user_agent: str, cookie: str = "") -> Request:
    headers = [(b"user-agent", user_agent.encode("utf-8"))]
    if cookie:
        headers.append((b"cookie", cookie.encode("utf-8")))
    return Request({"type": "http", "method": "GET", "path": "/s/current", "query_string": b"", "headers": headers})


def test_questionnaire_requires_wechat_browser_and_signed_unionid() -> None:
    outside_wechat = _questionnaire_access_decision(_request(user_agent="Safari"), "current")
    assert outside_wechat.allowed is False
    assert outside_wechat.error == "wechat_browser_required"
    assert outside_wechat.status_code == 403

    openid_only_cookie = build_questionnaire_h5_identity_cookie({"openid": "openid-current", "slug": "current"})
    missing_unionid = _questionnaire_access_decision(
        _request(user_agent="MicroMessenger", cookie=f"questionnaire_h5_identity={openid_only_cookie}"),
        "current",
    )
    assert missing_unionid.allowed is False
    assert missing_unionid.error == "unionid_oauth_required"
    assert missing_unionid.oauth_start_url.startswith("/api/h5/wechat/oauth/start?")


def test_questionnaire_oauth_callback_rejects_openid_without_unionid() -> None:
    class OpenidOnlyOAuth(QuestionnaireOAuthAdapter):
        def fetch_user_identity(self, request, state_payload):
            return {"ok": True, "openid": "openid-only", "unionid": "", "real_external_call_executed": True}

    adapter = OpenidOnlyOAuth(mode="sandbox")
    state = adapter.build_authorize_url(OAuthStartRequest(slug="current"))["state"]
    result = adapter.callback(OAuthCallbackRequest(code="openid-only-code", state=state))
    assert result["ok"] is False
    assert result["error"] == "unionid_required"
    assert result["status_code"] == 409


def test_real_questionnaire_oauth_ignores_forged_callback_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    class OpenidOnlyClient:
        @staticmethod
        def exchange_code(**_kwargs):
            return {"openid": "provider-openid", "access_token": ""}

    monkeypatch.setenv("AICRM_NEXT_ENABLE_REAL_WECHAT_OAUTH", "1")
    monkeypatch.setenv("WECHAT_MP_APP_ID", "wx-provider")
    monkeypatch.setenv("WECHAT_MP_APP_SECRET", "provider-secret")
    adapter = WeChatOAuthAdapter("production", oauth_client_factory=lambda: OpenidOnlyClient())
    result = adapter.resolve_oauth_identity(
        state="trusted-state",
        code="provider-code",
        openid="forged-openid",
        unionid="forged-unionid",
        external_userid="forged-external-userid",
    )
    assert result["ok"] is True
    assert result["result"]["openid"] == "provider-openid"
    assert result["result"]["unionid"] == ""
    assert result["result"]["external_userid"] == ""


def _event_and_run() -> tuple[InternalEvent, InternalEventConsumerRun]:
    event = InternalEvent(
        event_id="questionnaire-event-current",
        event_type="questionnaire.submitted",
        aggregate_type="questionnaire_submission",
        aggregate_id="submission-current",
        request_id="request-current",
        trace_id="trace-current",
    )
    return event, InternalEventConsumerRun(event_id=event.event_id, consumer_name="questionnaire_webhook")


def test_questionnaire_push_can_plan_from_customer_identity_without_unionid(monkeypatch: pytest.MonkeyPatch) -> None:
    event, run = _event_and_run()
    monkeypatch.setattr(
        event_consumers,
        "_load_authoritative_context",
        lambda _event: (
            {"id": 7, "external_push_enabled": True, "external_push_url": "https://fixture.invalid/hook"},
            {
                "submission_id": "submission-current",
                "questionnaire_id": 7,
                "customer_id": 91,
                "openid": "openid-current",
                "unionid": "",
            },
            [],
            "",
        ),
    )

    class PlannedEffect:
        @staticmethod
        def find_existing_job(**_kwargs):
            return None

        @staticmethod
        def plan_effect(**kwargs):
            assert kwargs["target_type"] == "questionnaire_submission"
            return {"id": 17, "status": "queued"}

    monkeypatch.setattr(event_consumers, "ExternalEffectService", PlannedEffect)
    result = event_consumers.questionnaire_webhook_consumer(event, run)
    assert result.status == "succeeded"
    assert result.response_summary["external_effect_job_created"] is True


def test_questionnaire_without_external_push_is_an_explicit_no_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    event, run = _event_and_run()
    monkeypatch.setattr(
        event_consumers,
        "_load_authoritative_context",
        lambda _event: (
            {"id": 7, "external_push_enabled": False},
            {"submission_id": "submission-current", "questionnaire_id": 7, "unionid": "union-current"},
            [],
            "",
        ),
    )
    result = event_consumers.questionnaire_webhook_consumer(event, run)
    assert result.status == "skipped"
    assert result.response_summary["external_effect_job_created"] is False
    assert result.response_summary["reason"] == "questionnaire_external_push_not_configured"


def test_questionnaire_lead_qr_copy_is_saved_per_questionnaire_and_projected() -> None:
    class ChannelReader:
        @staticmethod
        def require_usable_channel_qr(channel_id: int) -> dict:
            assert channel_id == 701
            return {
                "channel_id": 701,
                "channel_name": "问卷渠道",
                "qr_url": "https://example.com/questionnaire.png",
                "selectable": True,
            }

        get_channel_qr = require_usable_channel_qr

    repository = InMemoryQuestionnaireRepository()
    service = QuestionnaireOperationsService(repository=repository, channel_reader=ChannelReader())
    saved = service.save_completion(
        1,
        {
            "enabled": True,
            "action_type": "lead_qr",
            "lead_channel_id": 701,
            "lead_qr_title": "问卷完成",
            "lead_qr_subtitle": "扫码领取你的专属资料",
        },
    )
    assert saved["completion"]["lead_qr_title"] == "问卷完成"
    assert saved["completion"]["lead_qr_subtitle"] == "扫码领取你的专属资料"

    action = service.resolve_completion_action(repository.get_questionnaire(1))["completion_action"]
    assert action["lead_qr"]["title"] == "问卷完成"
    assert action["lead_qr"]["subtitle"] == "扫码领取你的专属资料"
