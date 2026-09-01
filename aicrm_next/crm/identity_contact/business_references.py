from __future__ import annotations


# Tables where one row belongs to one CRM Customer. Provider aliases remain as
# compatibility snapshots; customer_id is the durable business reference.
ONEID_SINGLE_REFERENCE_TABLES = (
    "alipay_pay_orders",
    "wechat_pay_orders",
    "wechat_shop_orders",
    "archived_messages",
    "contacts",
    "customer_list_index_next",
    "customer_detail_snapshot_next",
    "customer_timeline_event_next",
    "customer_recent_message_next",
    "customer_list_index_next_shadow",
    "customer_detail_snapshot_next_shadow",
    "customer_recent_message_next_shadow",
    "class_user_status_current",
    "class_user_status_history",
    "user_ops_pool_current_next",
    "user_ops_do_not_disturb_next",
    "automation_channel_contact",
    "automation_channel_assignment_event",
    "automation_channel_entry_effect_log",
    "automation_channel_entry_runtime",
    "ai_audience_member_current",
    "ai_audience_member_event",
    "segment_member_snapshots",
    "campaign_members",
    "automation_frequency_consumption",
    "automation_agent_run",
    "automation_agent_output",
    "automation_agent_webhook_item",
    "cloud_broadcast_plan_recipients",
    "cloud_broadcast_plan_recipient_messages",
    "commerce_coupon_claims",
    "service_period_entitlements",
    "service_period_events",
    "service_period_huangyoucan_usage_snapshot",
    "ai_audience_hxc_member_usage_projection",
    "user_ops_hxc_dashboard_snapshot",
    "radar_click_events",
    "external_campaign_preparation_recipients",
)

ONEID_MULTI_REFERENCE_TABLES = (
    "user_ops_send_records_next",
    "broadcast_jobs",
    "external_effect_job",
)
