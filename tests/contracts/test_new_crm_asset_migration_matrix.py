from __future__ import annotations

import json

from aicrm_next.capability_registry import CAPABILITY_SPECS
from tools.check_new_crm_asset_migration_matrix import MATRIX, validate


def test_new_crm_asset_migration_matrix_is_complete_and_safe() -> None:
    assert validate() == []


def test_all_registry_capabilities_have_explicit_real_blocked_or_presentation_status() -> None:
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    rows = {
        row["registry_capability_id"]: row
        for row in matrix["assets"]
        if "registry_capability_id" in row
    }
    assert set(rows) == {spec.capability_id for spec in CAPABILITY_SPECS}
    assert {row["implementation_status"] for row in rows.values()} <= {
        "real",
        "backend_blocked",
        "presentation_only",
    }
