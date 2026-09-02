from __future__ import annotations

import subprocess

import pytest

from scripts.ops import manage_production_runtime_units as runtime_units


class _SystemctlRunner:
    execute = True

    def __init__(self, *, state: str, returncode: int) -> None:
        self.state = state
        self.returncode = returncode

    def systemctl(
        self,
        *args: str,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        assert args == ("is-enabled", "legacy.timer")
        assert check is False
        assert capture_output is True
        return subprocess.CompletedProcess(args, self.returncode, stdout=f"{self.state}\n", stderr="")


def test_require_disabled_accepts_retired_unit_that_is_not_found() -> None:
    runner = _SystemctlRunner(state="not-found", returncode=4)

    runtime_units._require_disabled(runner, "legacy.timer", error_prefix="legacy timer is not disabled")


def test_require_disabled_rejects_enabled_unit() -> None:
    runner = _SystemctlRunner(state="enabled", returncode=0)

    with pytest.raises(RuntimeError, match="legacy timer is not disabled: legacy.timer: enabled"):
        runtime_units._require_disabled(runner, "legacy.timer", error_prefix="legacy timer is not disabled")
