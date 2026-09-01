from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Any

from aicrm_next.platform.shared.release import current_release_sha
from aicrm_next.platform.shared.runtime_settings import (
    managed_runtime_setting,
    runtime_setting,
    startup_environment_setting,
)
from aicrm_next.platform.shared.safe_logging import safe_log_exception

from . import repo
from .domain import (
    ENTRY_CHANGE_TYPES,
    channel_enabled,
    channel_payload,
    effect_status_for_duplicate,
    extract_corp_id,
    extract_scene,
    extract_welcome_code,
    scene_match,
    text,
)
from .identity_bridge import sync_external_contact_identity_for_event
from .crm_port import ensure_verified_wecom_customer, close_verified_wecom_relationship
from .schemas import (
    DiagnoseChannelRuntimeQuery,
    GenerateChannelQrCodeCommand,
    ProcessChannelEntryCommand,
    ProcessWeComExternalContactEventCommand,
    RepairChannelEntryCommand,
)
from .realtime_contract import utc_datetime, welcome_provider_deadline
from .wecom_adapter import WeComAdapterBlocked, WeComApiError, get_wecom_adapter, wecom_adapter_diagnostics
from .wecom_crypto import build_encrypted_reply, decrypt_message, parse_callback_xml, validate_callback_timestamp, verify_signature
from aicrm_next.platform.platform_foundation.command_bus.models import CommandContext
from aicrm_next.platform.platform_foundation.external_effects import (
    ExternalEffectService,
    WECOM_CONTACT_TAG_MARK,
    WECOM_WELCOME_MESSAGE_SEND,
)
from aicrm_next.platform.platform_foundation.internal_events import InternalEventService
from aicrm_next.platform.platform_foundation.external_effects.realtime import wake_external_effect_job
from aicrm_next.platform.platform_foundation.external_effects.adapters import ExternalEffectAdapterRegistry
from aicrm_next.channels.channel_entry.welcome_media_effects_repository import (
    WelcomeEffectGraphRequest,
    build_welcome_effect_graph_repository,
)

CUSTOMER_NAME_PLACEHOLDER_RE = re.compile(r"\{\{\s*客户名\s*\}\}")
LOGGER = logging.getLogger(__name__)


def callback_config() -> dict[str, str]:
    return {
        "corp_id": text(managed_runtime_setting("WECOM_CORP_ID")),
        "token": text(runtime_setting("WECOM_CALLBACK_TOKEN")),
        "aes_key": text(runtime_setting("WECOM_CALLBACK_AES_KEY")),
    }


def _event_key(corp_id: str, event_data: dict[str, Any]) -> str:
    fields = [
        corp_id,
        text(event_data.get("Event")),
        text(event_data.get("ChangeType")),
        text(event_data.get("ExternalUserID")),
        text(event_data.get("UserID")),
        text(event_data.get("CreateTime")),
        text(event_data.get("WelcomeCode")),
        text(event_data.get("State")),
    ]
    return "|".join(fields)


def decrypt_callback_body(*, query: dict[str, str], body: bytes) -> tuple[dict[str, Any], str]:
    validate_callback_timestamp(text(query.get("timestamp")))
    config = callback_config()
    xml_text = body.decode("utf-8")
    envelope = parse_callback_xml(xml_text)
    encrypted = text(envelope.get("Encrypt"))
    verify_signature(config["token"], text(query.get("timestamp")), text(query.get("nonce")), encrypted, text(query.get("msg_signature")))
    plain_xml = decrypt_message(encrypted, config["aes_key"], config["corp_id"])
    return parse_callback_xml(plain_xml), plain_xml


def verify_callback_echostr(query: dict[str, str]) -> str:
    validate_callback_timestamp(text(query.get("timestamp")))
    config = callback_config()
    echostr = text(query.get("echostr"))
    verify_signature(config["token"], text(query.get("timestamp")), text(query.get("nonce")), echostr, text(query.get("msg_signature")))
    return decrypt_message(echostr, config["aes_key"], config["corp_id"])


def encrypted_success_reply(query: dict[str, str]) -> str:
    config = callback_config()
    return build_encrypted_reply("success", config["token"], config["aes_key"], config["corp_id"], nonce=text(query.get("nonce")))


def resolve_channel_for_scene(*, scene_value: str, corp_id: str = "", persist_alias: bool = True) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    scene = text(scene_value)
    if not scene:
        return None, scene_match("missing_scene", "")
    asset = repo.find_qrcode_asset_by_scene(text(corp_id), scene)
    if asset:
        asset_status = text(asset.get("status"))
        match = scene_match(f"qrcode_asset_{asset_status or 'unknown'}", scene, {"id": asset.get("channel_id"), "scene_alias_id": asset.get("id"), "status": asset_status, "source": asset.get("generation_source")})
        match["qrcode_asset_id"] = int(asset.get("id") or 0) or None
        if asset_status in {"stale", "quarantined", "revoked"}:
            match["reason"] = "qrcode_asset_not_acceptable"
            return None, match
        if int(asset.get("id") or 0) > 0 and persist_alias:
            repo.touch_qrcode_asset_callback(int(asset["id"]))
        channel = {
            **asset,
            "id": int(asset.get("channel_id") or asset.get("channel_row_id") or 0),
            "scene_value": text(asset.get("channel_scene_value") or asset.get("scene_value")),
            "qr_url": text(asset.get("channel_qr_url") or asset.get("qr_url")),
            "status": text(asset.get("channel_status") or asset.get("status")),
        }
        if asset_status == "retired":
            match["reason"] = "retired_qrcode_asset_used"
        return channel, match
    channel = repo.find_confirmed_channel_by_scene_alias(text(corp_id), scene)
    if channel:
        if persist_alias:
            repo.update_alias_last_seen_at(text(corp_id), scene)
        return channel, scene_match("scene_alias", scene, channel)
    suggestion = repo.find_channel_by_historical_scene_value(scene)
    match = scene_match("not_found", scene)
    match["reason"] = "qrcode_scene_unrecognized"
    if suggestion:
        match["historical_vote"] = {
            "suggested_channel_id": int(suggestion.get("id") or 0) or None,
            "requires_admin_confirmation": True,
        }
    return None, match


def _log_effect(command: ProcessChannelEntryCommand, *, effect_type: str, idempotency_key: str, status: str, channel_id: int | None, scene_value: str, reason: str, request_json: dict[str, Any] | None = None, response_json: dict[str, Any] | None = None) -> dict[str, Any]:
    if command.dry_run:
        return {"effect_type": effect_type, "idempotency_key": idempotency_key, "status": "skipped", "reason": "dry_run"}
    return repo.upsert_channel_entry_effect_log(
        effect_type=effect_type,
        idempotency_key=idempotency_key,
        status=status,
        event_log_id=command.event_log_id,
        channel_id=channel_id,
        scene_value=scene_value,
        unionid=command.unionid,
        external_contact_id=command.external_contact_id,
        owner_staff_id=command.follow_user_userid,
        reason=reason,
        request_json=repo.json_safe(request_json or {}),
        response_json=repo.json_safe(response_json or {}),
    )


def _plan_channel_entry_effect(
    command: ProcessChannelEntryCommand,
    *,
    effect_type: str,
    adapter_name: str,
    operation: str,
    target_type: str,
    target_id: str,
    business_id: str,
    idempotency_key: str,
    payload: dict[str, Any],
    payload_summary: dict[str, Any],
    business_type: str = "channel_entry",
) -> dict[str, Any]:
    return ExternalEffectService().plan_effect(
        effect_type=effect_type,
        adapter_name=adapter_name,
        operation=operation,
        target_type=target_type,
        target_id=target_id,
        business_type=business_type,
        business_id=business_id,
        source_module="channel_entry.application",
        source_event_id=str(command.event_log_id or ""),
        idempotency_key=f"channel_entry:{idempotency_key}",
        context=CommandContext(
            actor_id=text(command.operator_id or command.follow_user_userid),
            actor_type="channel_entry",
            request_id=str(command.event_log_id or ""),
            trace_id=f"channel-entry-{command.event_log_id}" if command.event_log_id else idempotency_key,
            source_route="channel_entry.process_channel_entry",
            dry_run=bool(command.dry_run),
        ),
        payload=payload,
        payload_summary=payload_summary,
        status="queued",
        execution_mode="execute",
    )


def _wake_channel_entry_external_effect_job(
    job_id: Any,
    *,
    effect_type: str,
    reason: str,
    adapter_registry: ExternalEffectAdapterRegistry | None = None,
) -> bool:
    return wake_external_effect_job(
        job_id,
        reason=reason,
        effect_type=effect_type,
        adapter_registry=adapter_registry,
    )


def _channel_entry_target(command: ProcessChannelEntryCommand) -> tuple[str, str, dict[str, Any]]:
    unionid = text(command.unionid)
    if unionid:
        return "unionid", unionid, {"target_unionid": unionid}
    return "external_userid", text(command.external_contact_id), {}


def _relationship_epoch_key(command: ProcessChannelEntryCommand) -> str:
    """Return the durable relationship event identity for effect deduplication.

    WeCom retries of the same callback resolve to the same event-log row, while
    deleting and re-adding a contact creates a new row.  Keep the historical
    key shape for non-callback/manual commands that have no durable event row.
    """

    event_log_id = int(command.event_log_id or 0)
    return f":relationship_event:{event_log_id}" if event_log_id > 0 else ""


def _emit_channel_entry_internal_event(
    command: ProcessChannelEntryCommand,
    *,
    channel_id: int,
    scene: str,
    channel_contact: dict[str, Any],
) -> dict[str, Any]:
    unionid = text(command.unionid)
    if not unionid:
        return {"ok": False, "reason": "identity_pending_unionid", "deferred": True}
    try:
        result = InternalEventService().emit_event(
            event_type="channel_entry.entered",
            aggregate_type="automation_channel",
            aggregate_id=str(channel_id),
            subject_type="unionid",
            subject_id=unionid,
            idempotency_key=f"channel_entry:{command.event_log_id or channel_id}:{unionid}:{scene}",
            source_module="channel_entry.application",
            source_command_id=str(command.event_log_id or ""),
            payload={
                "source_type": "channel_entry",
                "source_key": f"channel:{channel_id}",
                "channel_id": channel_id,
                "scene_value": scene,
                "unionid": unionid,
                "external_userid": command.external_contact_id,
                "owner_userid": command.follow_user_userid,
                "channel_contact_id": channel_contact.get("id"),
                "payload": repo.json_safe(command.payload_json or {}),
            },
            payload_summary={
                "channel_id": channel_id,
                "scene_value": scene,
                "unionid": unionid,
            },
            context=CommandContext(
                actor_id=text(command.follow_user_userid) or "channel_entry",
                actor_type="system",
                request_id=str(command.event_log_id or ""),
                trace_id=f"channel-entry-{command.event_log_id}" if command.event_log_id else "",
                source_route="channel_entry.process_channel_entry",
            ),
        )
        return {"ok": True, "event_id": text((result.get("event") or {}).get("event_id")), "consumer_run_count": len(result.get("consumer_runs") or [])}
    except Exception as exc:
        safe_log_exception(LOGGER, "channel entry internal event emit failed", exc, level=logging.WARNING)
        return {"ok": False, "error": str(exc)}


def process_channel_entry_canonical(
    command: ProcessChannelEntryCommand,
    *,
    channel_id: int,
    scene: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not text(command.unionid):
        return (
            {"attempted": False, "deferred": True, "reason": "identity_pending_unionid"},
            {"ok": False, "deferred": True, "reason": "identity_pending_unionid"},
        )
    corp_id = extract_corp_id(command.payload_json)
    channel_contact = repo.upsert_channel_contact(
        channel_id=channel_id,
        unionid=command.unionid,
        external_contact_id=command.external_contact_id,
        owner_staff_id=command.follow_user_userid,
        source_payload=command.payload_json,
    )
    channel_entry_event = _emit_channel_entry_internal_event(
        command,
        channel_id=channel_id,
        scene=scene,
        channel_contact=channel_contact,
    )
    _log_effect(
        command,
        effect_type="channel_contact",
        idempotency_key=f"{corp_id}:{command.external_contact_id}:{command.follow_user_userid}:{channel_id}:contact",
        status="success",
        channel_id=channel_id,
        scene_value=scene,
        reason="upserted",
        response_json=channel_contact,
    )
    return channel_contact, channel_entry_event


def process_channel_entry_runtime(
    command: ProcessChannelEntryCommand,
    *,
    channel_id: int,
    scene: str,
    identity_status: str = "pending",
) -> dict[str, Any]:
    corp_id = extract_corp_id(command.payload_json)
    if command.dry_run:
        return {
            "planned": True,
            "deferred": True,
            "channel_id": channel_id,
            "external_userid": command.external_contact_id,
            "reason": "identity_pending_unionid",
        }
    runtime_entry = repo.upsert_channel_entry_runtime(
        corp_id=corp_id,
        event_log_id=command.event_log_id,
        channel_id=channel_id,
        scene_value=scene,
        external_userid=command.external_contact_id,
        follow_user_userid=command.follow_user_userid,
        welcome_code_present=bool(extract_welcome_code(command.payload_json)),
        unionid=command.unionid,
        identity_status=identity_status,
        runtime_status="received",
        payload_json=repo.json_safe(command.payload_json or {}),
        enqueue_identity_resolution=True,
        identity_resolution_reason="identity_pending_unionid",
    )
    runtime_entry.setdefault(
        "identity_resolution_queue",
        {"ok": False, "reason": "identity_resolution_enqueue_missing"},
    )
    _log_effect(
        command,
        effect_type="channel_entry_runtime",
        idempotency_key=f"{corp_id}:{command.external_contact_id}:{command.follow_user_userid}:{channel_id}:runtime",
        status="success",
        channel_id=channel_id,
        scene_value=scene,
        reason="runtime_entry_recorded",
        response_json=runtime_entry,
    )
    return runtime_entry


def _adapter_failure(exc: Exception) -> tuple[str, dict[str, Any]]:
    if isinstance(exc, WeComAdapterBlocked):
        payload: dict[str, Any] = {"reason": exc.reason}
        if exc.missing_config:
            payload["missing_config"] = exc.missing_config
        return exc.reason, payload
    if isinstance(exc, WeComApiError):
        payload = {"reason": "wecom_api_error", "message": exc.message}
        if exc.payload:
            payload["wecom_result"] = exc.payload
        return "wecom_api_error", payload
    return "wecom_api_error", {"reason": "wecom_api_error", "message": str(exc)}


def _identity_sync_error_fields(identity_sync: dict[str, Any]) -> tuple[str, str]:
    status = text(identity_sync.get("status"))
    if status in {"success", "skipped"}:
        return "", ""
    wecom_result = identity_sync.get("wecom_result")
    if not isinstance(wecom_result, dict):
        wecom_result = {}
    error_code = text(wecom_result.get("errcode")) or text(identity_sync.get("reason")) or status
    error_message = (
        text(wecom_result.get("errmsg"))
        or text(identity_sync.get("message"))
        or text(identity_sync.get("reason"))
        or status
    )
    return error_code, error_message


def _record_identity_sync_result(event_log_id: int | None, identity_sync: dict[str, Any]) -> dict[str, Any]:
    if not event_log_id:
        return {"ok": False, "reason": "event_log_id_missing"}
    error_code, error_message = _identity_sync_error_fields(identity_sync)
    try:
        repo.record_identity_sync_result(
            int(event_log_id),
            status=text(identity_sync.get("status")),
            error_code=error_code,
            error_message=error_message,
            response_json=repo.json_safe(identity_sync),
        )
    except Exception as exc:
        safe_log_exception(LOGGER, "identity sync diagnostic persistence failed", exc, level=logging.WARNING)
        return {"ok": False, "reason": "diagnostic_persist_failed", "message": str(exc)}
    return {"ok": True}


def _mark_runtime_identity_from_sync(
    event: dict[str, Any],
    *,
    corp_id: str,
    event_log_id: int | None,
    identity_sync: dict[str, Any],
) -> dict[str, Any]:
    try:
        return repo.mark_channel_entry_runtime_identity(
            event_log_id=event_log_id,
            corp_id=corp_id,
            scene_value=extract_scene(event),
            external_userid=text(event.get("ExternalUserID")),
            follow_user_userid=text(event.get("UserID")),
            unionid=text(identity_sync.get("unionid")),
            identity_status=text(identity_sync.get("status")) or "pending",
            identity_sync=identity_sync,
        )
    except Exception as exc:
        safe_log_exception(LOGGER, "channel entry runtime identity update failed", exc, level=logging.WARNING)
        return {"status": "failed", "reason": str(exc)}


def _canonicalize_channel_entry_after_identity(
    event: dict[str, Any],
    *,
    corp_id: str,
    event_log_id: int | None,
    identity_sync: dict[str, Any],
    persist_runtime_identity: bool = True,
) -> dict[str, Any]:
    unionid = text(identity_sync.get("unionid"))
    scene = extract_scene(event)
    external_userid = text(event.get("ExternalUserID"))
    if not unionid:
        return {"status": "skipped", "reason": "unionid_missing"}
    if not scene or not external_userid:
        return {"status": "skipped", "reason": "missing_state_or_external_userid"}
    channel, match = resolve_channel_for_scene(scene_value=scene, corp_id=corp_id, persist_alias=True)
    if not channel:
        return {"status": "skipped", "reason": text(match.get("reason")) or "channel_not_found", "scene_match": match}
    if not channel_enabled(channel):
        return {"status": "skipped", "reason": "channel_disabled", "scene_match": match}
    channel_id = int(channel.get("id") or 0)
    canonical_command = ProcessChannelEntryCommand(
        unionid=unionid,
        external_contact_id=external_userid,
        payload_json={**event, "corp_id": corp_id, "unionid": unionid},
        follow_user_userid=text(event.get("UserID")) or text(channel.get("owner_staff_id")) or "HuangYouCan",
        event_action=text(event.get("ChangeType")),
        send_welcome_message=False,
        event_log_id=event_log_id,
    )
    channel_contact, channel_entry_event = process_channel_entry_canonical(
        canonical_command,
        channel_id=channel_id,
        scene=scene,
    )
    runtime_identity = (
        _mark_runtime_identity_from_sync(
            event,
            corp_id=corp_id,
            event_log_id=event_log_id,
            identity_sync=identity_sync,
        )
        if persist_runtime_identity
        else {"status": "skipped", "reason": "backfill_worker_owns_runtime_identity"}
    )
    return {
        "status": "success",
        "scene_match": match,
        "channel_contact": channel_contact,
        "channel_entry_internal_event": channel_entry_event,
        "runtime_identity": runtime_identity,
    }


def _sync_identity_best_effort(
    event: dict[str, Any],
    *,
    corp_id: str,
    event_log_id: int | None,
    persist_runtime_identity: bool = True,
) -> dict[str, Any]:
    try:
        identity_sync = sync_external_contact_identity_for_event(event, corp_id=corp_id)
    except Exception as exc:
        reason, failure = _adapter_failure(exc)
        identity_sync = {"status": "failed", "reason": reason, **failure}
    diagnostic = _record_identity_sync_result(event_log_id, identity_sync)
    if diagnostic:
        identity_sync["diagnostic"] = diagnostic
    if text(identity_sync.get("status")) == "success":
        try:
            canonical = _canonicalize_channel_entry_after_identity(
                event,
                corp_id=corp_id,
                event_log_id=event_log_id,
                identity_sync=identity_sync,
                persist_runtime_identity=persist_runtime_identity,
            )
            identity_sync["channel_entry_canonical"] = canonical
        except Exception as exc:
            safe_log_exception(LOGGER, "channel entry canonicalization after identity failed", exc, level=logging.WARNING)
            identity_sync["channel_entry_canonical"] = {"status": "failed", "reason": str(exc)}
    else:
        identity_sync["runtime_identity"] = (
            _mark_runtime_identity_from_sync(
                event,
                corp_id=corp_id,
                event_log_id=event_log_id,
                identity_sync=identity_sync,
            )
            if persist_runtime_identity
            else {"status": "skipped", "reason": "backfill_worker_owns_runtime_identity"}
        )
    return identity_sync


def _plan_identity_resolution_for_event(
    event: dict[str, Any],
    *,
    corp_id: str,
    event_log_id: int | None,
) -> dict[str, Any]:
    """Persist provider work only; callback processing never calls WeCom here."""

    try:
        planned = repo.enqueue_channel_entry_identity_resolution(
            corp_id=corp_id,
            external_userid=text(event.get("ExternalUserID")),
            follow_user_userid=text(event.get("UserID")),
            payload_json=repo.json_safe(event),
            reason="callback_identity_resolution_required",
            event_log_id=event_log_id,
        )
    except Exception as exc:
        safe_log_exception(LOGGER, "callback identity resolution effect planning failed", exc, level=logging.WARNING)
        return {
            "status": "failed",
            "reason": "identity_resolution_effect_planning_failed",
            "message": str(exc),
            "real_external_call_executed": False,
        }
    return {
        "status": "queued" if planned.get("external_effect_job_id") else "held",
        "reason": "external_contact_detail_effect_queued" if planned.get("external_effect_job_id") else text(planned.get("reason")),
        **planned,
        "real_external_call_executed": False,
    }


def _welcome_attachments(channel: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    attachments: list[dict[str, Any]] = []
    for key, msgtype in (
        ("welcome_image_library_ids", "image"),
        ("welcome_attachment_library_ids", "file"),
        ("welcome_miniprogram_library_ids", "miniprogram"),
        ("welcome_group_invite_library_ids", "link"),
    ):
        raw = channel.get(key) or []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = []
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, dict):
                if item.get("missing") or item.get("exists") is False:
                    return attachments, "material_resolve_failed"
                attachment = {"msgtype": msgtype}
                if msgtype == "miniprogram":
                    required = ("appid", "page", "title", "pic_media_id")
                    if any(not text(item.get(field)) for field in required):
                        return attachments, "material_resolve_failed"
                    attachment.update({field: text(item.get(field)) for field in required})
                elif msgtype == "link":
                    title = text(item.get("title") or item.get("name"))
                    url = text(item.get("url") or item.get("join_url"))
                    if not title or not url:
                        return attachments, "material_resolve_failed"
                    attachment["link"] = {"title": title, "url": url}
                else:
                    media_id = text(item.get("media_id") or item.get("material_id") or item.get("pic_media_id"))
                    if not media_id:
                        return attachments, "material_resolve_failed"
                    attachment["media_id"] = media_id
                attachments.append(attachment)
                continue
            try:
                material_id = int(item or 0)
            except (TypeError, ValueError):
                return attachments, "material_resolve_failed"
            if material_id <= 0:
                return attachments, "material_resolve_failed"
            attachments.append({"msgtype": msgtype, "material_id": material_id})
    if len(attachments) > 9:
        return attachments, "attachment_limit_exceeded"
    return attachments, ""


def _resolve_welcome_customer_name(command: ProcessChannelEntryCommand) -> str:
    try:
        return text(
            repo.resolve_external_contact_customer_name(
                command.external_contact_id,
                corp_id=extract_corp_id(command.payload_json),
            )
        )
    except Exception:
        return ""


def _render_welcome_message_template(message: Any, command: ProcessChannelEntryCommand) -> str:
    rendered = text(message)
    if not CUSTOMER_NAME_PLACEHOLDER_RE.search(rendered):
        return rendered
    return CUSTOMER_NAME_PLACEHOLDER_RE.sub(_resolve_welcome_customer_name(command), rendered)


def _welcome_callback_received_at(command: ProcessChannelEntryCommand) -> datetime | None:
    # Historical repair is never allowed to replay a one-shot WelcomeCode.
    if text(command.event_action) == "repair_channel_entry":
        return None
    received_at = utc_datetime(command.callback_received_at)
    if received_at is not None:
        return received_at
    create_time = text(command.payload_json.get("CreateTime"))
    if create_time:
        try:
            return datetime.fromtimestamp(int(create_time), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None
    # Direct command callers are immediate by definition.
    return datetime.now(timezone.utc)


def _send_welcome(
    command: ProcessChannelEntryCommand,
    *,
    channel: dict[str, Any],
    scene: str,
    adapter_registry: ExternalEffectAdapterRegistry | None = None,
) -> dict[str, Any]:
    channel_id = int(channel.get("id") or 0)
    welcome_code = extract_welcome_code(command.payload_json)
    key = f"{extract_corp_id(command.payload_json)}:{command.external_contact_id}:{command.follow_user_userid}:{welcome_code}:welcome"
    if effect_status_for_duplicate(repo.get_channel_entry_effect_log("welcome_message", key)):
        return {"attempted": False, "sent": False, "reason": "idempotent_success_exists", "welcome_code": welcome_code}
    attachments, attachment_error = _welcome_attachments(channel)
    if attachment_error:
        result = {"attempted": True, "sent": False, "reason": attachment_error, "attachments": attachments}
        _log_effect(command, effect_type="welcome_message", idempotency_key=key, status="failed", channel_id=channel_id, scene_value=scene, reason=attachment_error, response_json=result)
        return result
    text_content = _render_welcome_message_template(channel.get("welcome_message"), command)
    if not text_content and not attachments:
        result = {"attempted": False, "sent": False, "reason": "no_welcome_message_configured"}
        _log_effect(command, effect_type="welcome_message", idempotency_key=key, status="skipped", channel_id=channel_id, scene_value=scene, reason=result["reason"], response_json=result)
        return result
    if not welcome_code:
        result = {"attempted": True, "sent": False, "reason": "welcome_code_missing"}
        _log_effect(command, effect_type="welcome_message", idempotency_key=key, status="skipped", channel_id=channel_id, scene_value=scene, reason=result["reason"], response_json=result)
        return result
    if not command.send_welcome_message:
        result = {"attempted": False, "sent": False, "reason": "send_welcome_message_disabled"}
        _log_effect(command, effect_type="welcome_message", idempotency_key=key, status="skipped", channel_id=channel_id, scene_value=scene, reason=result["reason"], response_json=result)
        return result
    payload: dict[str, Any] = {"welcome_code": welcome_code}
    if text_content:
        payload["text"] = {"content": text_content}
    if attachments:
        payload["attachments"] = attachments
    callback_received_at = _welcome_callback_received_at(command)
    if callback_received_at is None:
        result = {
            "attempted": False,
            "sent": False,
            "expired": True,
            "reason": "welcome_deadline_missing_no_replay",
            "welcome_code": welcome_code,
        }
        _log_effect(command, effect_type="welcome_message", idempotency_key=key, status="skipped", channel_id=channel_id, scene_value=scene, reason=result["reason"], request_json=payload, response_json=result)
        return result
    provider_deadline_at = welcome_provider_deadline(callback_received_at)
    if provider_deadline_at <= datetime.now(timezone.utc):
        result = {
            "attempted": False,
            "sent": False,
            "expired": True,
            "reason": "welcome_provider_deadline_elapsed",
            "welcome_code": welcome_code,
            "callback_received_at": callback_received_at.isoformat(),
            "provider_deadline_at": provider_deadline_at.isoformat(),
        }
        _log_effect(command, effect_type="welcome_message", idempotency_key=key, status="skipped", channel_id=channel_id, scene_value=scene, reason=result["reason"], request_json=payload, response_json=result)
        return result
    if command.dry_run:
        return {"attempted": False, "sent": False, "reason": "dry_run", "request_payload": payload}
    target_type, target_id, target_payload = _channel_entry_target(command)
    try:
        graph = build_welcome_effect_graph_repository().plan(
            WelcomeEffectGraphRequest(
                idempotency_key=f"channel_entry:{key}",
                channel_id=channel_id,
                corp_id=extract_corp_id(command.payload_json),
                external_userid=command.external_contact_id,
                follow_user_userid=command.follow_user_userid,
                welcome_code=welcome_code,
                target_type=target_type,
                target_id=target_id,
                target_payload=target_payload,
                text_content=text_content,
                attachments=tuple(attachments),
                actor_id=text(command.operator_id or command.follow_user_userid),
                source_event_id=str(command.event_log_id or ""),
                scene_value=scene,
                callback_received_at=callback_received_at,
                provider_deadline_at=provider_deadline_at,
            )
        )
    except Exception as exc:
        result = {"attempted": True, "sent": False, "reason": "external_effect_queue_failed", "welcome_code": welcome_code, "message": str(exc)}
        _log_effect(command, effect_type="welcome_message", idempotency_key=key, status="failed", channel_id=channel_id, scene_value=scene, reason="external_effect_queue_failed", request_json=payload, response_json=result)
        return result
    final_job_id = int(graph.get("final_effect_job_id") or 0)
    immediate_dispatch_scheduled = False
    if graph.get("status") == "ready":
        immediate_dispatch_scheduled = _wake_channel_entry_external_effect_job(
            final_job_id,
            effect_type=WECOM_WELCOME_MESSAGE_SEND,
            reason="channel_entry_welcome_message",
            adapter_registry=adapter_registry,
        )
    result = {
        "attempted": True,
        "sent": False,
        "queued": True,
        "reason": "external_effect_job_queued",
        "welcome_code": welcome_code,
        "execution_id": graph.get("execution_id"),
        "status_url": graph.get("status_url"),
        "external_effect_job_id": final_job_id,
        "external_effect_job_ids": graph.get("external_effect_job_ids") or [],
        "upload_effect_job_ids": graph.get("upload_effect_job_ids") or [],
        "dependency_status": graph.get("status"),
        "duplicate": bool(graph.get("duplicate")),
        "immediate_dispatch_scheduled": immediate_dispatch_scheduled,
        "immediate_dispatch_mode": "postgres_queue_trigger",
        "fallback_message": {},
        "welcome_effect_cancelled_for_fallback": False,
        "real_external_call_executed": False,
        "attachments": attachments,
    }
    _log_effect(command, effect_type="welcome_message", idempotency_key=key, status="queued", channel_id=channel_id, scene_value=scene, reason="external_effect_job_queued", request_json=payload, response_json=result)
    return result


def _apply_tag(
    command: ProcessChannelEntryCommand,
    *,
    channel: dict[str, Any],
    scene: str,
    adapter_registry: ExternalEffectAdapterRegistry | None = None,
) -> dict[str, Any]:
    channel_id = int(channel.get("id") or 0)
    tag_id = text(channel.get("entry_tag_id"))
    relationship_epoch = _relationship_epoch_key(command)
    key = f"{extract_corp_id(command.payload_json)}:{command.external_contact_id}:{command.follow_user_userid}:{tag_id}:{channel_id}{relationship_epoch}:tag"
    if not tag_id:
        result = {"attempted": False, "applied": False, "reason": "no_entry_tag_configured"}
        _log_effect(command, effect_type="entry_tag", idempotency_key=key, status="skipped", channel_id=channel_id, scene_value=scene, reason=result["reason"], response_json=result)
        return result
    if effect_status_for_duplicate(repo.get_channel_entry_effect_log("entry_tag", key)):
        return {"attempted": False, "applied": False, "reason": "idempotent_success_exists", "entry_tag_id": tag_id}
    payload = {"external_userid": command.external_contact_id, "follow_user_userid": command.follow_user_userid, "add_tags": [tag_id], "remove_tags": []}
    if text(command.unionid):
        payload["target_unionid"] = command.unionid
    if command.dry_run:
        return {"attempted": False, "applied": False, "reason": "dry_run", "request_payload": payload}
    target_type, target_id, target_payload = _channel_entry_target(command)
    try:
        job = _plan_channel_entry_effect(
            command,
            effect_type=WECOM_CONTACT_TAG_MARK,
            adapter_name="wecom_tag",
            operation="mark",
            target_type=target_type,
            target_id=target_id,
            business_id=str(channel_id),
            idempotency_key=key,
            payload={
                **payload,
                **target_payload,
                "channel_id": channel_id,
                "scene_value": scene,
            },
            payload_summary={
                "external_userid": command.external_contact_id,
                "target_type": target_type,
                "target_id": target_id,
                "target_unionid": text(target_payload.get("target_unionid")),
                "follow_user_userid": command.follow_user_userid,
                "channel_id": channel_id,
                "scene_value": scene,
                "tag_ids": [tag_id],
                "operation": "mark",
            },
        )
    except Exception as exc:
        result = {"attempted": True, "applied": False, "reason": "external_effect_queue_failed", "entry_tag_id": tag_id, "message": str(exc)}
        _log_effect(command, effect_type="entry_tag", idempotency_key=key, status="failed", channel_id=channel_id, scene_value=scene, reason="external_effect_queue_failed", request_json=payload, response_json=result)
        return result
    result = {
        "attempted": True,
        "applied": False,
        "queued": True,
        "reason": "external_effect_job_queued",
        "entry_tag_id": tag_id,
        "external_effect_job_id": job.get("id"),
        "immediate_dispatch_scheduled": _wake_channel_entry_external_effect_job(
            job.get("id"),
            effect_type=WECOM_CONTACT_TAG_MARK,
            reason="channel_entry_tag_mark",
            adapter_registry=adapter_registry,
        ),
        "real_external_call_executed": False,
    }
    _log_effect(command, effect_type="entry_tag", idempotency_key=key, status="queued", channel_id=channel_id, scene_value=scene, reason="external_effect_job_queued", request_json=payload, response_json=result)
    return result


def process_channel_entry(
    command: ProcessChannelEntryCommand,
    *,
    external_effect_adapter_registry: ExternalEffectAdapterRegistry | None = None,
) -> dict[str, Any]:
    scene = extract_scene(command.payload_json)
    corp_id = extract_corp_id(command.payload_json)
    channel, match = resolve_channel_for_scene(scene_value=scene, corp_id=corp_id, persist_alias=not command.dry_run)
    if not scene:
        return {"handled": False, "mode": "channel_not_found", "reason": "missing_channel_scene", "scene_match": match}
    if not channel:
        reason = text(match.get("reason")) or "channel_not_found"
        _log_effect(command, effect_type="channel_contact", idempotency_key=f"{corp_id}:{command.external_contact_id}:{scene}:not_found", status="failed", channel_id=None, scene_value=scene, reason=reason, response_json={"scene_match": match})
        return {"handled": False, "mode": "channel_not_found", "reason": reason, "scene_match": match}

    channel_id = int(channel["id"])
    assignment_result: dict[str, Any] = {}
    if not text(command.follow_user_userid) and text(channel.get("assignment_mode")) == "multi_staff":
        try:
            assignment_result = repo.choose_channel_assignee(
                channel_id,
                external_contact_id=command.external_contact_id,
                wecom_user_id="",
                write_event=not command.dry_run,
                source_payload=command.payload_json,
            )
        except Exception as exc:
            assignment_result = {"ok": False, "reason": text(str(exc)) or "assignment_failed"}
    command.follow_user_userid = (
        text(command.follow_user_userid)
        or text(assignment_result.get("assignee_staff_id"))
        or text(channel.get("owner_staff_id"))
        or "HuangYouCan"
    )
    if not channel_enabled(channel):
        for effect_type, response in {
            "channel_contact": {"attempted": False, "reason": "channel_disabled"},
            "welcome_message": {"attempted": False, "sent": False, "reason": "channel_disabled"},
            "entry_tag": {"attempted": False, "applied": False, "reason": "channel_disabled"},
        }.items():
            _log_effect(
                command,
                effect_type=effect_type,
                idempotency_key=f"{corp_id}:{command.external_contact_id}:{channel_id}:{effect_type}:channel_disabled",
                status="skipped",
                channel_id=channel_id,
                scene_value=scene,
                reason="channel_disabled",
                response_json=response,
            )
        return {
            "handled": False,
            "mode": "channel_disabled",
            "reason": "channel_disabled" if text(channel.get("status")) != "revoked" else "channel_revoked",
            "scene_match": match,
            "channel": channel_payload(channel),
            "baseline_effects": {
                "channel_contact": {"attempted": False, "reason": "channel_disabled"},
                "welcome_message": {"attempted": False, "sent": False, "reason": "channel_disabled"},
                "entry_tag": {"attempted": False, "applied": False, "reason": "channel_disabled"},
            },
        }

    has_unionid = bool(text(command.unionid))
    runtime_entry: dict[str, Any] = {}
    if command.dry_run:
        if has_unionid:
            channel_contact = {"planned": True, "channel_id": channel_id, "unionid": command.unionid, "external_contact_id": command.external_contact_id}
            channel_entry_event = {"ok": False, "reason": "dry_run"}
        else:
            runtime_entry = process_channel_entry_runtime(command, channel_id=channel_id, scene=scene)
            channel_contact = {"planned": False, "deferred": True, "reason": "identity_pending_unionid"}
            channel_entry_event = {"ok": False, "deferred": True, "reason": "identity_pending_unionid"}
    elif has_unionid:
        channel_contact, channel_entry_event = process_channel_entry_canonical(command, channel_id=channel_id, scene=scene)
    else:
        runtime_entry = process_channel_entry_runtime(command, channel_id=channel_id, scene=scene)
        channel_contact = {"attempted": False, "deferred": True, "reason": "identity_pending_unionid"}
        channel_entry_event = {"ok": False, "deferred": True, "reason": "identity_pending_unionid"}
        _log_effect(
            command,
            effect_type="channel_contact",
            idempotency_key=f"{corp_id}:{command.external_contact_id}:{command.follow_user_userid}:{channel_id}:contact:identity_pending",
            status="skipped",
            channel_id=channel_id,
            scene_value=scene,
            reason="identity_pending_unionid",
            response_json=channel_contact,
        )

    welcome = _send_welcome(
        command,
        channel=channel,
        scene=scene,
        adapter_registry=external_effect_adapter_registry,
    )
    tag = _apply_tag(
        command,
        channel=channel,
        scene=scene,
        adapter_registry=external_effect_adapter_registry,
    )
    mode = "channel_baseline_only" if has_unionid else "channel_runtime_only"
    reason = "channel_entry_baseline_recorded" if has_unionid else "channel_entry_runtime_recorded"
    return {
        "handled": True,
        "mode": mode,
        "reason": reason,
        "scene_match": match,
        "channel": channel_payload(channel),
        "baseline_effects": {"channel_contact": channel_contact, "welcome_message": welcome, "entry_tag": tag},
        "channel_contact": channel_contact,
        "runtime_entry": runtime_entry,
        "welcome_message": welcome,
        "entry_tag": tag,
        "assignment": assignment_result,
        "channel_entry_internal_event": channel_entry_event,
    }


def process_wecom_external_contact_event(
    command: ProcessWeComExternalContactEventCommand,
    *,
    external_effect_adapter_registry: ExternalEffectAdapterRegistry | None = None,
) -> dict[str, Any]:
    event = command.event_data
    logged = repo.log_external_contact_event(
        corp_id=command.corp_id,
        event_type=text(event.get("Event")),
        change_type=text(event.get("ChangeType")),
        external_userid=text(event.get("ExternalUserID")),
        user_id=text(event.get("UserID")),
        event_time=int(text(event.get("CreateTime")) or 0),
        event_key=_event_key(command.corp_id, event),
        payload_xml=command.payload_xml,
        payload_json=event,
    )
    result = {"handled": False, "event_log": logged}
    try:
        is_entry_event = text(event.get("Event")) == "change_external_contact" and text(event.get("ChangeType")) in ENTRY_CHANGE_TYPES
        if is_entry_event:
            external_userid = text(event.get("ExternalUserID"))
            owner_userid = text(event.get("UserID"))
            if not external_userid or not owner_userid:
                raise ValueError("verified WeCom entry event requires ExternalUserID and UserID")
            ensured = ensure_verified_wecom_customer(
                corp_id=command.corp_id,
                external_userid=external_userid,
                owner_userid=owner_userid,
                source_event_id=str(logged.get("id") or ""),
            )
            result["customer_identity"] = dict(ensured)
            entry_identity_plan: dict[str, Any] = {}
            if text(event.get("State")) and text(event.get("ExternalUserID")):
                entry = process_channel_entry(
                    ProcessChannelEntryCommand(
                        external_contact_id=text(event.get("ExternalUserID")),
                        payload_json={**event, "corp_id": command.corp_id},
                        follow_user_userid=text(event.get("UserID")),
                        event_action=text(event.get("ChangeType")),
                        send_welcome_message=bool(text(event.get("WelcomeCode"))),
                        event_log_id=int(logged.get("id") or 0) or None,
                        callback_received_at=command.callback_received_at,
                    ),
                    external_effect_adapter_registry=external_effect_adapter_registry,
                )
                result.update({"handled": bool(entry.get("handled")), "entry_result": entry})
                runtime_entry = entry.get("runtime_entry") if isinstance(entry.get("runtime_entry"), dict) else {}
                candidate = runtime_entry.get("identity_resolution_queue") if isinstance(runtime_entry, dict) else {}
                if isinstance(candidate, dict):
                    entry_identity_plan = dict(candidate)
            else:
                result["entry_result"] = {
                    "handled": False,
                    "reason": "missing_state_or_external_userid",
                    "state_present": bool(text(event.get("State"))),
                    "external_userid_present": bool(text(event.get("ExternalUserID"))),
                }
            if entry_identity_plan.get("external_effect_job_id"):
                result["identity_sync"] = {
                    "status": "queued",
                    "reason": "external_contact_detail_effect_queued",
                    **entry_identity_plan,
                    "real_external_call_executed": False,
                }
            else:
                result["identity_sync"] = _plan_identity_resolution_for_event(
                    event,
                    corp_id=command.corp_id,
                    event_log_id=int(logged.get("id") or 0) or None,
                )
            repo.mark_event_status(int(logged["id"]), "success")
        elif text(event.get("Event")) == "change_external_contact" and text(event.get("ChangeType")) in {
            "del_external_contact",
            "del_follow_user",
        }:
            external_userid = text(event.get("ExternalUserID"))
            owner_userid = text(event.get("UserID"))
            relationship = close_verified_wecom_relationship(
                corp_id=command.corp_id,
                external_userid=external_userid,
                owner_userid=owner_userid if text(event.get("ChangeType")) == "del_follow_user" else "",
            )
            result.update(
                {
                    "handled": True,
                    "relationship": relationship,
                    "identity_sync": {"status": "skipped", "reason": "relationship_closed"},
                }
            )
            repo.mark_event_status(int(logged["id"]), "success")
        else:
            result["identity_sync"] = {"status": "skipped", "reason": "non_entry_change_type"}
            _record_identity_sync_result(int(logged.get("id") or 0) or None, result["identity_sync"])
            repo.mark_event_status(int(logged["id"]), "success")
    except Exception as exc:
        repo.mark_event_status(int(logged["id"]), "failed", str(exc))
        raise
    return result


def diagnose_channel_runtime(query: DiagnoseChannelRuntimeQuery) -> dict[str, Any]:
    channel = repo.get_channel_by_id(int(query.channel_id or 0)) if int(query.channel_id or 0) > 0 else None
    match: dict[str, Any] = {}
    if not channel and text(query.scene_value):
        channel, match = resolve_channel_for_scene(scene_value=query.scene_value, persist_alias=False)
    elif channel:
        match = scene_match("channel_id", text(channel.get("scene_value")), channel)
    channel_id = int((channel or {}).get("id") or 0)
    aliases = repo.list_channel_scene_aliases(channel_id) if channel_id else []
    effects = repo.list_channel_entry_effect_logs(channel_id=channel_id or None, scene_value=text(query.scene_value), limit=20)
    adapter = wecom_adapter_diagnostics()
    return {
        "ok": True,
        "scene_resolve_path": match,
        "scene_resolve": match,
        "current_scene": text((channel or {}).get("scene_value")),
        "aliases": aliases,
        "channel": channel_payload(channel or {}) if channel else {},
        "channel_status": text((channel or {}).get("status")),
        "welcome_configured": bool(text((channel or {}).get("welcome_message"))),
        "entry_tag_configured": bool(text((channel or {}).get("entry_tag_id"))),
        "recent_wecom_external_contact_event_logs": repo.list_recent_events(text(query.scene_value), limit=20) if text(query.scene_value) else [],
        "recent_automation_channel_entry_effect_log": effects,
        "expected_baseline_effects": {"channel_contact": bool(channel and channel_enabled(channel)), "welcome_message": bool(text((channel or {}).get("welcome_message"))), "entry_tag": bool(text((channel or {}).get("entry_tag_id")))},
        "real_wecom_adapter_enabled": adapter["real_wecom_adapter_enabled"],
        "real_wecom_adapter_reason": adapter["real_wecom_adapter_reason"],
        "can_send_welcome": adapter["can_send_welcome"],
        "can_mark_tag": adapter["can_mark_tag"],
        "can_create_contact_way": adapter["can_create_contact_way"],
        "missing_config": adapter["missing_config"],
        "runtime_route_map": runtime_route_map_payload(),
        "callback_route_owner": "aicrm_next.channels.channel_entry",
        "web_release_sha": current_release_sha(),
        "worker_release_sha": text(startup_environment_setting("WORKER_RELEASE_SHA"))
        or "unknown",
    }


def dry_run_channel_entry(command: ProcessChannelEntryCommand) -> dict[str, Any]:
    command.dry_run = True
    result = process_channel_entry(command)
    result["dry_run"] = True
    result["would_actions"] = result.get("baseline_effects", {})
    baseline = result.get("baseline_effects") or {}
    result["would_send_welcome"] = bool((baseline.get("welcome_message") or {}).get("request_payload"))
    result["would_apply_tag"] = bool((baseline.get("entry_tag") or {}).get("request_payload"))
    return result


def repair_channel_entry(command: RepairChannelEntryCommand) -> dict[str, Any]:
    event = repo.get_external_contact_event_log(int(command.event_log_id or 0)) if int(command.event_log_id or 0) > 0 else None
    corp_id = text(command.corp_id) or callback_config().get("corp_id", "")
    payload = repo.decode_payload_json((event or {}).get("payload_json")) if event else {"State": text(command.scene_value), "ToUserName": corp_id}
    if corp_id and not extract_corp_id(payload):
        payload["ToUserName"] = corp_id
    external = text((event or {}).get("external_userid")) or text(command.external_userid)
    unionid = text(payload.get("unionid") or payload.get("UnionID"))
    owner = text((event or {}).get("user_id"))
    result = process_channel_entry(
        ProcessChannelEntryCommand(
            unionid=unionid,
            external_contact_id=external,
            payload_json=payload,
            follow_user_userid=owner,
            event_action="repair_channel_entry",
            send_welcome_message=bool(extract_welcome_code(payload)),
            event_log_id=int(command.event_log_id or 0) or None,
        )
    )
    if not extract_welcome_code(payload):
        result["welcome_repair"] = {"attempted": False, "reason": "welcome_code_unavailable_or_expired"}
    return result


def _generated_scene_value() -> str:
    from datetime import datetime

    return f"aqr_{datetime.now().strftime('%y%m%d')}_{secrets.token_hex(2)}"


def generate_channel_qrcode(command: GenerateChannelQrCodeCommand) -> dict[str, Any]:
    channel = repo.get_channel_by_id(int(command.channel_id))
    if not channel:
        raise LookupError("channel_not_found")
    if text(channel.get("carrier_type")) == "link" or text(channel.get("channel_type")) == "wecom_customer_acquisition":
        raise ValueError("link_channel_does_not_support_qrcode_generate")
    owner_staff_id = text(command.owner_staff_id) or text(channel.get("owner_staff_id"))
    payload_user_ids: list[str] = []
    if text(channel.get("assignment_mode")) == "multi_staff":
        payload_user_ids = [text(item.get("staff_id")) for item in repo.list_channel_assignees(int(command.channel_id), active_only=True) if text(item.get("staff_id"))]
    if not payload_user_ids and owner_staff_id:
        payload_user_ids = [owner_staff_id]
    if not payload_user_ids:
        raise ValueError("owner_staff_id_required")
    scene_value = text(command.scene_value) or _generated_scene_value()
    previous_scene = text(channel.get("scene_value"))
    corp_id = callback_config().get("corp_id", "")
    payload = {
        "type": 2 if len(payload_user_ids) > 1 else 1,
        "scene": 2,
        "style": 1,
        "skip_verify": bool(command.skip_verify if command.skip_verify is not None else channel.get("auto_accept_friend")),
        "state": scene_value,
        "user": payload_user_ids,
    }
    try:
        wecom_result = get_wecom_adapter().create_contact_way(payload)
    except Exception as exc:
        reason, failure = _adapter_failure(exc)
        repo.upsert_channel_entry_effect_log(
            effect_type="qrcode_generate",
            idempotency_key=f"{corp_id}:{command.channel_id}:{scene_value}:qrcode_generate",
            status="failed",
            channel_id=int(command.channel_id),
            scene_value=scene_value,
            external_contact_id="",
            owner_staff_id=owner_staff_id,
            reason=reason,
            request_json=payload,
            response_json=failure,
        )
        return {
            "ok": False,
            "reason": reason,
            "channel_id": int(command.channel_id),
            "scene_value": scene_value,
            "request_payload": payload,
            **failure,
            "source": "aicrm_next.channels.channel_entry",
            "route_owner": "ai_crm_next",
        }
    config_id = text((wecom_result or {}).get("config_id"))
    qr_url = text((wecom_result or {}).get("qr_code") or (wecom_result or {}).get("qr_url"))
    if not config_id or not qr_url:
        response = {"reason": "wecom_api_error", "wecom_result": dict(wecom_result or {})}
        repo.upsert_channel_entry_effect_log(
            effect_type="qrcode_generate",
            idempotency_key=f"{corp_id}:{command.channel_id}:{scene_value}:qrcode_generate",
            status="failed",
            channel_id=int(command.channel_id),
            scene_value=scene_value,
            external_contact_id="",
            owner_staff_id=owner_staff_id,
            reason="wecom_api_error",
            request_json=payload,
            response_json=response,
        )
        return {
            "ok": False,
            "reason": "wecom_api_error",
            "channel_id": int(command.channel_id),
            "scene_value": scene_value,
            "wecom_result": response["wecom_result"],
            "source": "aicrm_next.channels.channel_entry",
            "route_owner": "ai_crm_next",
        }
    asset = repo.insert_qrcode_asset(
        channel_id=int(command.channel_id),
        scene_value=scene_value,
        config_id=config_id,
        qr_url=qr_url,
        corp_id=corp_id,
        provider_payload_json=dict(wecom_result or {}),
        status="active",
        generation_source="next_create_contact_way",
        created_by=owner_staff_id,
    )
    if asset.get("conflict"):
        repo.upsert_channel_entry_effect_log(
            effect_type="qrcode_generate",
            idempotency_key=f"{corp_id}:{command.channel_id}:{scene_value}:qrcode_generate_conflict",
            status="failed",
            channel_id=int(command.channel_id),
            scene_value=scene_value,
            external_contact_id="",
            owner_staff_id=owner_staff_id,
            reason=text(asset.get("reason")) or "qrcode_asset_scene_channel_conflict",
            request_json=payload,
            response_json=asset,
        )
        return {
            "ok": False,
            "reason": text(asset.get("reason")) or "qrcode_asset_scene_channel_conflict",
            "channel_id": int(command.channel_id),
            "scene_value": scene_value,
            "source": "aicrm_next.channels.channel_entry",
            "route_owner": "ai_crm_next",
        }
    if previous_scene and previous_scene != scene_value:
        repo.upsert_channel_scene_alias(
            channel_id=int(command.channel_id),
            scene_value=previous_scene,
            corp_id=corp_id,
            qr_url=text(channel.get("qr_url")),
            carrier_type=text(channel.get("carrier_type")) or "qrcode",
            status="retired",
            source="next_create_contact_way_previous_scene",
        )
    repo.retire_active_qrcode_assets(int(command.channel_id), except_asset_id=int(asset.get("id") or 0) or None)
    updated = repo.update_channel_qrcode(channel_id=int(command.channel_id), scene_value=scene_value, qr_url=qr_url, config_id=config_id)
    alias = repo.upsert_channel_scene_alias(
        channel_id=int(command.channel_id),
        scene_value=scene_value,
        corp_id=corp_id,
        config_id=config_id,
        qr_url=qr_url,
        carrier_type="qrcode",
        status="active",
        source="next_create_contact_way",
    )
    repo.upsert_channel_entry_effect_log(
        effect_type="qrcode_generate",
        idempotency_key=f"{corp_id}:{command.channel_id}:{scene_value}:qrcode_generate",
        status="success",
        channel_id=int(command.channel_id),
        scene_value=scene_value,
        external_contact_id="",
        owner_staff_id=owner_staff_id,
        reason="created",
        request_json=payload,
        response_json=dict(wecom_result or {}),
    )
    return {
        "ok": True,
        "channel_id": int(command.channel_id),
        "scene_value": scene_value,
        "config_id": config_id,
        "qr_url": qr_url,
        "alias_id": int(alias.get("id") or 0),
        "qrcode_asset_id": int(asset.get("id") or 0),
        "provider_payload_user_count": len(payload_user_ids),
        "channel": channel_payload(updated or {**channel, "scene_value": scene_value, "qr_url": qr_url, "qr_ticket": config_id}),
        "source": "aicrm_next.channels.channel_entry",
        "route_owner": "ai_crm_next",
        "wecom_result": dict(wecom_result or {}),
    }


def runtime_route_map_payload() -> dict[str, Any]:
    return {
        "route_owner": "ai_crm_next",
        "wecom_callback_routes": {
            "/wecom/external-contact/callback": "aicrm_next.channels.channel_entry.api",
            "/api/wecom/events": "aicrm_next.channels.channel_entry.api",
            "/api/admin/channels/{channel_id}/qrcode/generate": "aicrm_next.channels.channel_entry.api",
            "/api/admin/channels/{channel_id}/qrcode/status": "aicrm_next.automation.automation_engine.channels_api",
            "/api/admin/channels/{channel_id}/qrcode/download": "aicrm_next.automation.automation_engine.channels_api",
        },
        "next_live_callback_gateway_enabled": True,
        "callback_async_enabled": "next_task_queue",
        "legacy_callback_fallback_enabled": False,
        "web_release_sha": current_release_sha(),
        "worker_release_sha": text(startup_environment_setting("WORKER_RELEASE_SHA"))
        or "unknown",
    }
