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


def test_active_workflows_exclude_legacy_www_and_old_host() -> None:
    assert not (WORKFLOWS / "public-www-production-cutover.yml").exists()
    assert not (WORKFLOWS / "relay-id-validation-wecom-callback.yml").exists()
    for path in WORKFLOWS.glob("*.yml"):
        source = path.read_text(encoding="utf-8")
        assert "www.youcangogogo.com" not in source, path.name
        assert "150.158.82.186" not in source, path.name


def test_deploy_verifies_second_system_without_route_mutation() -> None:
    deploy = (WORKFLOWS / "deploy.yml").read_text(encoding="utf-8")
    assert "PUBLIC_HEALTH_URL: ${{ vars.PUBLIC_HEALTH_URL }}" in deploy
    assert 'test "$public_release_sha" = "$after_sha"' in deploy
    assert "ensure_production_public_release_route.py" not in deploy
    assert "PUBLIC_SERVER_NAME" not in deploy
    assert "NGINX_CONFIG_PATH" not in deploy
