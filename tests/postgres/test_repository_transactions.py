from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from aicrm_next.automation.ops_enrollment.repo import SqlAlchemyUserOpsRepository
from aicrm_next.extensions.commerce.service_period.repo import PostgresServicePeriodRepository
from aicrm_next.platform.shared.postgres_connection import PostgresConnection
from aicrm_next.platform.shared.db_session import get_session_factory, reset_engine_cache_for_tests
from aicrm_next.crm.identity_contact.oneid_repository import PostgresOneIDService, unionid_scope_key


pytestmark = pytest.mark.postgres


def test_postgres_connection_translates_parameters_and_rolls_back(migrated_database_url: str) -> None:
    raw = psycopg.connect(migrated_database_url)
    connection = PostgresConnection(raw)
    try:
        connection.execute("CREATE TEMP TABLE current_transaction_probe (value INTEGER NOT NULL)")
        connection.commit()
        connection.execute("INSERT INTO current_transaction_probe (value) VALUES (?)", (7,))
        connection.rollback()
        row = connection.execute("SELECT COUNT(*) AS count FROM current_transaction_probe").fetchone()
        assert row == {"count": 0}
    finally:
        connection.close()


def test_postgres_connection_commit_is_visible_to_another_session(migrated_database_url: str) -> None:
    first = psycopg.connect(migrated_database_url)
    second = psycopg.connect(migrated_database_url)
    try:
        with first.cursor() as cursor:
            cursor.execute("CREATE TABLE IF NOT EXISTS current_commit_probe (probe_key TEXT PRIMARY KEY)")
            cursor.execute("TRUNCATE current_commit_probe")
            cursor.execute("INSERT INTO current_commit_probe (probe_key) VALUES ('visible')")
        first.commit()
        with second.cursor() as cursor:
            cursor.execute("SELECT probe_key FROM current_commit_probe")
            assert cursor.fetchone()[0] == "visible"
    finally:
        first.rollback()
        second.rollback()
        with first.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS current_commit_probe")
        first.commit()
        first.close()
        second.close()


def test_service_period_grants_openid_only_customer_without_unionid(migrated_database_url: str) -> None:
    product_code = "oneid-openid-only-service-period"
    out_trade_no = "oneid-openid-only-order"
    customer_id = 0
    trade_product_id = 0
    service_product_id = 0
    with psycopg.connect(migrated_database_url) as connection:
        connection.execute("DELETE FROM internal_event_outbox WHERE idempotency_key LIKE 'commerce.product_enrolled:service_period:service_period:oneid-openid-only-order:%'")
        connection.execute("DELETE FROM service_period_events WHERE out_trade_no = %s", (out_trade_no,))
        connection.execute("DELETE FROM service_period_entitlements WHERE last_out_trade_no = %s", (out_trade_no,))
        connection.execute("DELETE FROM service_period_products WHERE link_slug = %s", (product_code,))
        connection.execute("DELETE FROM wechat_pay_products WHERE product_code = %s", (product_code,))
        customer_id = connection.execute(
            "INSERT INTO customers(status, identity_completeness) VALUES ('active', 'single_identity') RETURNING id"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO customer_identities(
                customer_id, provider, identity_type, scope_key, normalized_value,
                assurance_level, status, verified_at, source_type
            ) VALUES (%s, 'wechat', 'openid', 'wechat_app:wx-openid-only-test', 'openid-only-payer',
                      'provider_verified', 'active', NOW(), 'postgres_test')
            """,
            (customer_id,),
        )
        trade_product_id = connection.execute(
            """
            INSERT INTO wechat_pay_products(product_code, name, amount_total, status, enabled)
            VALUES (%s, 'OpenID only service', 9900, 'active', TRUE)
            RETURNING id
            """,
            (product_code,),
        ).fetchone()[0]
        service_product_id = connection.execute(
            """
            INSERT INTO service_period_products(
                trade_product_id, link_slug, membership_config_id, membership_config_name, duration_days
            ) VALUES (%s, %s, 'member-openid-only', 'OpenID only member', 30)
            RETURNING id
            """,
            (trade_product_id, product_code),
        ).fetchone()[0]
        connection.commit()

    try:
        result = PostgresServicePeriodRepository(migrated_database_url).grant_or_renew_from_paid_order(
            order={
                "status": "paid",
                "out_trade_no": out_trade_no,
                "product_code": product_code,
                "customer_id": customer_id,
                "unionid": "",
                "amount_total": 9900,
                "metadata_json": {"payer_identity": {"openid": "openid-only-payer"}},
            }
        )
        assert result["ok"] is True
        assert result["event_type"] == "activated"
        assert result["entitlement"]["customer_id"] == customer_id
        assert result["entitlement"]["unionid"] == ""
        with psycopg.connect(migrated_database_url) as connection:
            row = connection.execute(
                "SELECT customer_id, unionid FROM service_period_entitlements WHERE service_product_id = %s",
                (service_product_id,),
            ).fetchone()
            assert row == (customer_id, "")
    finally:
        with psycopg.connect(migrated_database_url) as connection:
            connection.execute("DELETE FROM internal_event_outbox WHERE subject_type = 'customer' AND subject_id = %s", (str(customer_id),))
            connection.execute("DELETE FROM service_period_events WHERE service_product_id = %s", (service_product_id,))
            connection.execute("DELETE FROM service_period_entitlements WHERE service_product_id = %s", (service_product_id,))
            connection.execute("DELETE FROM service_period_products WHERE id = %s", (service_product_id,))
            connection.execute("DELETE FROM wechat_pay_products WHERE id = %s", (trade_product_id,))
            connection.execute("DELETE FROM customer_identities WHERE customer_id = %s", (customer_id,))
            connection.execute("DELETE FROM customers WHERE id = %s", (customer_id,))
            connection.commit()


def test_user_ops_send_record_repairs_a_sequence_behind_existing_ids(migrated_database_url: str) -> None:
    with psycopg.connect(migrated_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("TRUNCATE user_ops_send_records_next RESTART IDENTITY")
            cursor.execute(
                """
                INSERT INTO user_ops_send_records_next (id, record_key)
                SELECT value, 'sequence_drift_seed_' || value::text
                FROM generate_series(1, 100) AS value
                """
            )
            cursor.execute("ALTER SEQUENCE user_ops_send_records_next_id_seq RESTART WITH 1")
        connection.commit()

    reset_engine_cache_for_tests()
    session = get_session_factory(migrated_database_url)()
    try:
        repository = SqlAlchemyUserOpsRepository(session)
        created = repository.create_or_get_send_record_by_idempotency(
            idempotency_key="sequence-drift-recovery",
            payload={"operator": "current-postgres-test", "content_preview": "sequence recovery"},
        )
        assert created["id"] == 101
        assert created["idempotency_key"] == "sequence-drift-recovery"
    finally:
        session.close()
        reset_engine_cache_for_tests()
        with psycopg.connect(migrated_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("TRUNCATE user_ops_send_records_next RESTART IDENTITY")
            connection.commit()


def _clean_oneid_probe(database_url: str, corp_id: str, *unionids: str) -> None:
    with psycopg.connect(database_url) as connection:
        customer_ids = {
            row[0]
            for row in connection.execute(
                """
                SELECT customer_id FROM customer_identities
                WHERE scope_key = %s OR normalized_value = ANY(%s)
                UNION
                SELECT customer_id FROM wecom_external_contact_identity_map WHERE corp_id = %s
                """,
                (corp_id, list(unionids), corp_id),
            ).fetchall()
        }
        if customer_ids:
            merge_rows = connection.execute(
                "SELECT from_customer_id, to_customer_id FROM customer_merges WHERE from_customer_id = ANY(%s) OR to_customer_id = ANY(%s)",
                (list(customer_ids), list(customer_ids)),
            ).fetchall()
            customer_ids.update(value for row in merge_rows for value in row)
        connection.execute("DELETE FROM crm_user_identity_resolution_queue WHERE corp_id = %s", (corp_id,))
        connection.execute("DELETE FROM automation_channel_entry_runtime WHERE corp_id = %s", (corp_id,))
        connection.execute("DELETE FROM wecom_external_contact_follow_users WHERE corp_id = %s", (corp_id,))
        connection.execute("DELETE FROM wecom_external_contact_identity_map WHERE corp_id = %s", (corp_id,))
        if customer_ids:
            ids = list(customer_ids)
            connection.execute("DELETE FROM sidebar_customer_profile_fields WHERE customer_id = ANY(%s)", (ids,))
            connection.execute("DELETE FROM contact_tags WHERE customer_id = ANY(%s)", (ids,))
            connection.execute("DELETE FROM questionnaire_submissions WHERE customer_id = ANY(%s)", (ids,))
            connection.execute("UPDATE crm_user_identity SET customer_id = NULL WHERE customer_id = ANY(%s)", (ids,))
            connection.execute("DELETE FROM customer_identity_conflicts WHERE left_customer_id = ANY(%s) OR right_customer_id = ANY(%s)", (ids, ids))
            connection.execute("DELETE FROM customer_merges WHERE from_customer_id = ANY(%s) OR to_customer_id = ANY(%s)", (ids, ids))
            connection.execute("DELETE FROM customer_identities WHERE customer_id = ANY(%s)", (ids,))
            connection.execute("DELETE FROM customers WHERE id = ANY(%s)", (ids,))
        connection.commit()


def test_wecom_oneid_ensure_is_concurrency_safe(migrated_database_url: str) -> None:
    corp_id = "corp-oneid-concurrency-test"
    _clean_oneid_probe(migrated_database_url, corp_id)
    service = PostgresOneIDService()
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda _: service.ensure_verified_wecom_identity(
                        corp_id=corp_id,
                        external_userid="external-concurrent",
                        owner_userid="staff-concurrent",
                        source_event_id="event-concurrent",
                    ),
                    range(16),
                )
            )
        assert len({result.customer_id for result in results}) == 1
        assert len({result.identity_id for result in results}) == 1
        assert sum(1 for result in results if result.created) == 1
        with psycopg.connect(migrated_database_url) as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM customer_identities WHERE provider = 'wecom' AND scope_key = %s",
                    (corp_id,),
                ).fetchone()[0]
                == 1
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM wecom_external_contact_follow_users WHERE corp_id = %s AND relation_status = 'active'",
                    (corp_id,),
                ).fetchone()[0]
                == 1
            )
    finally:
        _clean_oneid_probe(migrated_database_url, corp_id)


def test_unionid_customer_survives_conditional_merge_and_conflicts_stay_separate(migrated_database_url: str) -> None:
    merge_corp = "corp-oneid-merge-test"
    conflict_corp = "corp-oneid-conflict-test"
    _clean_oneid_probe(migrated_database_url, merge_corp, "union-merge-probe")
    _clean_oneid_probe(migrated_database_url, conflict_corp, "union-conflict-probe")
    with psycopg.connect(migrated_database_url) as connection:
        connection.execute("DELETE FROM user_ops_send_records_next WHERE record_key = 'oneid-merge-propagation'")
        connection.commit()
    service = PostgresOneIDService()
    scope = unionid_scope_key()
    with psycopg.connect(migrated_database_url) as connection:
        union_customer_id = connection.execute("INSERT INTO customers(status, identity_completeness) VALUES ('active', 'enriched') RETURNING id").fetchone()[0]
        connection.execute(
            """
            INSERT INTO customer_identities(
                customer_id, provider, identity_type, scope_key, normalized_value,
                assurance_level, status, verified_at, source_type
            ) VALUES (%s, 'wechat', 'unionid', %s, 'union-merge-probe',
                      'provider_verified', 'active', NOW(), 'postgres_test')
            """,
            (union_customer_id, scope),
        )
        connection.commit()
    merged_external = service.ensure_verified_wecom_identity(
        corp_id=merge_corp,
        external_userid="external-merge-probe",
        owner_userid="staff-merge-probe",
    )
    with psycopg.connect(migrated_database_url) as connection:
        runtime_customer_id = connection.execute(
            """
            INSERT INTO automation_channel_entry_runtime(corp_id, external_userid, follow_user_userid)
            VALUES (%s, 'external-merge-probe', 'staff-merge-probe')
            RETURNING customer_id
            """,
            (merge_corp,),
        ).fetchone()[0]
        assert runtime_customer_id == merged_external.customer_id
        connection.execute(
            """
            INSERT INTO user_ops_send_records_next(record_key, target_customer_ids_json)
            VALUES ('oneid-merge-propagation', jsonb_build_array(%s))
            """,
            (merged_external.customer_id,),
        )
        connection.commit()
    merged = service.attach_verified_unionid(
        corp_id=merge_corp,
        external_userid="external-merge-probe",
        unionid="union-merge-probe",
        owner_userid="staff-merge-probe",
    )
    assert merged["action"] == "merged"
    assert merged["customer_id"] == union_customer_id
    assert merged["merged_customer_id"] == merged_external.customer_id
    with psycopg.connect(migrated_database_url) as connection:
        assert (
            connection.execute(
                "SELECT customer_id FROM automation_channel_entry_runtime WHERE corp_id = %s",
                (merge_corp,),
            ).fetchone()[0]
            == union_customer_id
        )
        assert connection.execute("SELECT target_customer_ids_json FROM user_ops_send_records_next WHERE record_key = 'oneid-merge-propagation'").fetchone()[
            0
        ] == [union_customer_id]

    first = service.ensure_verified_wecom_identity(
        corp_id=conflict_corp,
        external_userid="external-conflict-a",
        owner_userid="staff-conflict",
    )
    attached = service.attach_verified_unionid(
        corp_id=conflict_corp,
        external_userid="external-conflict-a",
        unionid="union-conflict-probe",
        owner_userid="staff-conflict",
    )
    second = service.ensure_verified_wecom_identity(
        corp_id=conflict_corp,
        external_userid="external-conflict-b",
        owner_userid="staff-conflict",
    )
    conflict = service.attach_verified_unionid(
        corp_id=conflict_corp,
        external_userid="external-conflict-b",
        unionid="union-conflict-probe",
        owner_userid="staff-conflict",
    )
    assert attached["customer_id"] == first.customer_id
    assert conflict["status"] == "conflict"
    assert conflict["customer_id"] == second.customer_id
    assert conflict["unionid_customer_id"] == first.customer_id
    with psycopg.connect(migrated_database_url) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM customer_identity_conflicts WHERE left_customer_id IN (%s, %s) AND right_customer_id IN (%s, %s)",
                (first.customer_id, second.customer_id, first.customer_id, second.customer_id),
            ).fetchone()[0]
            == 1
        )

    with psycopg.connect(migrated_database_url) as connection:
        connection.execute("DELETE FROM user_ops_send_records_next WHERE record_key = 'oneid-merge-propagation'")
        connection.commit()
    _clean_oneid_probe(migrated_database_url, merge_corp, "union-merge-probe")
    _clean_oneid_probe(migrated_database_url, conflict_corp, "union-conflict-probe")


def test_wechat_openid_is_app_scoped_and_unionid_customer_survives_merge(
    migrated_database_url: str,
) -> None:
    openid = "openid-scope-probe"
    unionid = "union-scope-probe"
    app_a = "wx-app-scope-a"
    app_b = "wx-app-scope-b"
    service = PostgresOneIDService()
    try:
        app_a_result = service.ensure_verified_wechat_identity(
            app_id=app_a,
            openid=openid,
            source_type="postgres_test",
        )
        app_b_result = service.ensure_verified_wechat_identity(
            app_id=app_b,
            openid=openid,
            unionid=unionid,
            source_type="postgres_test",
        )
        assert app_a_result["customer_id"] != app_b_result["customer_id"]

        merged = service.ensure_verified_wechat_identity(
            app_id=app_a,
            openid=openid,
            unionid=unionid,
            source_type="postgres_test",
        )
        assert merged["action"] == "merged"
        assert merged["customer_id"] == app_b_result["customer_id"]
        assert merged["merged_customer_id"] == app_a_result["customer_id"]

        with psycopg.connect(migrated_database_url) as connection:
            rows = connection.execute(
                """
                SELECT scope_key, aicrm_customer_root_id(customer_id)
                FROM customer_identities
                WHERE provider = 'wechat' AND identity_type = 'openid'
                  AND normalized_value = %s AND status = 'active'
                ORDER BY scope_key
                """,
                (openid,),
            ).fetchall()
            assert [row[0] for row in rows] == [f"wechat_app:{app_a}", f"wechat_app:{app_b}"]
            assert {row[1] for row in rows} == {app_b_result["customer_id"]}
    finally:
        with psycopg.connect(migrated_database_url) as connection:
            customer_ids = {
                row[0]
                for row in connection.execute(
                    "SELECT customer_id FROM customer_identities WHERE normalized_value = ANY(%s)",
                    ([openid, unionid],),
                ).fetchall()
            }
            merge_ids = (
                {
                    value
                    for row in connection.execute(
                        """
                        SELECT from_customer_id, to_customer_id FROM customer_merges
                        WHERE from_customer_id = ANY(%s) OR to_customer_id = ANY(%s)
                        """,
                        (list(customer_ids), list(customer_ids)),
                    ).fetchall()
                    for value in row
                }
                if customer_ids
                else set()
            )
            customer_ids.update(merge_ids)
            if customer_ids:
                ids = list(customer_ids)
                connection.execute(
                    "DELETE FROM customer_merges WHERE from_customer_id = ANY(%s) OR to_customer_id = ANY(%s)",
                    (ids, ids),
                )
                connection.execute("DELETE FROM customer_identities WHERE customer_id = ANY(%s)", (ids,))
                connection.execute("DELETE FROM customers WHERE id = ANY(%s)", (ids,))
            connection.commit()
