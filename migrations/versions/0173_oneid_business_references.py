"""Add canonical OneID references to active customer business tables.

Revision ID: 0173_oneid_business_references
Revises: 0172_wecom_oneid_core
"""

from __future__ import annotations

from alembic import op


revision = "0173_oneid_business_references"
down_revision = "0172_wecom_oneid_core"
branch_labels = None
depends_on = None


SINGLE_CUSTOMER_TABLES = (
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
)

PAYMENT_ORDER_TABLES = (
    "alipay_pay_orders",
    "wechat_pay_orders",
)

MULTI_CUSTOMER_TABLES = (
    "user_ops_send_records_next",
    "broadcast_jobs",
    "external_effect_job",
)


def upgrade() -> None:
    _expand_identity_completeness()
    _create_resolution_functions()
    for table_name in SINGLE_CUSTOMER_TABLES:
        _add_single_customer_reference(table_name)
    for table_name in PAYMENT_ORDER_TABLES:
        _add_payment_order_references(table_name)
    _add_wecom_runtime_reference()
    for table_name in MULTI_CUSTOMER_TABLES:
        _add_multi_customer_reference(table_name)
    _add_campaign_preparation_recipient_reference()
    _add_external_effect_single_target()
    _add_provider_identity_references()
    _harden_service_period_oneid_uniqueness()
    _create_merge_propagation_trigger()
    op.execute(
        """
        INSERT INTO schema_release_compatibility (
            revision, parent_revision, change_kind, compatibility_epoch,
            previous_runtime_compatible, downgrade_policy, metadata_json
        ) VALUES (
            '0173_oneid_business_references', '0172_wecom_oneid_core', 'expand', 1,
            TRUE, 'forward_only',
            '{"capability":"oneid_business_references","legacy_aliases":"compatibility_snapshots"}'::jsonb
        )
        ON CONFLICT (revision) DO NOTHING
        """
    )


def _expand_identity_completeness() -> None:
    op.execute(
        """
        ALTER TABLE customers DROP CONSTRAINT IF EXISTS customers_identity_completeness_check;
        ALTER TABLE customers
            ADD CONSTRAINT customers_identity_completeness_check
            CHECK (identity_completeness IN ('single_identity', 'wecom_only', 'enriched', 'conflict'));
        """
    )


def _create_resolution_functions() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aicrm_customer_root_id(input_customer_id BIGINT)
        RETURNS BIGINT
        LANGUAGE sql
        STABLE
        AS $$
            WITH RECURSIVE lineage AS (
                SELECT id, merged_into_customer_id, status, 0 AS depth
                FROM customers
                WHERE id = input_customer_id
                UNION ALL
                SELECT customer.id, customer.merged_into_customer_id, customer.status, lineage.depth + 1
                FROM customers customer
                JOIN lineage ON customer.id = lineage.merged_into_customer_id
                WHERE lineage.status = 'merged' AND lineage.depth < 32
            )
            SELECT id
            FROM lineage
            ORDER BY depth DESC
            LIMIT 1
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aicrm_customer_id_by_unionid(input_unionid TEXT)
        RETURNS BIGINT
        LANGUAGE sql
        STABLE
        AS $$
            SELECT MIN(root_customer_id)
            FROM (
                SELECT DISTINCT aicrm_customer_root_id(identity.customer_id) AS root_customer_id
                FROM customer_identities identity
                WHERE identity.provider = 'wechat'
                  AND identity.identity_type = 'unionid'
                  AND identity.normalized_value = BTRIM(COALESCE(input_unionid, ''))
                  AND identity.status = 'active'
            ) candidates
            HAVING COUNT(*) = 1
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aicrm_customer_id_by_identity(
            input_provider TEXT,
            input_identity_type TEXT,
            input_scope_key TEXT,
            input_value TEXT
        )
        RETURNS BIGINT
        LANGUAGE sql
        STABLE
        AS $$
            SELECT aicrm_customer_root_id(identity.customer_id)
            FROM customer_identities identity
            WHERE identity.provider = BTRIM(COALESCE(input_provider, ''))
              AND identity.identity_type = BTRIM(COALESCE(input_identity_type, ''))
              AND identity.scope_key = BTRIM(COALESCE(input_scope_key, ''))
              AND identity.normalized_value = BTRIM(COALESCE(input_value, ''))
              AND identity.status = 'active'
            LIMIT 1
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aicrm_customer_identity_id(
            input_provider TEXT,
            input_identity_type TEXT,
            input_scope_key TEXT,
            input_value TEXT
        )
        RETURNS BIGINT
        LANGUAGE sql
        STABLE
        AS $$
            SELECT identity.id
            FROM customer_identities identity
            WHERE identity.provider = BTRIM(COALESCE(input_provider, ''))
              AND identity.identity_type = BTRIM(COALESCE(input_identity_type, ''))
              AND identity.scope_key = BTRIM(COALESCE(input_scope_key, ''))
              AND identity.normalized_value = BTRIM(COALESCE(input_value, ''))
              AND identity.status = 'active'
            LIMIT 1
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aicrm_customer_identity_id_by_unionid(input_unionid TEXT)
        RETURNS BIGINT
        LANGUAGE sql
        STABLE
        AS $$
            SELECT MIN(identity.id)
            FROM customer_identities identity
            WHERE identity.provider = 'wechat'
              AND identity.identity_type = 'unionid'
              AND identity.normalized_value = BTRIM(COALESCE(input_unionid, ''))
              AND identity.status = 'active'
            HAVING COUNT(DISTINCT identity.scope_key) = 1
               AND COUNT(DISTINCT aicrm_customer_root_id(identity.customer_id)) = 1
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aicrm_customer_id_by_wecom_external_userid(input_external_userid TEXT)
        RETURNS BIGINT
        LANGUAGE sql
        STABLE
        AS $$
            SELECT MIN(root_customer_id)
            FROM (
                SELECT DISTINCT aicrm_customer_root_id(identity.customer_id) AS root_customer_id
                FROM customer_identities identity
                WHERE identity.provider = 'wecom'
                  AND identity.identity_type = 'external_userid'
                  AND identity.normalized_value = BTRIM(COALESCE(input_external_userid, ''))
                  AND identity.status = 'active'
            ) candidates
            HAVING COUNT(*) = 1
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aicrm_customer_ids_by_unionids(input_unionids JSONB)
        RETURNS JSONB
        LANGUAGE sql
        STABLE
        AS $$
            SELECT COALESCE(jsonb_agg(customer_id ORDER BY customer_id), '[]'::jsonb)
            FROM (
                SELECT DISTINCT aicrm_customer_id_by_unionid(value) AS customer_id
                FROM jsonb_array_elements_text(COALESCE(input_unionids, '[]'::jsonb)) AS source(value)
            ) resolved
            WHERE customer_id IS NOT NULL
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aicrm_customer_root_ids(input_customer_ids JSONB)
        RETURNS JSONB
        LANGUAGE sql
        STABLE
        AS $$
            SELECT COALESCE(jsonb_agg(customer_id ORDER BY customer_id), '[]'::jsonb)
            FROM (
                SELECT DISTINCT aicrm_customer_root_id(value::bigint) AS customer_id
                FROM jsonb_array_elements_text(COALESCE(input_customer_ids, '[]'::jsonb)) AS source(value)
                WHERE value ~ '^[0-9]+$'
            ) resolved
            WHERE customer_id IS NOT NULL
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aicrm_sync_customer_reference()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.customer_id IS NOT NULL THEN
                NEW.customer_id := aicrm_customer_root_id(NEW.customer_id);
            ELSIF BTRIM(COALESCE(NEW.unionid, '')) <> '' THEN
                NEW.customer_id := aicrm_customer_id_by_unionid(NEW.unionid);
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aicrm_sync_target_customer_references()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF jsonb_array_length(COALESCE(NEW.target_customer_ids_json, '[]'::jsonb)) > 0 THEN
                NEW.target_customer_ids_json := aicrm_customer_root_ids(NEW.target_customer_ids_json);
            ELSE
                NEW.target_customer_ids_json := aicrm_customer_ids_by_unionids(NEW.target_unionids_json);
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aicrm_sync_external_effect_customer_reference()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF jsonb_array_length(COALESCE(NEW.target_customer_ids_json, '[]'::jsonb)) > 0 THEN
                NEW.target_customer_ids_json := aicrm_customer_root_ids(NEW.target_customer_ids_json);
            ELSE
                NEW.target_customer_ids_json := aicrm_customer_ids_by_unionids(NEW.target_unionids_json);
            END IF;
            IF NEW.target_customer_id IS NOT NULL THEN
                NEW.target_customer_id := aicrm_customer_root_id(NEW.target_customer_id);
            ELSIF COALESCE(NEW.payload_json->>'target_customer_id', NEW.payload_json->>'customer_id', '') ~ '^[0-9]+$' THEN
                NEW.target_customer_id := aicrm_customer_root_id(
                    COALESCE(NEW.payload_json->>'target_customer_id', NEW.payload_json->>'customer_id')::bigint
                );
            ELSIF BTRIM(COALESCE(NEW.target_unionid, '')) <> '' THEN
                NEW.target_customer_id := aicrm_customer_id_by_unionid(NEW.target_unionid);
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aicrm_sync_payment_order_customer_reference()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.customer_id IS NOT NULL THEN
                NEW.customer_id := aicrm_customer_root_id(NEW.customer_id);
            END IF;
            IF NEW.payer_identity_id IS NULL AND BTRIM(COALESCE(NEW.unionid, '')) <> '' THEN
                NEW.payer_identity_id := aicrm_customer_identity_id_by_unionid(NEW.unionid);
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aicrm_sync_campaign_recipient_customer_reference()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.customer_id IS NOT NULL THEN
                NEW.customer_id := aicrm_customer_root_id(NEW.customer_id);
            ELSIF BTRIM(COALESCE(NEW.resolved_unionid, '')) <> '' THEN
                NEW.customer_id := aicrm_customer_id_by_unionid(NEW.resolved_unionid);
            ELSIF BTRIM(COALESCE(NEW.resolved_external_userid, '')) <> '' THEN
                NEW.customer_id := aicrm_customer_id_by_wecom_external_userid(NEW.resolved_external_userid);
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aicrm_sync_wecom_runtime_customer_reference()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.customer_id IS NOT NULL THEN
                NEW.customer_id := aicrm_customer_root_id(NEW.customer_id);
            ELSIF BTRIM(COALESCE(NEW.unionid, '')) <> '' THEN
                NEW.customer_id := aicrm_customer_id_by_unionid(NEW.unionid);
            ELSIF BTRIM(COALESCE(NEW.external_userid, '')) <> '' THEN
                NEW.customer_id := aicrm_customer_id_by_wecom_external_userid(NEW.external_userid);
            END IF;
            RETURN NEW;
        END
        $$
        """
    )


def _add_single_customer_reference(table_name: str) -> None:
    constraint_name = f"fk_{table_name}_customer_id"
    index_name = f"ix_{table_name}_customer_id"
    trigger_name = f"trg_{table_name}_customer_id"
    op.execute(
        f"""
        DO $$
        BEGIN
            IF to_regclass('public.{table_name}') IS NULL THEN
                RETURN;
            END IF;
            ALTER TABLE {table_name}
                ADD COLUMN IF NOT EXISTS customer_id BIGINT;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = '{constraint_name}'
                  AND conrelid = 'public.{table_name}'::regclass
            ) THEN
                ALTER TABLE {table_name}
                    ADD CONSTRAINT {constraint_name}
                    FOREIGN KEY (customer_id) REFERENCES customers(id) NOT VALID;
            END IF;
            UPDATE {table_name}
            SET customer_id = aicrm_customer_id_by_unionid(unionid)
            WHERE customer_id IS NULL AND BTRIM(COALESCE(unionid, '')) <> '';
            CREATE INDEX IF NOT EXISTS {index_name}
                ON {table_name}(customer_id) WHERE customer_id IS NOT NULL;
            DROP TRIGGER IF EXISTS {trigger_name} ON {table_name};
            CREATE TRIGGER {trigger_name}
                BEFORE INSERT OR UPDATE OF customer_id, unionid ON {table_name}
                FOR EACH ROW EXECUTE FUNCTION aicrm_sync_customer_reference();
        END
        $$
        """
    )


def _add_multi_customer_reference(table_name: str) -> None:
    index_name = f"ix_{table_name}_target_customer_ids_json"
    trigger_name = f"trg_{table_name}_target_customer_ids"
    op.execute(
        f"""
        DO $$
        BEGIN
            IF to_regclass('public.{table_name}') IS NULL THEN
                RETURN;
            END IF;
            ALTER TABLE {table_name}
                ADD COLUMN IF NOT EXISTS target_customer_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb;
            UPDATE {table_name}
            SET target_customer_ids_json = aicrm_customer_ids_by_unionids(target_unionids_json);
            CREATE INDEX IF NOT EXISTS {index_name}
                ON {table_name} USING GIN(target_customer_ids_json);
            DROP TRIGGER IF EXISTS {trigger_name} ON {table_name};
            CREATE TRIGGER {trigger_name}
                BEFORE INSERT OR UPDATE OF target_customer_ids_json, target_unionids_json ON {table_name}
                FOR EACH ROW EXECUTE FUNCTION aicrm_sync_target_customer_references();
        END
        $$
        """
    )


def _add_payment_order_references(table_name: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF to_regclass('public.{table_name}') IS NULL THEN
                RETURN;
            END IF;
            ALTER TABLE {table_name}
                ADD COLUMN IF NOT EXISTS customer_id BIGINT,
                ADD COLUMN IF NOT EXISTS payer_identity_id BIGINT;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'fk_{table_name}_customer_id'
                  AND conrelid = 'public.{table_name}'::regclass
            ) THEN
                ALTER TABLE {table_name}
                    ADD CONSTRAINT fk_{table_name}_customer_id
                    FOREIGN KEY (customer_id) REFERENCES customers(id) NOT VALID;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'fk_{table_name}_payer_identity_id'
                  AND conrelid = 'public.{table_name}'::regclass
            ) THEN
                ALTER TABLE {table_name}
                    ADD CONSTRAINT fk_{table_name}_payer_identity_id
                    FOREIGN KEY (payer_identity_id) REFERENCES customer_identities(id) NOT VALID;
            END IF;
            UPDATE {table_name}
            SET payer_identity_id = aicrm_customer_identity_id_by_unionid(unionid)
            WHERE payer_identity_id IS NULL AND BTRIM(COALESCE(unionid, '')) <> '';
            CREATE INDEX IF NOT EXISTS ix_{table_name}_customer_id
                ON {table_name}(customer_id) WHERE customer_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS ix_{table_name}_payer_identity_id
                ON {table_name}(payer_identity_id) WHERE payer_identity_id IS NOT NULL;
            DROP TRIGGER IF EXISTS trg_{table_name}_customer_id ON {table_name};
            CREATE TRIGGER trg_{table_name}_customer_id
                BEFORE INSERT OR UPDATE OF customer_id, payer_identity_id, unionid ON {table_name}
                FOR EACH ROW EXECUTE FUNCTION aicrm_sync_payment_order_customer_reference();
        END
        $$
        """
    )


def _add_wecom_runtime_reference() -> None:
    op.execute(
        """
        ALTER TABLE automation_channel_entry_runtime
            ADD COLUMN IF NOT EXISTS customer_id BIGINT;
        ALTER TABLE automation_channel_entry_runtime
            DROP CONSTRAINT IF EXISTS fk_automation_channel_entry_runtime_customer_id;
        ALTER TABLE automation_channel_entry_runtime
            ADD CONSTRAINT fk_automation_channel_entry_runtime_customer_id
            FOREIGN KEY (customer_id) REFERENCES customers(id) NOT VALID;
        UPDATE automation_channel_entry_runtime
        SET customer_id = COALESCE(
            aicrm_customer_id_by_unionid(unionid),
            aicrm_customer_id_by_wecom_external_userid(external_userid)
        )
        WHERE customer_id IS NULL;
        CREATE INDEX IF NOT EXISTS ix_automation_channel_entry_runtime_customer_id
            ON automation_channel_entry_runtime(customer_id) WHERE customer_id IS NOT NULL;
        DROP TRIGGER IF EXISTS trg_automation_channel_entry_runtime_customer_id
            ON automation_channel_entry_runtime;
        CREATE TRIGGER trg_automation_channel_entry_runtime_customer_id
            BEFORE INSERT OR UPDATE OF customer_id, unionid, external_userid
            ON automation_channel_entry_runtime
            FOR EACH ROW EXECUTE FUNCTION aicrm_sync_wecom_runtime_customer_reference();
        """
    )


def _add_campaign_preparation_recipient_reference() -> None:
    op.execute(
        """
        ALTER TABLE external_campaign_preparation_recipients
            ADD COLUMN IF NOT EXISTS customer_id BIGINT;
        ALTER TABLE external_campaign_preparation_recipients
            DROP CONSTRAINT IF EXISTS fk_external_campaign_preparation_recipients_customer_id;
        ALTER TABLE external_campaign_preparation_recipients
            ADD CONSTRAINT fk_external_campaign_preparation_recipients_customer_id
            FOREIGN KEY (customer_id) REFERENCES customers(id) NOT VALID;
        UPDATE external_campaign_preparation_recipients
        SET customer_id = COALESCE(
            aicrm_customer_id_by_unionid(resolved_unionid),
            aicrm_customer_id_by_wecom_external_userid(resolved_external_userid)
        )
        WHERE customer_id IS NULL;
        CREATE INDEX IF NOT EXISTS ix_external_campaign_preparation_recipients_customer_id
            ON external_campaign_preparation_recipients(customer_id) WHERE customer_id IS NOT NULL;
        DROP TRIGGER IF EXISTS trg_external_campaign_preparation_recipients_customer_id
            ON external_campaign_preparation_recipients;
        CREATE TRIGGER trg_external_campaign_preparation_recipients_customer_id
            BEFORE INSERT OR UPDATE OF customer_id, resolved_unionid, resolved_external_userid
            ON external_campaign_preparation_recipients
            FOR EACH ROW EXECUTE FUNCTION aicrm_sync_campaign_recipient_customer_reference();
        """
    )


def _add_external_effect_single_target() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.external_effect_job') IS NULL THEN
                RETURN;
            END IF;
            ALTER TABLE external_effect_job
                ADD COLUMN IF NOT EXISTS target_customer_id BIGINT;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'fk_external_effect_job_target_customer_id'
                  AND conrelid = 'public.external_effect_job'::regclass
            ) THEN
                ALTER TABLE external_effect_job
                    ADD CONSTRAINT fk_external_effect_job_target_customer_id
                    FOREIGN KEY (target_customer_id) REFERENCES customers(id) NOT VALID;
            END IF;
            UPDATE external_effect_job
            SET target_customer_id = aicrm_customer_id_by_unionid(target_unionid)
            WHERE target_customer_id IS NULL AND BTRIM(COALESCE(target_unionid, '')) <> '';
            CREATE INDEX IF NOT EXISTS ix_external_effect_job_target_customer_id
                ON external_effect_job(target_customer_id) WHERE target_customer_id IS NOT NULL;
            DROP TRIGGER IF EXISTS trg_external_effect_job_customer_id ON external_effect_job;
            CREATE TRIGGER trg_external_effect_job_customer_id
                BEFORE INSERT OR UPDATE OF target_customer_id, target_customer_ids_json,
                    target_unionid, target_unionids_json
                ON external_effect_job
                FOR EACH ROW EXECUTE FUNCTION aicrm_sync_external_effect_customer_reference();
        END
        $$
        """
    )


def _add_provider_identity_references() -> None:
    op.execute(
        """
        ALTER TABLE questionnaire_submissions
            ADD COLUMN IF NOT EXISTS respondent_identity_id BIGINT;
        ALTER TABLE questionnaire_submissions
            DROP CONSTRAINT IF EXISTS fk_questionnaire_submissions_respondent_identity_id;
        ALTER TABLE questionnaire_submissions
            ADD CONSTRAINT fk_questionnaire_submissions_respondent_identity_id
            FOREIGN KEY (respondent_identity_id) REFERENCES customer_identities(id) NOT VALID;
        CREATE INDEX IF NOT EXISTS ix_questionnaire_submissions_respondent_identity_id
            ON questionnaire_submissions(respondent_identity_id)
            WHERE respondent_identity_id IS NOT NULL;

        ALTER TABLE archived_messages
            ADD COLUMN IF NOT EXISTS customer_identity_id BIGINT;
        ALTER TABLE archived_messages
            DROP CONSTRAINT IF EXISTS fk_archived_messages_customer_identity_id;
        ALTER TABLE archived_messages
            ADD CONSTRAINT fk_archived_messages_customer_identity_id
            FOREIGN KEY (customer_identity_id) REFERENCES customer_identities(id) NOT VALID;
        CREATE INDEX IF NOT EXISTS ix_archived_messages_customer_identity_id
            ON archived_messages(customer_identity_id)
            WHERE customer_identity_id IS NOT NULL;

        ALTER TABLE wechat_shop_orders
            ADD COLUMN IF NOT EXISTS buyer_identity_id BIGINT,
            ADD COLUMN IF NOT EXISTS buyer_openid TEXT NOT NULL DEFAULT '';
        ALTER TABLE wechat_shop_orders
            DROP CONSTRAINT IF EXISTS fk_wechat_shop_orders_buyer_identity_id;
        ALTER TABLE wechat_shop_orders
            ADD CONSTRAINT fk_wechat_shop_orders_buyer_identity_id
            FOREIGN KEY (buyer_identity_id) REFERENCES customer_identities(id) NOT VALID;
        CREATE INDEX IF NOT EXISTS ix_wechat_shop_orders_buyer_identity_id
            ON wechat_shop_orders(buyer_identity_id)
            WHERE buyer_identity_id IS NOT NULL;
        """
    )


def _harden_service_period_oneid_uniqueness() -> None:
    op.execute(
        """
        ALTER TABLE service_period_entitlements
            DROP CONSTRAINT IF EXISTS service_period_entitlements_tenant_id_service_product_id_unionid_key;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_service_period_entitlements_customer
            ON service_period_entitlements(tenant_id, service_product_id, customer_id)
            WHERE customer_id IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_service_period_entitlements_unionid_compat
            ON service_period_entitlements(tenant_id, service_product_id, unionid)
            WHERE customer_id IS NULL AND BTRIM(unionid) <> '';
        """
    )


def _create_merge_propagation_trigger() -> None:
    single_tables = (
        *SINGLE_CUSTOMER_TABLES,
        *PAYMENT_ORDER_TABLES,
        "automation_channel_entry_runtime",
        "external_campaign_preparation_recipients",
    )
    single_updates = "\n".join(
        f"UPDATE {table_name} SET customer_id = NEW.to_customer_id WHERE customer_id = NEW.from_customer_id;" for table_name in single_tables
    )
    multi_updates = "\n".join(
        f"""
        UPDATE {table_name} target
        SET target_customer_ids_json = (
            SELECT COALESCE(jsonb_agg(customer_id ORDER BY customer_id), '[]'::jsonb)
            FROM (
                SELECT DISTINCT CASE
                    WHEN value::bigint = NEW.from_customer_id THEN NEW.to_customer_id
                    ELSE value::bigint
                END AS customer_id
                FROM jsonb_array_elements_text(target.target_customer_ids_json) source(value)
            ) normalized
        )
        WHERE target.target_customer_ids_json @> jsonb_build_array(NEW.from_customer_id);
        """
        for table_name in MULTI_CUSTOMER_TABLES
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION aicrm_propagate_customer_merge()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            {single_updates}
            {multi_updates}
            UPDATE external_effect_job
            SET target_customer_id = NEW.to_customer_id
            WHERE target_customer_id = NEW.from_customer_id;
            RETURN NEW;
        END
        $$;
        DROP TRIGGER IF EXISTS trg_customer_merges_propagate_references ON customer_merges;
        CREATE TRIGGER trg_customer_merges_propagate_references
            AFTER INSERT ON customer_merges
            FOR EACH ROW EXECUTE FUNCTION aicrm_propagate_customer_merge();
        """
    )


def downgrade() -> None:
    # These columns are durable references. Rollback disables OneID reads and
    # keeps the shadow references, just like the identity-core migration.
    pass
