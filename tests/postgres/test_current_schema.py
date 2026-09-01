from __future__ import annotations

import pytest

from aicrm_next.crm.identity_contact.business_references import (
    ONEID_MULTI_REFERENCE_TABLES,
    ONEID_SINGLE_REFERENCE_TABLES,
)


pytestmark = pytest.mark.postgres


CRITICAL_CURRENT_TABLES = {
    "customers",
    "customer_identities",
    "customer_merges",
    "customer_identity_conflicts",
    "contacts",
    "crm_user_identity",
    "customer_list_index_next",
    "customer_timeline_event_next",
    "automation_agents",
    "external_effect_job",
    "internal_event",
    "webhook_inbox",
    "questionnaires",
    "wechat_pay_orders",
    "service_period_entitlements",
    "data_health_snapshot",
    "schema_release_compatibility",
    "config_releases",
    "ai_audience_package",
}


def test_current_schema_contains_each_live_domain(pg_connection) -> None:
    with pg_connection.cursor() as cursor:
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        tables = {row[0] for row in cursor.fetchall()}
    assert CRITICAL_CURRENT_TABLES <= tables


def test_identity_and_queue_columns_match_current_runtime_contract(pg_connection) -> None:
    with pg_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name IN (
                  'crm_user_identity', 'crm_user_identity_resolution_queue',
                  'wecom_external_contact_identity_map', 'wecom_external_contact_follow_users',
                  'sidebar_customer_profile_fields', 'contact_tags', 'questionnaire_submissions',
                  'external_effect_job', 'webhook_inbox'
              )
            """
        )
        columns: dict[str, set[str]] = {}
        for table_name, column_name in cursor.fetchall():
            columns.setdefault(table_name, set()).add(column_name)
    assert {"unionid", "primary_external_userid", "primary_openid"} <= columns["crm_user_identity"]
    assert "customer_id" in columns["crm_user_identity"]
    assert {"customer_id", "identity_id", "enrichment_status"} <= columns["crm_user_identity_resolution_queue"]
    assert {"customer_id", "identity_id"} <= columns["wecom_external_contact_identity_map"]
    assert {"customer_id", "identity_id"} <= columns["wecom_external_contact_follow_users"]
    for table_name in ("sidebar_customer_profile_fields", "contact_tags", "questionnaire_submissions"):
        assert "customer_id" in columns[table_name]
    assert {
        "tenant_id",
        "idempotency_key",
        "lane",
        "lease_token",
        "status",
        "created_release_sha",
        "processed_release_sha",
        "health_classification_code",
    } <= columns["external_effect_job"]
    assert {"idempotency_key", "status", "attempt_count"} <= columns["webhook_inbox"]


def test_business_tables_expose_canonical_oneid_references(pg_connection) -> None:
    table_names = tuple(
        sorted(
            set(ONEID_SINGLE_REFERENCE_TABLES) | set(ONEID_MULTI_REFERENCE_TABLES) | {"questionnaire_submissions", "archived_messages", "wechat_shop_orders"}
        )
    )
    with pg_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ANY(%s)
            """,
            (list(table_names),),
        )
        columns: dict[str, set[str]] = {}
        for table_name, column_name in cursor.fetchall():
            columns.setdefault(table_name, set()).add(column_name)
    for table_name in ONEID_SINGLE_REFERENCE_TABLES:
        assert "customer_id" in columns[table_name], table_name
    for table_name in ONEID_MULTI_REFERENCE_TABLES:
        assert "target_customer_ids_json" in columns[table_name], table_name
    for table_name in ("alipay_pay_orders", "wechat_pay_orders"):
        assert {"customer_id", "payer_identity_id"} <= columns[table_name]
    assert "target_customer_id" in columns["external_effect_job"]
    assert "respondent_identity_id" in columns["questionnaire_submissions"]
    assert "customer_identity_id" in columns["archived_messages"]
    assert {"buyer_identity_id", "buyer_openid"} <= columns["wechat_shop_orders"]


def test_oneid_compatibility_functions_are_installed(pg_connection) -> None:
    expected = {
        "aicrm_customer_root_id",
        "aicrm_customer_id_by_unionid",
        "aicrm_customer_identity_id_by_unionid",
        "aicrm_customer_id_by_wecom_external_userid",
        "aicrm_customer_ids_by_unionids",
        "aicrm_customer_root_ids",
        "aicrm_customer_id_by_identity",
        "aicrm_customer_identity_id",
    }
    with pg_connection.cursor() as cursor:
        cursor.execute("SELECT proname FROM pg_proc WHERE proname = ANY(%s)", (list(expected),))
        found = {row[0] for row in cursor.fetchall()}
    assert found == expected


def test_external_effect_idempotency_is_enforced_by_postgres(pg_connection) -> None:
    with pg_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE schemaname = 'public' AND tablename = 'external_effect_job'
            """
        )
        definitions = [row[0].lower().replace('"', "") for row in cursor.fetchall()]
    assert any("unique" in definition and "tenant_id" in definition and "idempotency_key" in definition for definition in definitions)


def test_product_wecom_tagging_configuration_is_in_current_schema(pg_connection) -> None:
    with pg_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'wechat_pay_products'
              AND column_name = 'wecom_tagging_json'
            """
        )
        row = cursor.fetchone()
    assert row is not None
    assert row[0] == "jsonb"
    assert row[1] == "NO"
    assert "enabled" in str(row[2])


def test_lead_qr_copy_columns_are_owned_by_products_and_questionnaires(pg_connection) -> None:
    with pg_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name, column_name, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name IN ('wechat_pay_products', 'questionnaires')
              AND column_name IN ('lead_qr_title', 'lead_qr_subtitle')
            """
        )
        rows = cursor.fetchall()
    assert {(row[0], row[1]) for row in rows} == {
        ("wechat_pay_products", "lead_qr_title"),
        ("wechat_pay_products", "lead_qr_subtitle"),
        ("questionnaires", "lead_qr_title"),
        ("questionnaires", "lead_qr_subtitle"),
    }
    assert all(row[2] == "NO" and "''" in str(row[3]) for row in rows)
