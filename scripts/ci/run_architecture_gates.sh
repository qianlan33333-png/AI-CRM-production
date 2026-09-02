#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

MODE="full"
if [ "${1:-}" = "--mode" ]; then
  MODE="${2:-full}"
elif [[ "${1:-}" == --mode=* ]]; then
  MODE="${1#--mode=}"
elif [ -n "${1:-}" ]; then
  MODE="$1"
fi

if [ -x ".venv/bin/python" ]; then
  PYTHON=".venv/bin/python"
else
  PYTHON="python"
fi

run_fast() {
"$PYTHON" tools/check_capability_registry.py
"$PYTHON" tools/check_new_crm_asset_migration_matrix.py
"$PYTHON" tools/check_deployment_profiles.py
"$PYTHON" tools/check_job_catalog.py
"$PYTHON" tools/check_domain_migration_contract.py
"$PYTHON" tools/check_legacy_cleanup_contract.py
"$PYTHON" tools/check_import_graph.py
"$PYTHON" tools/check_runtime_module_sizes.py
"$PYTHON" scripts/ci/check_auth_credential_boundaries.py
"$PYTHON" tools/check_route_ownership_manifest.py
"$PYTHON" scripts/ci/update_route_policy_manifest.py --check
"$PYTHON" tools/check_admin_route_auth.py
"$PYTHON" tools/check_repository_ownership.py
"$PYTHON" tools/check_runtime_configuration_contract.py
"$PYTHON" tools/check_retired_runtime_references.py
"$PYTHON" scripts/ci/check_github_action_pins.py
"$PYTHON" scripts/ci/check_github_actions_expression_length.py
"$PYTHON" scripts/ci/check_queue_runtime_cutover_kernel.py
"$PYTHON" scripts/ci/check_id_validation_promotion_manifest.py
"$PYTHON" scripts/ci/check_admin_queue_command_boundary.py
"$PYTHON" scripts/ci/check_welcome_media_effect_ownership.py
}

run_db() {
  "$PYTHON" tools/check_db_access_boundary.py
  "$PYTHON" tools/check_data_table_lifecycle.py
  "$PYTHON" tools/check_sql_static_guard.py
  "$PYTHON" -m pytest tests/postgres/test_migration_head.py -q --tb=short
}

run_full_only() {
  "$PYTHON" tools/check_architecture_boundaries.py
  "$PYTHON" tools/check_external_effects_boundary.py
  "$PYTHON" scripts/ci/check_group_ops_effect_ownership.py
  "$PYTHON" tools/check_background_job_contract.py
  "$PYTHON" tools/check_schema_change_templates.py
  "$PYTHON" scripts/ci/runtime_contract_inventory.py --check docs/architecture/runtime_contract_inventory.json
  "$PYTHON" scripts/ci/check_high_risk_contract_inventory.py
  "$PYTHON" scripts/ci/check_unionid_identity_contract.py
  "$PYTHON" scripts/ci/check_sidebar_questionnaire_access_contract.py
  "$PYTHON" scripts/ci/check_pii_logging.py
  "$PYTHON" scripts/ci/check_dependency_security.py
}

case "$MODE" in
  fast)
    run_fast
    ;;
  db)
    run_fast
    run_db
    ;;
  full)
    run_fast
    run_db
    run_full_only
    ;;
  *)
    echo "Unknown architecture gate mode: $MODE" >&2
    exit 2
    ;;
esac
