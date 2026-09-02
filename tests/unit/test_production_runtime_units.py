from __future__ import annotations

import subprocess

import pytest

from scripts.ops.manage_production_runtime_units import _require_disabled


class SystemctlRunner:
    execute = True

    def __init__(self, *, returncode: int, state: str) -> None:
        self.returncode = returncode
        self.state = state

    def systemctl(self, *_args, **_kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], self.returncode, stdout=self.state, stderr="")


@pytest.mark.parametrize("state", ["disabled", "masked", "not-found"])
def test_require_disabled_accepts_non_enabled_systemd_states(state: str) -> None:
    _require_disabled(
        SystemctlRunner(returncode=1, state=state),
        "retired.timer",
        error_prefix="legacy timer is not disabled",
    )


def test_require_disabled_rejects_enabled_systemd_state() -> None:
    with pytest.raises(RuntimeError, match="legacy timer is not disabled: retired.timer: enabled"):
        _require_disabled(
            SystemctlRunner(returncode=0, state="enabled"),
            "retired.timer",
            error_prefix="legacy timer is not disabled",
        )
