#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aicrm_next.capability_registry import CAPABILITY_SPECS


MATRIX = ROOT / "docs/architecture/new_crm_asset_migration_matrix.json"
PROFILE = ROOT / "deploy/deployment_profiles/production-current.json"

REQUIRED_ASSET_FIELDS = {
    "id",
    "source",
    "type",
    "target_path",
    "sensitivity",
    "implementation_status",
    "dependencies",
    "tests",
    "deployment_status",
    "disposition",
}
ALLOWED_TYPES = {"memory", "skill", "capability"}
ALLOWED_STATUSES = {"real", "backend_blocked", "presentation_only", "not_applicable"}
ALLOWED_DISPOSITIONS = {"already_canonical", "migrated_sanitized", "excluded_raw"}
FORBIDDEN_CONTENT = {
    "private_user_path": re.compile(r"/" + r"Users/[^/\s]+/"),
    "codex_private_memory": re.compile(r"\.codex/(?:memories|sessions)/"),
    "private_key_block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(r"\b(?:gho|ghp|ghs|ghu|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "cn_mobile": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
}


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate() -> list[str]:
    errors: list[str] = []
    matrix = _load(MATRIX)
    profile = _load(PROFILE)

    if matrix.get("repository") != "qianlan33333-png/AI-CRM-production":
        errors.append("matrix repository must be the production repository")
    if matrix.get("allowed_deploy_hosts") != ["124.220.53.183"]:
        errors.append("matrix must allow exactly host 124.220.53.183")
    if matrix.get("raw_sources_committed") is not False:
        errors.append("raw_sources_committed must remain false")

    assets = matrix.get("assets")
    if not isinstance(assets, list) or not assets:
        return errors + ["assets must be a non-empty list"]

    seen_ids: set[str] = set()
    registry_rows: dict[str, dict[str, object]] = {}
    for index, raw in enumerate(assets):
        if not isinstance(raw, dict):
            errors.append(f"asset[{index}] must be an object")
            continue
        missing = REQUIRED_ASSET_FIELDS - raw.keys()
        if missing:
            errors.append(f"asset[{index}] missing fields: {sorted(missing)}")
            continue
        asset_id = str(raw["id"])
        if asset_id in seen_ids:
            errors.append(f"duplicate asset id: {asset_id}")
        seen_ids.add(asset_id)
        if raw["type"] not in ALLOWED_TYPES:
            errors.append(f"{asset_id}: invalid type")
        if raw["implementation_status"] not in ALLOWED_STATUSES:
            errors.append(f"{asset_id}: invalid implementation_status")
        if raw["disposition"] not in ALLOWED_DISPOSITIONS:
            errors.append(f"{asset_id}: invalid disposition")
        if not isinstance(raw["dependencies"], list) or not isinstance(raw["tests"], list):
            errors.append(f"{asset_id}: dependencies and tests must be lists")
        target = ROOT / str(raw["target_path"])
        if not target.exists():
            errors.append(f"{asset_id}: target does not exist: {raw['target_path']}")
        registry_id = raw.get("registry_capability_id")
        if registry_id:
            registry_rows[str(registry_id)] = raw

    expected_specs = {spec.capability_id: spec for spec in CAPABILITY_SPECS}
    if set(registry_rows) != set(expected_specs):
        errors.append(
            "registry capability coverage mismatch: "
            f"expected={sorted(expected_specs)} actual={sorted(registry_rows)}"
        )
    for capability_id, spec in expected_specs.items():
        row = registry_rows.get(capability_id)
        if row is None:
            continue
        if row["implementation_status"] not in {"real", "backend_blocked", "presentation_only"}:
            errors.append(f"{capability_id}: capability status must use the capability vocabulary")
        if row["dependencies"] != list(spec.dependencies):
            errors.append(f"{capability_id}: dependency list drift")

    enabled = set(profile.get("enabled_capabilities", []))
    if enabled != set(expected_specs):
        errors.append("production profile and capability registry differ")

    artifacts = matrix.get("migration_artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        errors.append("migration_artifacts must be a non-empty list")
    else:
        for relative in artifacts:
            path = ROOT / str(relative)
            if not path.is_file():
                errors.append(f"migration artifact missing: {relative}")
                continue
            text = path.read_text(encoding="utf-8")
            for name, pattern in FORBIDDEN_CONTENT.items():
                if pattern.search(text):
                    errors.append(f"{relative}: forbidden {name} content")

    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("New CRM asset migration matrix OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
