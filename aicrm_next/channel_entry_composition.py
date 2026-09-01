from __future__ import annotations

from functools import partial

from .channels.channel_entry.application import process_wecom_external_contact_event
from .channels.channel_entry.crm_port import ChannelCrmDependencies, configure_channel_crm_port
from .channels.channel_entry.inbox import WeComCallbackInboxWorker
from .crm.customer_tags.projection_port import build_customer_tag_projection_port
from .crm.identity_contact.event_log_port import build_identity_event_log_port
from .crm.identity_contact.resolution_effects import (
    enqueue_channel_entry_identity_resolution_in_connection,
    plan_identity_resolution_effect,
)
from .crm.identity_contact.resolution_queue_port import build_identity_resolution_queue_port
from .crm.identity_contact.resolver import resolve_external_userid_with_dbapi, resolve_identity_with_dbapi
from .crm.identity_contact.oneid_repository import PostgresOneIDService
from .crm.identity_contact.sidebar_authorization import build_sidebar_authorization_service
from .platform.platform_foundation.external_effects.adapters import ExternalEffectAdapterRegistry


def _ensure_verified_wecom_customer(**kwargs):
    result = PostgresOneIDService().ensure_verified_wecom_identity(**kwargs)
    build_sidebar_authorization_service().invalidate(
        corp_id=str(kwargs.get("corp_id") or ""),
        user_id=str(kwargs.get("owner_userid") or ""),
        external_userid=str(kwargs.get("external_userid") or ""),
    )
    return {
        "customer_id": result.customer_id,
        "identity_id": result.identity_id,
        "created": result.created,
        "status": result.status,
    }


def _apply_verified_wecom_detail(**kwargs):
    return PostgresOneIDService().apply_verified_wecom_detail(**kwargs)


def _close_verified_wecom_relationship(**kwargs):
    closed_count = PostgresOneIDService().close_relationship(**kwargs)
    owner_userid = str(kwargs.get("owner_userid") or "")
    if owner_userid:
        build_sidebar_authorization_service().invalidate(
            corp_id=str(kwargs.get("corp_id") or ""),
            user_id=owner_userid,
            external_userid=str(kwargs.get("external_userid") or ""),
        )
    return {"status": "inactive", "closed_count": closed_count}


def configure_channel_crm_dependencies() -> None:
    """Wire the channels-to-CRM public ports at the application boundary."""

    configure_channel_crm_port(
        ChannelCrmDependencies(
            resolve_identity_with_dbapi=resolve_identity_with_dbapi,
            resolve_external_userid_with_dbapi=resolve_external_userid_with_dbapi,
            identity_resolution_queue_factory=build_identity_resolution_queue_port,
            identity_event_log_factory=build_identity_event_log_port,
            customer_tag_projection_factory=build_customer_tag_projection_port,
            plan_identity_resolution_effect=plan_identity_resolution_effect,
            enqueue_channel_entry_identity_resolution_in_connection=(
                enqueue_channel_entry_identity_resolution_in_connection
            ),
            ensure_verified_wecom_customer=_ensure_verified_wecom_customer,
            apply_verified_wecom_detail=_apply_verified_wecom_detail,
            close_verified_wecom_relationship=_close_verified_wecom_relationship,
        )
    )


def build_wecom_callback_inbox_worker_factory(
    *,
    external_effect_adapter_registry: ExternalEffectAdapterRegistry,
):
    configure_channel_crm_dependencies()
    processor = partial(
        process_wecom_external_contact_event,
        external_effect_adapter_registry=external_effect_adapter_registry,
    )
    return partial(WeComCallbackInboxWorker, processor=processor)


__all__ = [
    "build_wecom_callback_inbox_worker_factory",
    "configure_channel_crm_dependencies",
]
