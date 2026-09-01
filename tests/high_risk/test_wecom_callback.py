from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aicrm_next.channels.channel_entry.inbox import (
    WeComCallbackInboxWorker,
    ingest_wecom_callback,
    wecom_callback_idempotency_key,
)
from aicrm_next.channels.channel_entry.identity_bridge_service import IdentityBridgeService
from aicrm_next.platform.platform_foundation.webhook_inbox.repository import InMemoryWebhookInboxRepository


pytestmark = pytest.mark.high_risk

CURRENT_EVENT = {
    "ToUserName": "corp-current",
    "Event": "change_external_contact",
    "ChangeType": "add_external_contact",
    "ExternalUserID": "external-current",
    "UserID": "owner-current",
    "CreateTime": "1800000000",
    "WelcomeCode": "welcome-current",
}


def test_provider_detail_cannot_create_identity_without_the_current_employee_relationship() -> None:
    class UnexpectedRepository:
        def normalize_external_contact_identity(self, *_args, **_kwargs):
            raise AssertionError("unverified provider detail must not reach identity persistence")

    result = IdentityBridgeService(repository=UnexpectedRepository())._sync_external_contact_identity_for_detail(
        detail_payload={
            "external_contact": {"external_userid": "wm-forged"},
            "follow_user": [{"userid": "another-owner"}],
        },
        external_userid="wm-forged",
        owner_userid="current-owner",
        corp_id="corp-1",
    )

    assert result["status"] == "failed"
    assert result["reason"] == "provider_relationship_mismatch"


def _ingest_current_callback(repository: InMemoryWebhookInboxRepository) -> dict[str, object]:
    return ingest_wecom_callback(
        query={"timestamp": "1800000000", "nonce": "nonce-current"},
        headers={"authorization": "must-not-persist", "x-request-id": "request-current"},
        body=b"<xml>encrypted-envelope</xml>",
        event_data=dict(CURRENT_EVENT),
        plain_xml="<xml><Event>change_external_contact</Event></xml>",
        route="/api/wecom/events",
        repository=repository,
    )


def test_callback_ack_is_durable_idempotent_and_does_not_process_inline() -> None:
    repository = InMemoryWebhookInboxRepository()
    first = _ingest_current_callback(repository)
    second = _ingest_current_callback(repository)
    row = repository.rows[0]
    assert first["ack_boundary"] == "durable_inbox_only"
    assert second["duplicate"] is True
    assert first["id"] == second["id"]
    assert len(repository.rows) == 1
    assert row["status"] == "received"
    assert "authorization" not in row["raw_headers_json"]
    assert first["idempotency_key"] == wecom_callback_idempotency_key("corp-current", CURRENT_EVENT)


def test_callback_worker_retries_dead_letters_and_recovers_without_provider_io() -> None:
    repository = InMemoryWebhookInboxRepository()
    ingested = _ingest_current_callback(repository)
    repository.rows[0]["max_attempts"] = 2

    def fail_processing(_command: object) -> dict[str, object]:
        raise RuntimeError("fake callback processor failure")

    worker = WeComCallbackInboxWorker(repository, processor=fail_processing)
    preview = worker.run_due()
    first = worker.run_due(dry_run=False)
    repository.rows[0]["next_retry_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    second = worker.run_due(dry_run=False)

    assert preview["dry_run"] is True and preview["due_count"] == 1
    assert first["failed_retryable_count"] == 1
    assert second["dead_letter_count"] == 1
    assert repository.rows[0]["status"] == "dead_letter"

    recovered_worker = WeComCallbackInboxWorker(
        repository,
        processor=lambda _command: {"handled": True, "event_log": {"id": 1}},
    )
    recovered = recovered_worker.dispatch_one(int(ingested["id"]), reason="current-test-recovery")
    assert recovered["ok"] is True
    assert recovered["status"] == "succeeded"
    assert repository.rows[0]["status"] == "succeeded"
