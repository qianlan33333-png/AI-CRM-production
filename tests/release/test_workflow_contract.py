from __future__ import annotations

from pathlib import Path

import pytest
import yaml


pytestmark = pytest.mark.release
ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflow(name: str) -> dict[str, object]:
    payload = yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(payload, dict)
    return payload


def test_pr_gate_is_one_required_job_with_fixed_core_checks() -> None:
    workflow = _workflow("ci-fast.yml")
    assert workflow["name"] == "PR Gate"
    assert set(workflow["on"]) == {"pull_request"}
    assert tuple(workflow["jobs"]) == ("pr-gate",)
    job = workflow["jobs"]["pr-gate"]
    assert job["name"] == "pr / gate"
    assert job["timeout-minutes"] == "25"
    source = (WORKFLOWS / "ci-fast.yml").read_text(encoding="utf-8")
    assert "scripts/ci/select_test_scope.py" not in source
    assert "make check" in source
    assert "tests/high_risk tests/release" in source
    assert "AICRM_WECOM_EXECUTION_MODE: disabled" in source
    assert "postgres:16" in source


def test_full_regression_has_only_manual_or_high_risk_call_and_no_matrix() -> None:
    workflow = _workflow("full-regression.yml")
    triggers = set(workflow["on"])
    assert triggers == {"workflow_call", "workflow_dispatch"}
    assert tuple(workflow["jobs"]) == ("full-regression",)
    source = (WORKFLOWS / "full-regression.yml").read_text(encoding="utf-8")
    assert "matrix:" not in source
    assert "schedule:" not in source
    assert "scripts/ci/run_ci.py --tier full" in source


def test_promotion_and_deploy_preserve_exact_sha_lock_health_and_rollback() -> None:
    promotion = (WORKFLOWS / "promote-production.yml").read_text(encoding="utf-8")
    deploy = (WORKFLOWS / "deploy.yml").read_text(encoding="utf-8")
    deploy_workflow = _workflow("deploy.yml")
    deploy_ssh_step = next(
        step
        for step in deploy_workflow["jobs"]["deploy"]["steps"]
        if step.get("name") == "Deploy via SSH"
    )
    assert "workflow_run:" not in promotion
    assert "push:" in promotion
    assert "branches:" in promotion
    assert "- main" in promotion
    assert "uses: ./.github/workflows/full-regression.yml" in promotion
    assert "needs: verify-main" in promotion
    assert "release_sha: ${{ github.sha }}" in promotion
    assert "ref: ${{ inputs.release_sha }}" in deploy
    assert "aicrm-production-deploy" in deploy
    assert "flock -n 9" in deploy
    assert "x-aicrm-release-sha" in deploy.lower()
    assert "cleanup_deploy" in deploy
    assert "before_sha" in deploy
    assert "scripts/ops/check_runtime_readiness.py" in deploy
    assert "tee /tmp/aicrm-runtime-readiness.json" in deploy
    assert deploy_ssh_step["with"]["command_timeout"] == "20m"


def test_active_production_callback_relay_uses_current_host() -> None:
    relay = (WORKFLOWS / "relay-id-validation-wecom-callback.yml").read_text(encoding="utf-8")
    assert "EXPECTED_SOURCE_HOST: 124.220.53.183" in relay
    assert "RELAY ONE CALLBACK FROM 124 TO 49" in relay
    assert "SHA256:qbHmMUPtj9373JhvK807wWcewj5xxhOfuCwq1gu16n8" in relay
    assert "150.158.82.186" not in relay


def test_public_www_cutover_is_manual_exact_sha_and_guarded() -> None:
    workflow = _workflow("public-www-production-cutover.yml")
    source = (WORKFLOWS / "public-www-production-cutover.yml").read_text(encoding="utf-8")
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["jobs"]["cutover"]["environment"] == "production"
    assert "CUTOVER_PUBLIC_WWW_TO_CURRENT_RELEASE" in source
    assert "test \"$(git rev-parse HEAD)\" = \"$EXPECTED_RELEASE_SHA\"" in source
    assert "ensure_production_public_release_route.py --execute" in source
    assert "--local-health-url http://127.0.0.1:5001/health" in source
    assert "--public-health-url \"$public_health_url\"" in source
    assert "test \"$actual_sha\" = \"$EXPECTED_RELEASE_SHA\"" in source
