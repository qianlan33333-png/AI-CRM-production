"""Add the WeCom-first OneID customer identity core.

Revision ID: 0172_wecom_oneid_core
Revises: 0171_user_ops_send_seq
"""

from __future__ import annotations

from alembic import op


revision = "0172_wecom_oneid_core"
down_revision = "0171_user_ops_send_seq"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS customers (
            id BIGSERIAL PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'merged', 'disabled')),
            identity_completeness TEXT NOT NULL DEFAULT 'wecom_only'
                CHECK (identity_completeness IN ('wecom_only', 'enriched', 'conflict')),
            merged_into_customer_id BIGINT REFERENCES customers(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            merged_at TIMESTAMPTZ,
            CONSTRAINT ck_customers_merge_target CHECK (
                (status = 'merged' AND merged_into_customer_id IS NOT NULL)
                OR (status <> 'merged' AND merged_into_customer_id IS NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_identities (
            id BIGSERIAL PRIMARY KEY,
            customer_id BIGINT NOT NULL REFERENCES customers(id),
            provider TEXT NOT NULL,
            identity_type TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            normalized_value TEXT NOT NULL,
            assurance_level TEXT NOT NULL DEFAULT 'provider_verified',
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'conflicted', 'retired')),
            verified_at TIMESTAMPTZ,
            source_type TEXT NOT NULL DEFAULT '',
            source_event_id TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_customer_identity_value_nonempty CHECK (
                provider <> '' AND identity_type <> '' AND scope_key <> '' AND normalized_value <> ''
            )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_customer_identities_active_key
        ON customer_identities(provider, identity_type, scope_key, normalized_value)
        WHERE status = 'active'
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_customer_identities_customer ON customer_identities(customer_id, status)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_merges (
            id BIGSERIAL PRIMARY KEY,
            from_customer_id BIGINT NOT NULL REFERENCES customers(id),
            to_customer_id BIGINT NOT NULL REFERENCES customers(id),
            evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
            rule TEXT NOT NULL,
            operator TEXT NOT NULL DEFAULT 'system',
            source_type TEXT NOT NULL DEFAULT '',
            source_event_id TEXT NOT NULL DEFAULT '',
            reversible_status TEXT NOT NULL DEFAULT 'not_reversed'
                CHECK (reversible_status IN ('not_reversed', 'reversed', 'not_reversible')),
            merged_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_customer_merges_from UNIQUE (from_customer_id),
            CONSTRAINT ck_customer_merge_distinct CHECK (from_customer_id <> to_customer_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_identity_conflicts (
            id BIGSERIAL PRIMARY KEY,
            left_customer_id BIGINT NOT NULL REFERENCES customers(id),
            right_customer_id BIGINT NOT NULL REFERENCES customers(id),
            provider TEXT NOT NULL,
            identity_type TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            reason TEXT NOT NULL,
            evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
            dedupe_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'ignored')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMPTZ
        )
        """
    )

    for table, columns in {
        "crm_user_identity": ["customer_id BIGINT REFERENCES customers(id)"],
        "wecom_external_contact_identity_map": [
            "customer_id BIGINT REFERENCES customers(id)",
            "identity_id BIGINT REFERENCES customer_identities(id)",
        ],
        "wecom_external_contact_follow_users": [
            "customer_id BIGINT REFERENCES customers(id)",
            "identity_id BIGINT REFERENCES customer_identities(id)",
        ],
        "crm_user_identity_resolution_queue": [
            "customer_id BIGINT REFERENCES customers(id)",
            "identity_id BIGINT REFERENCES customer_identities(id)",
            "enrichment_status TEXT NOT NULL DEFAULT 'pending'",
            "last_enrichment_attempt_at TIMESTAMPTZ",
        ],
        "sidebar_customer_profile_fields": ["customer_id BIGINT REFERENCES customers(id)"],
        "contact_tags": ["customer_id BIGINT REFERENCES customers(id)"],
        "questionnaire_submissions": ["customer_id BIGINT REFERENCES customers(id)"],
    }.items():
        for column in columns:
            op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column}")

    op.execute(
        """
        ALTER TABLE crm_user_identity_resolution_queue
        DROP CONSTRAINT IF EXISTS ck_identity_resolution_enrichment_status;
        ALTER TABLE crm_user_identity_resolution_queue
        ADD CONSTRAINT ck_identity_resolution_enrichment_status
        CHECK (enrichment_status IN (
            'resolved', 'pending', 'not_applicable', 'capability_unavailable', 'conflict'
        ))
        """
    )
    op.execute(
        """
        ALTER TABLE identity_resolution_completion_receipt
        DROP CONSTRAINT ck_identity_resolution_completion_status
        """
    )
    op.execute(
        """
        ALTER TABLE identity_resolution_completion_receipt
        ADD CONSTRAINT ck_identity_resolution_completion_status
        CHECK (result_status IN ('resolved', 'conflict', 'ignored', 'pending'))
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_identity_resolution_customer ON crm_user_identity_resolution_queue(customer_id, enrichment_status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_wecom_identity_map_customer ON wecom_external_contact_identity_map(customer_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_wecom_follow_users_customer_active ON wecom_external_contact_follow_users(customer_id, relation_status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_contact_tags_customer ON contact_tags(customer_id) WHERE customer_id IS NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_questionnaire_submissions_customer ON questionnaire_submissions(customer_id, submitted_at DESC) WHERE customer_id IS NOT NULL")

    # Backfill canonical UnionID customers first. This deliberately creates no
    # cross-record guesses: a legacy UnionID row is already a strong identity.
    op.execute(
        """
        DO $$
        DECLARE identity_row RECORD; new_customer_id BIGINT;
        BEGIN
            FOR identity_row IN
                SELECT unionid FROM crm_user_identity
                WHERE unionid <> '' AND customer_id IS NULL
                ORDER BY unionid
            LOOP
                INSERT INTO customers(status, identity_completeness)
                VALUES ('active', 'enriched') RETURNING id INTO new_customer_id;
                INSERT INTO customer_identities(
                    customer_id, provider, identity_type, scope_key, normalized_value,
                    assurance_level, verified_at, source_type
                ) VALUES (
                    new_customer_id, 'wechat', 'unionid',
                    'wechat_open_platform:aicrm_primary', identity_row.unionid,
                    'provider_verified', CURRENT_TIMESTAMP, 'legacy_unionid_backfill'
                );
                UPDATE crm_user_identity SET customer_id = new_customer_id
                WHERE unionid = identity_row.unionid;
            END LOOP;
        END $$
        """
    )
    op.execute(
        """
        DO $$
        DECLARE contact_row RECORD; target_customer_id BIGINT; target_identity_id BIGINT;
        BEGIN
            FOR contact_row IN
                SELECT id, corp_id, external_userid, unionid
                FROM wecom_external_contact_identity_map
                WHERE external_userid <> '' AND customer_id IS NULL
                ORDER BY corp_id, external_userid, id
            LOOP
                target_customer_id := NULL;
                IF contact_row.unionid <> '' THEN
                    SELECT customer_id INTO target_customer_id
                    FROM customer_identities
                    WHERE provider = 'wechat'
                      AND identity_type = 'unionid'
                      AND scope_key = 'wechat_open_platform:aicrm_primary'
                      AND normalized_value = contact_row.unionid
                      AND status = 'active';
                END IF;
                IF target_customer_id IS NULL THEN
                    INSERT INTO customers(status, identity_completeness)
                    VALUES ('active', CASE WHEN contact_row.unionid <> '' THEN 'enriched' ELSE 'wecom_only' END)
                    RETURNING id INTO target_customer_id;
                END IF;
                IF contact_row.unionid <> '' THEN
                    INSERT INTO customer_identities(
                        customer_id, provider, identity_type, scope_key, normalized_value,
                        assurance_level, verified_at, source_type
                    ) VALUES (
                        target_customer_id, 'wechat', 'unionid',
                        'wechat_open_platform:aicrm_primary', contact_row.unionid,
                        'provider_verified', CURRENT_TIMESTAMP, 'legacy_wecom_backfill'
                    )
                    ON CONFLICT (provider, identity_type, scope_key, normalized_value)
                        WHERE status = 'active'
                    DO UPDATE SET updated_at = CURRENT_TIMESTAMP
                    RETURNING customer_id INTO target_customer_id;
                END IF;
                INSERT INTO customer_identities(
                    customer_id, provider, identity_type, scope_key, normalized_value,
                    assurance_level, verified_at, source_type
                ) VALUES (
                    target_customer_id, 'wecom', 'external_userid',
                    contact_row.corp_id, contact_row.external_userid,
                    'provider_verified', CURRENT_TIMESTAMP, 'legacy_wecom_backfill'
                )
                ON CONFLICT (provider, identity_type, scope_key, normalized_value)
                    WHERE status = 'active'
                DO UPDATE SET updated_at = CURRENT_TIMESTAMP
                RETURNING id, customer_id INTO target_identity_id, target_customer_id;
                UPDATE wecom_external_contact_identity_map
                SET customer_id = target_customer_id, identity_id = target_identity_id
                WHERE id = contact_row.id;
            END LOOP;
        END $$
        """
    )
    op.execute(
        """
        UPDATE wecom_external_contact_follow_users follow_user
        SET customer_id = identity_map.customer_id,
            identity_id = identity_map.identity_id
        FROM wecom_external_contact_identity_map identity_map
        WHERE follow_user.corp_id = identity_map.corp_id
          AND follow_user.external_userid = identity_map.external_userid
          AND follow_user.customer_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE crm_user_identity_resolution_queue queue
        SET customer_id = identity_map.customer_id,
            identity_id = identity_map.identity_id,
            enrichment_status = CASE
                WHEN queue.status = 'resolved' THEN 'resolved'
                WHEN queue.status = 'conflict' THEN 'conflict'
                ELSE 'pending'
            END
        FROM wecom_external_contact_identity_map identity_map
        WHERE queue.corp_id = identity_map.corp_id
          AND queue.external_userid = identity_map.external_userid
          AND queue.customer_id IS NULL
        """
    )
    for table in ("sidebar_customer_profile_fields", "contact_tags", "questionnaire_submissions"):
        op.execute(
            f"""
            UPDATE {table} target
            SET customer_id = identity.customer_id
            FROM crm_user_identity identity
            WHERE target.unionid = identity.unionid
              AND target.unionid <> ''
              AND target.customer_id IS NULL
            """
        )

    op.execute("ALTER TABLE sidebar_customer_profile_fields ADD COLUMN IF NOT EXISTS id BIGSERIAL")
    op.execute("ALTER TABLE sidebar_customer_profile_fields DROP CONSTRAINT IF EXISTS sidebar_customer_profile_fields_pkey")
    op.execute("ALTER TABLE sidebar_customer_profile_fields ADD CONSTRAINT sidebar_customer_profile_fields_pkey PRIMARY KEY (id)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_sidebar_customer_profile_fields_customer ON sidebar_customer_profile_fields(customer_id) WHERE customer_id IS NOT NULL")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_contact_tags_customer_user_tag ON contact_tags(customer_id, userid, tag_id) WHERE customer_id IS NOT NULL AND userid <> '' AND tag_id <> ''")

    op.execute(
        """
        INSERT INTO schema_release_compatibility (
            revision, parent_revision, change_kind, compatibility_epoch,
            previous_runtime_compatible, downgrade_policy, metadata_json
        ) VALUES (
            '0172_wecom_oneid_core', '0171_user_ops_send_seq', 'expand', 1,
            TRUE, 'forward_only',
            '{"capability":"wecom_oneid","read_cutover":"feature_switch"}'::jsonb
        )
        ON CONFLICT (revision) DO NOTHING
        """
    )


def downgrade() -> None:
    # OneID rows and merge lineage are durable business evidence. Runtime
    # rollback is performed by disabling the OneID read switch.
    pass
