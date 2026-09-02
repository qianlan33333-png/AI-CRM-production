#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "deploy" / "production_runtime_units.json"
SYSTEMD_DIR = Path("/etc/systemd/system")
DEPLOY_GUARD_FILE = Path("/home/ubuntu/.aicrm-production-deploy-in-progress")
WEB_START_AUTHORIZATION_FILE = Path("/run/aicrm-production-web-start-authorized")
RUNTIME_START_AUTHORIZATION_FILE = Path("/run/aicrm-production-runtime-start-authorized")
QUEUE_RUNTIME_GENERATION_ENV = Path("/home/ubuntu/.aicrm-queue-runtime-generation.env")
DEPLOY_GUARD_DROPIN = "00-aicrm-deploy-transaction-guard.conf"
DEPLOY_GUARD_SOURCE = ROOT / "deploy" / "systemd" / DEPLOY_GUARD_DROPIN
PRIMARY_WEB_GUARD_SOURCE = ROOT / "deploy" / "systemd" / "00-aicrm-primary-web-transaction-guard.conf"
DEFAULT_TIMER_SERVICE_DRAIN_TIMEOUT_SECONDS = 120
TIMER_SERVICE_DRAIN_POLL_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True)
class TimerUnit:
    timer: str
    service: str
    kick_after_timer_restart: bool = False
    kick_failure_fatal: bool = False


@dataclass(frozen=True)
class ServiceUnit:
    service: str
    health_url: str | None = None
    stop_for_migration: bool = False


@dataclass(frozen=True)
class RetiredDropIn:
    unit: str
    dropin: str


@dataclass(frozen=True)
class SuccessorOwner:
    legacy_owner: str
    capability: str
    successor_kind: str
    successor_unit: str
    health_contract: str
    backlog_contract: str


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def database_application_names(manifest: dict[str, Any]) -> dict[str, str]:
    raw = manifest.get("database_application_names") or {}
    if not isinstance(raw, dict):
        raise ValueError("database_application_names must be an object")
    return {
        str(service or "").strip(): str(application_name or "").strip()
        for service, application_name in raw.items()
    }


def active_timers(manifest: dict[str, Any]) -> list[TimerUnit]:
    timers: list[TimerUnit] = []
    for item in manifest.get("active_autostart") or []:
        timers.append(
            TimerUnit(
                timer=str(item["timer"]),
                service=str(item["service"]),
                kick_after_timer_restart=bool(item.get("kick_after_timer_restart", False)),
                kick_failure_fatal=bool(item.get("kick_failure_fatal", False)),
            )
        )
    return timers


def active_services(manifest: dict[str, Any]) -> list[ServiceUnit]:
    services: list[ServiceUnit] = []
    for item in manifest.get("active_services") or []:
        services.append(
            ServiceUnit(
                service=str(item["service"]),
                health_url=item.get("health_url") or None,
                stop_for_migration=bool(item.get("stop_for_migration", False)),
            )
        )
    return services


def cutover_owner_inventory(manifest: dict[str, Any]) -> str:
    section = manifest.get("cutover_managed_legacy") or {}
    if not isinstance(section, dict):
        raise ValueError("cutover_managed_legacy must be an object")
    return str(section.get("owner_inventory") or "").strip()


def cutover_legacy_timers(manifest: dict[str, Any]) -> list[TimerUnit]:
    section = manifest.get("cutover_managed_legacy") or {}
    if not isinstance(section, dict):
        raise ValueError("cutover_managed_legacy must be an object")
    timers: list[TimerUnit] = []
    for item in section.get("timers") or []:
        if not isinstance(item, dict):
            raise ValueError("cutover_managed_legacy timers must declare timer and service")
        timer = str(item.get("timer") or "").strip()
        service = str(item.get("service") or "").strip()
        if not timer or not service:
            raise ValueError("cutover_managed_legacy timers must declare timer and service")
        timers.append(TimerUnit(timer=timer, service=service))
    return timers


def cutover_replacement_timers(manifest: dict[str, Any]) -> list[TimerUnit]:
    section = manifest.get("cutover_replacement_autostart") or {}
    if not isinstance(section, dict):
        raise ValueError("cutover_replacement_autostart must be an object")
    timers: list[TimerUnit] = []
    for item in section.get("timers") or []:
        if not isinstance(item, dict):
            raise ValueError("cutover_replacement_autostart timers must declare timer and service")
        timer = str(item.get("timer") or "").strip()
        service = str(item.get("service") or "").strip()
        if not timer or not service:
            raise ValueError("cutover_replacement_autostart timers must declare timer and service")
        timers.append(TimerUnit(timer=timer, service=service))
    return timers


def cutover_replacement_owner_inventory(manifest: dict[str, Any]) -> str:
    section = manifest.get("cutover_replacement_autostart") or {}
    if not isinstance(section, dict):
        raise ValueError("cutover_replacement_autostart must be an object")
    return str(section.get("owner_inventory") or "").strip()


def cutover_successor_owner_inventory(manifest: dict[str, Any]) -> str:
    section = manifest.get("cutover_successor_matrix") or {}
    if not isinstance(section, dict):
        raise ValueError("cutover_successor_matrix must be an object")
    return str(section.get("owner_inventory") or "").strip()


def cutover_successor_owners(manifest: dict[str, Any]) -> list[SuccessorOwner]:
    section = manifest.get("cutover_successor_matrix") or {}
    if not isinstance(section, dict):
        raise ValueError("cutover_successor_matrix must be an object")
    owners: list[SuccessorOwner] = []
    for item in section.get("owners") or []:
        if not isinstance(item, dict):
            raise ValueError("cutover successor owners must be objects")
        owner = SuccessorOwner(
            legacy_owner=str(item.get("legacy_owner") or "").strip(),
            capability=str(item.get("capability") or "").strip(),
            successor_kind=str(item.get("successor_kind") or "").strip(),
            successor_unit=str(item.get("successor_unit") or "").strip(),
            health_contract=str(item.get("health_contract") or "").strip(),
            backlog_contract=str(item.get("backlog_contract") or "").strip(),
        )
        if not all(
            (
                owner.legacy_owner,
                owner.capability,
                owner.successor_kind,
                owner.successor_unit,
                owner.health_contract,
                owner.backlog_contract,
            )
        ):
            raise ValueError("cutover successor owners must declare every contract field")
        if owner.successor_kind not in {"persistent_service", "timer"}:
            raise ValueError("cutover successor_kind must be persistent_service or timer")
        owners.append(owner)
    return owners


def cutover_legacy_persistent_services(manifest: dict[str, Any]) -> list[ServiceUnit]:
    section = manifest.get("cutover_managed_legacy") or {}
    if not isinstance(section, dict):
        raise ValueError("cutover_managed_legacy must be an object")
    services: list[ServiceUnit] = []
    for item in section.get("persistent_services") or []:
        if not isinstance(item, dict):
            raise ValueError("cutover_managed_legacy persistent services must declare service")
        service = str(item.get("service") or "").strip()
        if not service:
            raise ValueError("cutover_managed_legacy persistent services must declare service")
        services.append(ServiceUnit(service=service))
    return services


def cutover_legacy_units(manifest: dict[str, Any]) -> list[str]:
    timers = cutover_legacy_timers(manifest)
    persistent = cutover_legacy_persistent_services(manifest)
    return list(
        dict.fromkeys(
            (
                *(unit.timer for unit in timers),
                *(unit.service for unit in timers),
                *(unit.service for unit in persistent),
            )
        )
    )


def staged_runtime_generation(path: Path | None = None) -> int:
    marker = path or QUEUE_RUNTIME_GENERATION_ENV
    if not marker.exists():
        return 0
    values = []
    for line in marker.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.strip().partition("=")
        if separator and key == "AICRM_QUEUE_WORKER_GENERATION":
            values.append(value.strip())
    if not values:
        return 0
    if len(values) != 1:
        raise ValueError("queue runtime generation marker must declare exactly one generation")
    try:
        generation = int(values[0])
    except ValueError as exc:
        raise ValueError("queue runtime generation marker must be an integer") from exc
    if generation < 0:
        raise ValueError("queue runtime generation marker must be >= 0")
    return generation


def runtime_cutover_committed(path: Path | None = None) -> bool:
    marker = path or QUEUE_RUNTIME_GENERATION_ENV
    if not marker.exists():
        return False
    values = []
    for line in marker.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.strip().partition("=")
        if separator and key == "AICRM_QUEUE_CUTOVER_COMMITTED":
            values.append(value.strip())
    if not values:
        return False
    if len(values) != 1 or values[0] not in {"0", "1"}:
        raise ValueError("queue runtime cutover committed marker must be exactly 0 or 1")
    committed = values[0] == "1"
    if committed and staged_runtime_generation(marker) <= 0:
        raise ValueError("queue runtime cutover cannot be committed at generation 0")
    return committed


def primary_web_service(manifest: dict[str, Any]) -> ServiceUnit:
    item = manifest.get("primary_web") or {}
    service = str(item.get("service") or "").strip()
    if not service:
        raise ValueError("production runtime units manifest must declare primary_web.service")
    return ServiceUnit(service=service, health_url=item.get("health_url") or None)


def retired_dropins(manifest: dict[str, Any]) -> list[RetiredDropIn]:
    return [RetiredDropIn(unit=str(item["unit"]), dropin=str(item["dropin"])) for item in manifest.get("retired_dropins") or []]


def _deploy_path(unit: str) -> Path:
    return ROOT / "deploy" / unit


def _read_unit(unit: str) -> str:
    return _deploy_path(unit).read_text(encoding="utf-8")


def _unique(items: list[str], label: str) -> None:
    duplicates = sorted({item for item in items if items.count(item) > 1})
    if duplicates:
        raise ValueError(f"duplicate {label}: {duplicates}")


def _timer_service(timer: str) -> str | None:
    for line in _read_unit(timer).splitlines():
        if line.strip().startswith("Unit="):
            return line.split("=", 1)[1].strip()
    return None


def approval_timers(manifest: dict[str, Any]) -> list[TimerUnit]:
    timers: list[TimerUnit] = []
    for item in manifest.get("approval_required") or []:
        if not isinstance(item, dict):
            raise ValueError("approval_required entries must declare timer and service")
        timer_name = str(item.get("timer") or "").strip()
        service = str(item.get("service") or "").strip()
        if not timer_name or not service:
            raise ValueError("approval_required entries must declare timer and service")
        timers.append(TimerUnit(timer=timer_name, service=service))
    return timers


def retired_units(manifest: dict[str, Any]) -> list[str]:
    return [str(unit) for unit in manifest.get("retired_forbidden") or []]


def retired_unit_files(manifest: dict[str, Any]) -> list[str]:
    return [str(unit) for unit in manifest.get("retired_unit_files") or []]


def _validate_timer_unit(timer: TimerUnit) -> None:
    timer_path = _deploy_path(timer.timer)
    service_path = _deploy_path(timer.service)
    if not timer_path.exists():
        raise FileNotFoundError(f"missing timer unit: {timer_path}")
    if not service_path.exists():
        raise FileNotFoundError(f"missing service unit for {timer.timer}: {service_path}")
    timer_text = _read_unit(timer.timer)
    if _timer_service(timer.timer) != timer.service:
        raise ValueError(f"{timer.timer} Unit= must point to {timer.service}")
    if "WantedBy=timers.target" not in timer_text:
        raise ValueError(f"{timer.timer} must install into timers.target")
    if "OnCalendar=" in timer_text and "Persistent=true" not in timer_text:
        raise ValueError(f"{timer.timer} uses OnCalendar and must set Persistent=true")


def _directive_values(body: str, directive: str) -> list[str]:
    prefix = f"{directive}="
    return [line.strip().split("=", 1)[1].strip() for line in body.splitlines() if line.strip().startswith(prefix)]


def _entrypoint_path(exec_start: str) -> Path | None:
    module_match = re.search(r"\bpython(?:3(?:\.\d+)*)?\s+-m\s+([A-Za-z_][A-Za-z0-9_.]*)", exec_start)
    if module_match:
        module_path = Path(*module_match.group(1).split("."))
        module_file = ROOT / module_path.with_suffix(".py")
        if module_file.exists():
            return module_file
        return ROOT / module_path / "__main__.py"
    script_match = re.search(r"\bpython(?:3(?:\.\d+)*)?\s+([A-Za-z0-9_./-]+\.py)\b", exec_start)
    if not script_match:
        return None
    relative_path = Path(script_match.group(1))
    if relative_path.is_absolute() or ".." in relative_path.parts:
        return None
    return ROOT / relative_path


def _validate_managed_service(service: str) -> None:
    path = _deploy_path(service)
    if not path.exists():
        raise FileNotFoundError(f"missing managed service unit: {path}")
    body = _read_unit(service)
    if not _directive_values(body, "EnvironmentFile"):
        raise ValueError(f"{service} must declare EnvironmentFile")
    if _directive_values(body, "User") != ["ubuntu"]:
        raise ValueError(f"{service} must declare User=ubuntu")
    if _directive_values(body, "WorkingDirectory") != ["/home/ubuntu/极简 crm"]:
        raise ValueError(f"{service} must declare WorkingDirectory=/home/ubuntu/极简 crm")
    exec_starts = _directive_values(body, "ExecStart")
    if len(exec_starts) != 1:
        raise ValueError(f"{service} must declare exactly one ExecStart")
    entrypoint = _entrypoint_path(exec_starts[0])
    if entrypoint is None:
        raise ValueError(f"{service} ExecStart must reference a repository Python entrypoint")
    if not entrypoint.exists():
        raise FileNotFoundError(f"managed service entrypoint does not exist: {service}: {entrypoint}")


def _validate_database_application_name(service: str, expected: str) -> None:
    body = _read_unit(service)
    assignments: dict[str, list[str]] = {}
    for directive in _directive_values(body, "Environment"):
        key, separator, value = directive.strip().strip('"').partition("=")
        if separator:
            assignments.setdefault(key, []).append(value)
    for key in ("DB_APPLICATION_NAME", "PGAPPNAME"):
        if assignments.get(key) != [expected]:
            raise ValueError(f"{service} must declare exactly one {key}={expected}")


def _guarded_units(manifest: dict[str, Any]) -> list[str]:
    units = [
        primary_web_service(manifest).service,
        *(service.service for service in active_services(manifest)),
        *(unit.timer for unit in active_timers(manifest)),
        *(unit.service for unit in active_timers(manifest)),
        *(unit.timer for unit in cutover_replacement_timers(manifest)),
        *(unit.service for unit in cutover_replacement_timers(manifest)),
        *(unit.timer for unit in approval_timers(manifest)),
        *(unit.service for unit in approval_timers(manifest)),
        *retired_units(manifest),
    ]
    return list(dict.fromkeys(units))


def installable_runtime_units(manifest: dict[str, Any]) -> list[str]:
    """Return unit files copied by this release's runtime installer."""

    units = [
        primary_web_service(manifest).service,
        *(service.service for service in active_services(manifest)),
        *(unit.timer for unit in active_timers(manifest)),
        *(unit.service for unit in active_timers(manifest)),
        *(unit.timer for unit in cutover_replacement_timers(manifest)),
        *(unit.service for unit in cutover_replacement_timers(manifest)),
        *(unit.timer for unit in approval_timers(manifest)),
        *(unit.service for unit in approval_timers(manifest)),
    ]
    return list(dict.fromkeys(units))


def _deploy_guard_source(manifest: dict[str, Any], unit: str) -> Path:
    if unit == primary_web_service(manifest).service:
        return PRIMARY_WEB_GUARD_SOURCE
    return DEPLOY_GUARD_SOURCE


def _deploy_guard_destination(service: str) -> Path:
    return SYSTEMD_DIR / f"{service}.d" / DEPLOY_GUARD_DROPIN


def _validate_deploy_guards() -> None:
    for source in (DEPLOY_GUARD_SOURCE, PRIMARY_WEB_GUARD_SOURCE):
        if not source.exists():
            raise FileNotFoundError(f"missing production deploy guard: {source}")
    generic_values = _directive_values(DEPLOY_GUARD_SOURCE.read_text(encoding="utf-8"), "ConditionPathExists")
    if generic_values != [f"|!{DEPLOY_GUARD_FILE}", f"|{RUNTIME_START_AUTHORIZATION_FILE}"]:
        raise ValueError(f"production deploy guard must block starts while {DEPLOY_GUARD_FILE} exists")
    primary_values = _directive_values(
        PRIMARY_WEB_GUARD_SOURCE.read_text(encoding="utf-8"),
        "ConditionPathExists",
    )
    if primary_values != [
        f"|!{DEPLOY_GUARD_FILE}",
        f"|{WEB_START_AUTHORIZATION_FILE}",
        f"|{RUNTIME_START_AUTHORIZATION_FILE}",
    ]:
        raise ValueError("primary Web deploy guard must require an idle transaction or explicit canary authorization")


def validate_manifest(manifest: dict[str, Any], *, validate_unit_files: bool = True) -> None:
    if manifest.get("schema_version") != 5:
        raise ValueError("production runtime units manifest schema_version must be 5")
    drain_timeout = int(manifest.get("timer_service_drain_timeout_seconds") or DEFAULT_TIMER_SERVICE_DRAIN_TIMEOUT_SECONDS)
    if drain_timeout < 1 or drain_timeout > 900:
        raise ValueError("timer_service_drain_timeout_seconds must be between 1 and 900")
    primary_web = primary_web_service(manifest)
    timers = active_timers(manifest)
    services = active_services(manifest)
    cutover_inventory = cutover_owner_inventory(manifest)
    cutover_timers = cutover_legacy_timers(manifest)
    cutover_persistent = cutover_legacy_persistent_services(manifest)
    replacement_inventory = cutover_replacement_owner_inventory(manifest)
    replacement_timers = cutover_replacement_timers(manifest)
    successor_inventory = cutover_successor_owner_inventory(manifest)
    successors = cutover_successor_owners(manifest)
    if not cutover_inventory:
        raise ValueError("cutover_managed_legacy.owner_inventory is required")
    if not cutover_timers and not cutover_persistent:
        raise ValueError("cutover_managed_legacy must declare at least one old owner")
    if not replacement_inventory or replacement_inventory != cutover_inventory:
        raise ValueError("cutover replacement and legacy owner inventories must match")
    if successor_inventory != cutover_inventory:
        raise ValueError("cutover successor and legacy owner inventories must match")
    approval = approval_timers(manifest)
    approval_required = [unit.timer for unit in approval]
    retired_forbidden = retired_units(manifest)
    retired_files = retired_unit_files(manifest)
    retired_overlay_dropins = retired_dropins(manifest)
    active_timer_names = [unit.timer for unit in timers]
    active_service_names = [unit.service for unit in timers] + [unit.service for unit in services]
    cutover_timer_names = [unit.timer for unit in cutover_timers]
    cutover_service_names = [unit.service for unit in cutover_timers] + [unit.service for unit in cutover_persistent]
    replacement_timer_names = [unit.timer for unit in replacement_timers]
    replacement_service_names = [unit.service for unit in replacement_timers]
    application_names = database_application_names(manifest)
    application_name_services = {
        primary_web.service,
        *(service.service for service in services),
        *(unit.service for unit in timers),
        *replacement_service_names,
    }
    if set(application_names) != application_name_services:
        missing = sorted(application_name_services - set(application_names))
        extra = sorted(set(application_names) - application_name_services)
        raise ValueError(
            f"database_application_names must exactly cover active runtime services: missing={missing}, extra={extra}"
        )
    invalid_application_names = sorted(
        application_name
        for application_name in application_names.values()
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", application_name)
    )
    if invalid_application_names:
        raise ValueError(f"invalid database application_name values: {invalid_application_names}")
    _unique(list(application_names.values()), "database application_name")
    legacy_owner_names = [unit.timer for unit in cutover_timers] + [
        unit.service for unit in cutover_persistent
    ]
    successor_legacy_names = [owner.legacy_owner for owner in successors]
    if (
        len(successor_legacy_names) != len(set(successor_legacy_names))
        or set(successor_legacy_names) != set(legacy_owner_names)
    ):
        raise ValueError("every retired owner must declare exactly one successor")
    successor_timer_names = [
        owner.successor_unit for owner in successors if owner.successor_kind == "timer"
    ]
    unclassified_successor_timers = sorted(
        set(successor_timer_names) - (set(replacement_timer_names) | set(active_timer_names))
    )
    if unclassified_successor_timers:
        raise ValueError(
            "timer successors must be active or cutover replacements: "
            f"{unclassified_successor_timers}"
        )
    replacement_without_owner = sorted(
        set(replacement_timer_names) - set(successor_timer_names)
    )
    if replacement_without_owner:
        raise ValueError(
            "every replacement timer must own at least one retired capability: "
            f"{replacement_without_owner}"
        )
    successor_service_names = {
        owner.successor_unit
        for owner in successors
        if owner.successor_kind == "persistent_service"
    }
    if not successor_service_names.issubset(set(active_service_names)):
        raise ValueError("persistent successors must be active canonical services")
    legacy_unit_names = set(cutover_legacy_units(manifest))
    invalid_successors = sorted(
        owner.successor_unit for owner in successors if owner.successor_unit in legacy_unit_names
    )
    if invalid_successors:
        raise ValueError(f"legacy owners cannot be their own successor: {invalid_successors}")
    _unique(
        active_timer_names
        + approval_required
        + replacement_timer_names
        + cutover_timer_names
        + retired_forbidden,
        "timer classification",
    )
    _unique(
        active_service_names + replacement_service_names + cutover_service_names,
        "runtime service classification",
    )
    _unique(retired_files, "retired unit file")
    _unique([f"{item.unit}.d/{item.dropin}" for item in retired_overlay_dropins], "retired drop-in")
    for item in retired_overlay_dropins:
        if not item.unit.endswith(".service"):
            raise ValueError(f"retired drop-in unit must be a service: {item.unit}")
        if not item.dropin.endswith(".conf") or Path(item.dropin).name != item.dropin:
            raise ValueError(f"retired drop-in must be a .conf basename: {item.dropin}")
    managed_service_names = [
        primary_web.service,
        *(service.service for service in services),
        *(unit.service for unit in timers),
        *(unit.service for unit in approval),
    ]
    _unique(managed_service_names, "managed service")
    overlaps = sorted(
        set(
            managed_service_names
            + active_timer_names
            + approval_required
            + replacement_timer_names
            + replacement_service_names
            + cutover_timer_names
            + cutover_service_names
        )
        & set(retired_forbidden)
    )
    if overlaps:
        raise ValueError(f"retired units must not be managed: {overlaps}")
    invalid_retired_files = sorted(set(retired_files) - set(retired_forbidden))
    if invalid_retired_files:
        raise ValueError(f"retired unit files must also be retired_forbidden: {invalid_retired_files}")
    for unit in retired_files:
        if Path(unit).name != unit or not unit.endswith((".service", ".timer")):
            raise ValueError(f"retired unit file must be a systemd basename: {unit}")
    if validate_unit_files:
        _validate_deploy_guards()
        for unit in timers:
            _validate_timer_unit(unit)
        for unit in cutover_timers:
            _validate_timer_unit(unit)
        for unit in replacement_timers:
            _validate_timer_unit(unit)
        for unit in approval:
            _validate_timer_unit(unit)
        for service in (*managed_service_names, *replacement_service_names, *cutover_service_names):
            _validate_managed_service(service)
        for service, application_name in application_names.items():
            _validate_database_application_name(service, application_name)


class Runner:
    def __init__(self, *, execute: bool) -> None:
        self.execute = execute

    def run(self, command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str] | None:
        print(_shell_join(command))
        if not self.execute:
            return None
        return subprocess.run(command, cwd=ROOT, text=True, check=check)

    def systemctl(
        self,
        *args: str,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str] | None:
        if capture_output:
            command = ["sudo", "systemctl", *args]
            print(_shell_join(command))
            if not self.execute:
                return None
            return subprocess.run(command, cwd=ROOT, text=True, check=check, capture_output=True)
        return self.run(["sudo", "systemctl", *args], check=check)


def _shell_join(command: list[str]) -> str:
    return " ".join(_quote(part) for part in command)


def _quote(value: str) -> str:
    if not value:
        return "''"
    safe = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./:=+-"
    if all(char in safe for char in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _copy_unit(runner: Runner, unit: str) -> None:
    runner.run(["sudo", "cp", f"deploy/{unit}", str(SYSTEMD_DIR) + "/"])


def _install_deploy_guard(manifest: dict[str, Any], runner: Runner) -> None:
    for unit in _guarded_units(manifest):
        destination = _deploy_guard_destination(unit)
        source = _deploy_guard_source(manifest, unit).relative_to(ROOT)
        runner.run(["sudo", "install", "-d", "-m", "0755", str(destination.parent)])
        runner.run(["sudo", "install", "-m", "0644", str(source), str(destination)])


def _verify_deploy_guard_installed(manifest: dict[str, Any], runner: Runner) -> None:
    for unit in _guarded_units(manifest):
        source = _deploy_guard_source(manifest, unit).relative_to(ROOT)
        destination = _deploy_guard_destination(unit)
        runner.run(["sudo", "test", "-f", str(destination)])
        runner.run(["sudo", "cmp", "-s", str(source), str(destination)])


def phase_begin_transaction(manifest: dict[str, Any], runner: Runner) -> None:
    _install_deploy_guard(manifest, runner)
    runner.run(["sudo", "rm", "-f", str(WEB_START_AUTHORIZATION_FILE)])
    runner.run(["sudo", "rm", "-f", str(RUNTIME_START_AUTHORIZATION_FILE)])
    runner.run(["sudo", "touch", str(DEPLOY_GUARD_FILE)])
    runner.run(["sudo", "chmod", "0644", str(DEPLOY_GUARD_FILE)])
    runner.systemctl("daemon-reload")
    runner.run(["sudo", "test", "-e", str(DEPLOY_GUARD_FILE)])
    _verify_deploy_guard_installed(manifest, runner)


def phase_authorize_web_start(manifest: dict[str, Any], runner: Runner) -> None:
    runner.run(["sudo", "test", "-e", str(DEPLOY_GUARD_FILE)])
    _require_inactive(
        runner,
        primary_web_service(manifest).service,
        error_prefix="primary Web must be stopped before canary authorization",
    )
    runner.run(["sudo", "touch", str(WEB_START_AUTHORIZATION_FILE)])
    runner.run(["sudo", "chmod", "0644", str(WEB_START_AUTHORIZATION_FILE)])
    runner.systemctl("daemon-reload")
    runner.run(["sudo", "test", "-e", str(WEB_START_AUTHORIZATION_FILE)])
    _verify_deploy_guard_installed(manifest, runner)


def phase_authorize_runtime_start(manifest: dict[str, Any], runner: Runner) -> None:
    runner.run(["sudo", "test", "-e", str(DEPLOY_GUARD_FILE)])
    # The narrow Web authorization lives under /run and can disappear after the
    # exact-SHA Web smoke. The broader runtime authorization must never pre-exist.
    runner.run(["sudo", "test", "!", "-e", str(RUNTIME_START_AUTHORIZATION_FILE)])
    _require_active(
        runner,
        primary_web_service(manifest).service,
        error_prefix="primary Web must be active before runtime authorization",
    )
    web_authorization = runner.run(
        ["sudo", "test", "-e", str(WEB_START_AUTHORIZATION_FILE)],
        check=False,
    )
    if runner.execute and web_authorization is not None and web_authorization.returncode != 0:
        print("web_start_authorization=missing_after_verified_web_smoke action=continue")
    runner.run(["sudo", "touch", str(RUNTIME_START_AUTHORIZATION_FILE)])
    runner.run(["sudo", "chmod", "0644", str(RUNTIME_START_AUTHORIZATION_FILE)])
    runner.run(["sudo", "rm", "-f", str(WEB_START_AUTHORIZATION_FILE)])
    runner.systemctl("daemon-reload")
    runner.run(["sudo", "test", "-e", str(RUNTIME_START_AUTHORIZATION_FILE)])
    runner.run(["sudo", "test", "!", "-e", str(WEB_START_AUTHORIZATION_FILE)])
    _verify_deploy_guard_installed(manifest, runner)


def phase_authorize_runtime_restore(manifest: dict[str, Any], runner: Runner) -> None:
    """Resume timers after a stop transaction aborts before Web is stopped."""

    runner.run(["sudo", "test", "-e", str(DEPLOY_GUARD_FILE)])
    _require_active(
        runner,
        primary_web_service(manifest).service,
        error_prefix="primary Web must remain active for partial runtime restore",
    )
    runner.run(["sudo", "touch", str(RUNTIME_START_AUTHORIZATION_FILE)])
    runner.run(["sudo", "chmod", "0644", str(RUNTIME_START_AUTHORIZATION_FILE)])
    runner.run(["sudo", "rm", "-f", str(WEB_START_AUTHORIZATION_FILE)])
    runner.systemctl("daemon-reload")
    runner.run(["sudo", "test", "-e", str(RUNTIME_START_AUTHORIZATION_FILE)])
    runner.run(["sudo", "test", "!", "-e", str(WEB_START_AUTHORIZATION_FILE)])
    _verify_deploy_guard_installed(manifest, runner)


def phase_release_runtime_guard(manifest: dict[str, Any], runner: Runner) -> None:
    runner.run(["sudo", "test", "-e", str(DEPLOY_GUARD_FILE)])
    runner.run(["sudo", "test", "-e", str(RUNTIME_START_AUTHORIZATION_FILE)])
    runner.run(["sudo", "test", "!", "-e", str(WEB_START_AUTHORIZATION_FILE)])
    _require_active(
        runner,
        primary_web_service(manifest).service,
        error_prefix="primary Web must be active before runtime guard release",
    )
    _verify_desired_runtime_state(manifest, runner)
    runner.run(["sudo", "rm", "-f", str(RUNTIME_START_AUTHORIZATION_FILE)])
    runner.run(["sudo", "rm", "-f", str(DEPLOY_GUARD_FILE)])
    runner.systemctl("daemon-reload")
    runner.run(["sudo", "test", "!", "-e", str(WEB_START_AUTHORIZATION_FILE)])
    runner.run(["sudo", "test", "!", "-e", str(RUNTIME_START_AUTHORIZATION_FILE)])
    runner.run(["sudo", "test", "!", "-e", str(DEPLOY_GUARD_FILE)])
    _verify_deploy_guard_installed(manifest, runner)


def _retired_dropin_path(item: RetiredDropIn) -> Path:
    return SYSTEMD_DIR / f"{item.unit}.d" / item.dropin


def _verify_retired_dropins_absent(manifest: dict[str, Any], runner: Runner) -> None:
    for item in retired_dropins(manifest):
        runner.run(["sudo", "test", "!", "-e", str(_retired_dropin_path(item))])


def phase_retire_legacy_overlays(manifest: dict[str, Any], runner: Runner) -> None:
    for item in retired_dropins(manifest):
        runner.run(["sudo", "rm", "-f", str(_retired_dropin_path(item))])
    runner.systemctl("daemon-reload")
    _verify_retired_dropins_absent(manifest, runner)


def phase_remove_candidate_only_runtime(
    manifest: dict[str, Any],
    previous_manifest: dict[str, Any],
    runner: Runner,
) -> None:
    """Remove unit files and transaction guards introduced only by a failed candidate."""

    candidate_only_units = sorted(
        set(installable_runtime_units(manifest))
        - set(installable_runtime_units(previous_manifest))
    )
    candidate_only_guards = sorted(
        set(_guarded_units(manifest)) - set(_guarded_units(previous_manifest))
    )
    for unit in candidate_only_units:
        runner.systemctl("disable", "--now", unit, check=False)
        runner.systemctl("stop", unit, check=False)
        runner.systemctl("reset-failed", unit, check=False)
        runner.run(["sudo", "rm", "-f", str(SYSTEMD_DIR / unit)])
    for unit in candidate_only_guards:
        runner.run(["sudo", "rm", "-f", str(_deploy_guard_destination(unit))])
    runner.systemctl("daemon-reload")
    for unit in candidate_only_units:
        runner.run(["sudo", "test", "!", "-e", str(SYSTEMD_DIR / unit)])
        _verify_retired_unit_state(runner, unit, allow_static=True)
    for unit in candidate_only_guards:
        runner.run(["sudo", "test", "!", "-e", str(_deploy_guard_destination(unit))])
    print(
        "candidate_only_runtime_removed="
        + ",".join(candidate_only_units)
        + " candidate_only_guards_removed="
        + ",".join(candidate_only_guards)
    )


def _timer_service_active_state(runner: Runner, service: str) -> str:
    proc = runner.systemctl(
        "show",
        service,
        "--property=ActiveState",
        "--value",
        check=False,
        capture_output=True,
    )
    if not runner.execute or proc is None:
        return "inactive"
    return (proc.stdout or "").strip().lower() or "unknown"


def _wait_for_timer_services_to_drain(manifest: dict[str, Any], runner: Runner, services: list[str]) -> None:
    unique_services = list(dict.fromkeys(services))
    if not unique_services:
        return
    timeout_seconds = int(manifest.get("timer_service_drain_timeout_seconds") or DEFAULT_TIMER_SERVICE_DRAIN_TIMEOUT_SECONDS)
    deadline = time.monotonic() + timeout_seconds
    while True:
        pending = {service: state for service in unique_services if (state := _timer_service_active_state(runner, service)) not in {"inactive", "failed"}}
        if not runner.execute or not pending:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            detail = ", ".join(f"{service}={state}" for service, state in sorted(pending.items()))
            raise RuntimeError(f"timer services did not drain within {timeout_seconds}s: {detail}")
        time.sleep(min(TIMER_SERVICE_DRAIN_POLL_INTERVAL_SECONDS, remaining))


def phase_stop_for_migration(
    manifest: dict[str, Any],
    runner: Runner,
    *,
    allow_already_stopped: bool = False,
) -> None:
    runner.run(["sudo", "test", "-e", str(DEPLOY_GUARD_FILE)])
    generation = staged_runtime_generation()
    cutover_timers = cutover_legacy_timers(manifest)
    cutover_persistent = cutover_legacy_persistent_services(manifest)
    if generation <= 0:
        for unit in cutover_timers:
            _require_enabled(runner, unit.timer, error_prefix="pre-cutover legacy timer is not enabled")
            if not allow_already_stopped:
                _require_active(runner, unit.timer, error_prefix="pre-cutover legacy timer is not active")
        for service in cutover_persistent:
            _require_enabled(runner, service.service, error_prefix="pre-cutover legacy service is not enabled")
            if not allow_already_stopped:
                _require_active(runner, service.service, error_prefix="pre-cutover legacy service is not active")
        for unit in cutover_timers:
            runner.systemctl("stop", unit.timer, check=False)
        for service in cutover_persistent:
            runner.systemctl("stop", service.service, check=False)
        _wait_for_timer_services_to_drain(
            manifest,
            runner,
            [
                *(unit.service for unit in cutover_timers),
                *(service.service for service in cutover_persistent),
            ],
        )
        for unit in cutover_timers:
            runner.systemctl("reset-failed", unit.timer, check=False)
            runner.systemctl("reset-failed", unit.service, check=False)
            _require_inactive(runner, unit.timer, error_prefix="pre-cutover legacy timer did not stop")
            _require_inactive(runner, unit.service, error_prefix="pre-cutover legacy service did not drain")
            _require_not_failed(runner, unit.timer, error_prefix="pre-cutover legacy timer remains failed")
            _require_not_failed(runner, unit.service, error_prefix="pre-cutover legacy service remains failed")
        for service in cutover_persistent:
            runner.systemctl("reset-failed", service.service, check=False)
            _require_inactive(runner, service.service, error_prefix="pre-cutover legacy service did not stop")
            _require_not_failed(runner, service.service, error_prefix="pre-cutover legacy service remains failed")
        print(f"cutover_managed_legacy={cutover_owner_inventory(manifest)} generation=0 action=temporarily_stopped")
    else:
        _verify_cutover_legacy_retired(manifest, runner, generation=generation)
    committed = runtime_cutover_committed()
    if generation > 0 and not committed:
        _verify_cutover_replacements_disabled(manifest, runner, generation=generation)
    timers = [
        *active_timers(manifest),
        *approval_timers(manifest),
        *(cutover_replacement_timers(manifest) if committed else []),
    ]
    for unit in timers:
        runner.systemctl("stop", unit.timer, check=False)
    _wait_for_timer_services_to_drain(manifest, runner, [unit.service for unit in timers])
    for unit in timers:
        runner.systemctl("stop", unit.service, check=False)
    for unit in timers:
        runner.systemctl("reset-failed", unit.timer, check=False)
        runner.systemctl("reset-failed", unit.service, check=False)
        _require_inactive(runner, unit.timer, error_prefix="runtime timer did not stop")
        _require_inactive(runner, unit.service, error_prefix="runtime service did not stop")
        _require_not_failed(runner, unit.timer, error_prefix="runtime timer remains failed")
        _require_not_failed(runner, unit.service, error_prefix="runtime service remains failed")
    for service in active_services(manifest):
        runner.systemctl("stop", service.service, check=False)
        runner.systemctl("reset-failed", service.service, check=False)
        _require_inactive(runner, service.service, error_prefix="runtime service did not stop")
        _require_not_failed(runner, service.service, error_prefix="runtime service remains failed")
    for unit in retired_units(manifest):
        runner.systemctl("disable", "--now", unit, check=False)
        runner.systemctl("stop", unit, check=False)
        runner.systemctl("reset-failed", unit, check=False)
        _verify_retired_unit_state(runner, unit, allow_static=True)
    primary_web = primary_web_service(manifest).service
    runner.systemctl("stop", primary_web, check=False)
    runner.systemctl("reset-failed", primary_web, check=False)
    _require_inactive(runner, primary_web, error_prefix="primary Web did not stop")
    _require_not_failed(runner, primary_web, error_prefix="primary Web remains failed")


def _wait_for_health(url: str, *, execute: bool, attempts: int = 20, interval: float = 0.5) -> None:
    print(f"curl -sSf {url}")
    if not execute:
        return
    last_error = ""
    for _ in range(attempts):
        try:
            with urlopen(url, timeout=2) as response:
                if 200 <= int(response.status) < 300:
                    return
        except URLError as exc:
            last_error = str(exc)
        time.sleep(interval)
    raise RuntimeError(f"health check failed for {url}: {last_error}")


def _is_enabled(runner: Runner, unit: str) -> bool:
    proc = runner.systemctl("is-enabled", unit, check=False)
    return bool(runner.execute and proc is not None and proc.returncode == 0)


def _require_active(runner: Runner, unit: str, *, error_prefix: str) -> None:
    proc = runner.systemctl("is-active", unit, check=False)
    if runner.execute and (proc is None or proc.returncode != 0):
        raise RuntimeError(f"{error_prefix}: {unit}")


def _require_inactive(runner: Runner, unit: str, *, error_prefix: str) -> None:
    proc = runner.systemctl("is-active", unit, check=False)
    if runner.execute and proc is not None and proc.returncode == 0:
        raise RuntimeError(f"{error_prefix}: {unit}")


def _require_enabled(runner: Runner, unit: str, *, error_prefix: str) -> None:
    proc = runner.systemctl("is-enabled", unit, check=False)
    if runner.execute and (proc is None or proc.returncode != 0):
        raise RuntimeError(f"{error_prefix}: {unit}")


def _require_disabled(runner: Runner, unit: str, *, error_prefix: str) -> None:
    proc = runner.systemctl("is-enabled", unit, check=False, capture_output=True)
    if not runner.execute:
        return
    state = (proc.stdout or "").strip().lower() if proc is not None else ""
    if proc is None or proc.returncode == 0 or state not in {"disabled", "masked", "not-found"}:
        raise RuntimeError(f"{error_prefix}: {unit}: {state or 'unknown'}")


def _require_not_failed(runner: Runner, unit: str, *, error_prefix: str) -> None:
    proc = runner.systemctl("is-failed", unit, check=False)
    if runner.execute and proc is not None and proc.returncode == 0:
        raise RuntimeError(f"{error_prefix}: {unit}")


def _verify_approval_timer_state(runner: Runner, unit: str) -> None:
    proc = runner.systemctl("is-enabled", unit, check=False)
    if not runner.execute:
        _require_active(runner, unit, error_prefix="enabled approval timer is not active")
        return
    if proc is not None and proc.returncode == 0:
        _require_active(runner, unit, error_prefix="enabled approval timer is not active")
    else:
        _require_inactive(runner, unit, error_prefix="disabled approval timer is still active")


def _verify_retired_unit_state(runner: Runner, unit: str, *, allow_static: bool = False) -> None:
    enabled = runner.systemctl("is-enabled", unit, check=False, capture_output=True)
    enabled_state = (enabled.stdout or "").strip() if enabled is not None else ""
    static_guard_only = allow_static and enabled_state == "static"
    if runner.execute and enabled is not None and enabled.returncode == 0 and not static_guard_only:
        raise RuntimeError(f"retired runtime unit is still enabled: {unit}")

    checks = (
        ("is-active", "retired runtime unit is still active"),
        ("is-failed", "retired runtime unit remains failed"),
    )
    for action, error_prefix in checks:
        proc = runner.systemctl(action, unit, check=False)
        if runner.execute and proc is not None and proc.returncode == 0:
            raise RuntimeError(f"{error_prefix}: {unit}")


def _verify_cutover_legacy_retired(
    manifest: dict[str, Any],
    runner: Runner,
    *,
    generation: int,
) -> None:
    for unit in cutover_legacy_timers(manifest):
        _require_disabled(runner, unit.timer, error_prefix="post-cutover legacy timer is not disabled")
        _require_inactive(runner, unit.timer, error_prefix="post-cutover legacy timer is still active")
        _require_inactive(runner, unit.service, error_prefix="post-cutover legacy service is still active")
    for service in cutover_legacy_persistent_services(manifest):
        _require_disabled(runner, service.service, error_prefix="post-cutover legacy service is not disabled")
        _require_inactive(runner, service.service, error_prefix="post-cutover legacy service is still active")
    print(
        f"cutover_managed_legacy={cutover_owner_inventory(manifest)} "
        f"generation={generation} action=verified_retired"
    )


def _verify_cutover_replacements_disabled(
    manifest: dict[str, Any],
    runner: Runner,
    *,
    generation: int,
) -> None:
    for unit in cutover_replacement_timers(manifest):
        _require_disabled(runner, unit.timer, error_prefix="cutover replacement timer is not disabled")
        _require_inactive(runner, unit.timer, error_prefix="cutover replacement timer is still active")
        _require_inactive(runner, unit.service, error_prefix="cutover replacement service is still active")
    print(
        f"cutover_replacement_autostart={cutover_replacement_owner_inventory(manifest)} "
        f"generation={generation} action=verified_disabled"
    )


def _verify_cutover_replacements_active(
    manifest: dict[str, Any],
    runner: Runner,
    *,
    generation: int,
) -> None:
    for unit in cutover_replacement_timers(manifest):
        _require_enabled(runner, unit.timer, error_prefix="cutover replacement timer is not enabled")
        _require_active(runner, unit.timer, error_prefix="cutover replacement timer is not active")
    print(
        f"cutover_replacement_autostart={cutover_replacement_owner_inventory(manifest)} "
        f"generation={generation} action=verified_active"
    )


def phase_install_primary_web(manifest: dict[str, Any], runner: Runner) -> None:
    service = primary_web_service(manifest).service
    for retired_file in retired_unit_files(manifest):
        runner.run(["sudo", "rm", "-f", str(SYSTEMD_DIR / retired_file)])
    _copy_unit(runner, service)
    _install_deploy_guard(manifest, runner)
    runner.systemctl("daemon-reload")
    runner.systemctl("enable", service)


def phase_install_enable_after_web_health(manifest: dict[str, Any], runner: Runner) -> None:
    services = active_services(manifest)
    timers = active_timers(manifest)
    approval = approval_timers(manifest)
    replacements = cutover_replacement_timers(manifest)
    enabled_approval_timers = {unit.timer for unit in approval if _is_enabled(runner, unit.timer)}
    copied_services: set[str] = set()
    for service in services:
        _copy_unit(runner, service.service)
        copied_services.add(service.service)
    for unit in timers:
        if unit.service not in copied_services:
            _copy_unit(runner, unit.service)
            copied_services.add(unit.service)
        _copy_unit(runner, unit.timer)
    for unit in approval:
        if unit.service not in copied_services:
            _copy_unit(runner, unit.service)
            copied_services.add(unit.service)
        _copy_unit(runner, unit.timer)
    for unit in replacements:
        if unit.service not in copied_services:
            _copy_unit(runner, unit.service)
            copied_services.add(unit.service)
        _copy_unit(runner, unit.timer)
    runner.systemctl("daemon-reload")
    for service in services:
        runner.systemctl("enable", service.service)
        runner.systemctl("restart", service.service)
        if service.health_url:
            _wait_for_health(service.health_url, execute=runner.execute)
        runner.systemctl("status", service.service, "--no-pager")
    for unit in timers:
        runner.systemctl("enable", unit.timer)
        runner.systemctl("restart", unit.timer)
        if unit.kick_after_timer_restart:
            proc = runner.systemctl("start", unit.service, check=False)
            if runner.execute and proc is not None and proc.returncode != 0:
                runner.systemctl("status", unit.service, "--no-pager", check=False)
                runner.run(["sudo", "journalctl", "-u", unit.service, "-n", "80", "--no-pager"], check=False)
                if unit.kick_failure_fatal:
                    raise RuntimeError(f"fatal runtime kick failed: {unit.service}")
        runner.systemctl("status", unit.timer, "--no-pager")
    for unit in approval:
        if unit.timer not in enabled_approval_timers:
            continue
        runner.systemctl("restart", unit.timer)
        runner.systemctl("status", unit.timer, "--no-pager")
    generation = staged_runtime_generation()
    committed = runtime_cutover_committed()
    if committed:
        for unit in replacements:
            runner.systemctl("enable", unit.timer)
            runner.systemctl("restart", unit.timer)
            runner.systemctl("status", unit.timer, "--no-pager")
        _verify_cutover_replacements_active(manifest, runner, generation=generation)
    else:
        for unit in replacements:
            runner.systemctl("disable", "--now", unit.timer, check=False)
            runner.systemctl("stop", unit.service, check=False)
            runner.systemctl("reset-failed", unit.timer, check=False)
            runner.systemctl("reset-failed", unit.service, check=False)
        _verify_cutover_replacements_disabled(manifest, runner, generation=generation)
    if generation <= 0:
        for service in cutover_legacy_persistent_services(manifest):
            runner.systemctl("restart", service.service)
            runner.systemctl("status", service.service, "--no-pager")
        for unit in cutover_legacy_timers(manifest):
            runner.systemctl("restart", unit.timer)
            runner.systemctl("status", unit.timer, "--no-pager")
        print(f"cutover_managed_legacy={cutover_owner_inventory(manifest)} generation=0 action=restarted_installed_units")
    else:
        _verify_cutover_legacy_retired(manifest, runner, generation=generation)


def _verify_desired_runtime_state(manifest: dict[str, Any], runner: Runner) -> None:
    primary_web = primary_web_service(manifest).service
    _require_enabled(runner, primary_web, error_prefix="required runtime unit is not enabled")
    _require_active(
        runner,
        primary_web,
        error_prefix="required runtime unit is not active",
    )
    for service in active_services(manifest):
        _require_enabled(runner, service.service, error_prefix="required runtime unit is not enabled")
        _require_active(runner, service.service, error_prefix="required runtime unit is not active")
    for unit in active_timers(manifest):
        _require_enabled(runner, unit.timer, error_prefix="required runtime unit is not enabled")
        _require_active(runner, unit.timer, error_prefix="required runtime unit is not active")
    for unit in approval_timers(manifest):
        _verify_approval_timer_state(runner, unit.timer)
    generation = staged_runtime_generation()
    committed = runtime_cutover_committed()
    if committed:
        _verify_cutover_replacements_active(manifest, runner, generation=generation)
    else:
        _verify_cutover_replacements_disabled(manifest, runner, generation=generation)
    if generation <= 0:
        for unit in cutover_legacy_timers(manifest):
            _require_enabled(runner, unit.timer, error_prefix="pre-cutover legacy timer is not enabled")
            _require_active(runner, unit.timer, error_prefix="pre-cutover legacy timer is not active")
        for service in cutover_legacy_persistent_services(manifest):
            _require_enabled(runner, service.service, error_prefix="pre-cutover legacy service is not enabled")
            _require_active(runner, service.service, error_prefix="pre-cutover legacy service is not active")
        print(f"cutover_managed_legacy={cutover_owner_inventory(manifest)} generation=0 action=verified_running")
    else:
        _verify_cutover_legacy_retired(manifest, runner, generation=generation)
    for unit in retired_units(manifest):
        _verify_retired_unit_state(runner, unit)
    for unit in retired_unit_files(manifest):
        runner.run(["sudo", "test", "!", "-e", str(SYSTEMD_DIR / unit)])
    _verify_retired_dropins_absent(manifest, runner)
    approval_required = [unit.timer for unit in approval_timers(manifest)]
    if approval_required:
        print("approval_required_timers=" + ",".join(str(unit) for unit in approval_required))


def phase_verify_staged_runtime(manifest: dict[str, Any], runner: Runner) -> None:
    runner.run(["sudo", "test", "-e", str(DEPLOY_GUARD_FILE)])
    runner.run(["sudo", "test", "-e", str(RUNTIME_START_AUTHORIZATION_FILE)])
    runner.run(["sudo", "test", "!", "-e", str(WEB_START_AUTHORIZATION_FILE)])
    _verify_deploy_guard_installed(manifest, runner)
    _verify_desired_runtime_state(manifest, runner)


def phase_verify(manifest: dict[str, Any], runner: Runner) -> None:
    runner.run(["sudo", "test", "!", "-e", str(WEB_START_AUTHORIZATION_FILE)])
    runner.run(["sudo", "test", "!", "-e", str(RUNTIME_START_AUTHORIZATION_FILE)])
    runner.run(["sudo", "test", "!", "-e", str(DEPLOY_GUARD_FILE)])
    _verify_deploy_guard_installed(manifest, runner)
    _verify_desired_runtime_state(manifest, runner)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage approved AI-CRM production systemd runtime units.")
    parser.add_argument(
        "--phase",
        required=True,
        choices=(
            "authorize-runtime-start",
            "authorize-runtime-restore",
            "authorize-web-start",
            "begin-transaction",
            "ensure-stopped-for-rollback",
            "retire-legacy-overlays",
            "stop-for-migration",
            "stop-for-migration-recovery",
            "install-primary-web",
            "remove-candidate-only-runtime",
            "release-runtime-guard",
            "install-enable-after-web-health",
            "verify",
            "verify-staged-runtime",
        ),
    )
    parser.add_argument("--manifest", default=str(MANIFEST_PATH))
    parser.add_argument("--previous-manifest", default="")
    parser.add_argument("--execute", action="store_true", default=False)
    parser.add_argument("--dry-run", action="store_true", default=False)
    args = parser.parse_args(argv)

    manifest = load_manifest(Path(args.manifest))
    validate_manifest(
        manifest,
        validate_unit_files=args.phase
        not in {
            "authorize-runtime-start",
            "authorize-runtime-restore",
            "authorize-web-start",
            "begin-transaction",
            "ensure-stopped-for-rollback",
            "stop-for-migration",
            "stop-for-migration-recovery",
            "remove-candidate-only-runtime",
            "release-runtime-guard",
        },
    )
    previous_manifest: dict[str, Any] | None = None
    if args.phase == "remove-candidate-only-runtime":
        previous_manifest_path = str(args.previous_manifest or "").strip()
        if not previous_manifest_path:
            parser.error("--previous-manifest is required for remove-candidate-only-runtime")
        previous_manifest = load_manifest(Path(previous_manifest_path))
        validate_manifest(previous_manifest, validate_unit_files=False)
    runner = Runner(execute=bool(args.execute and not args.dry_run))
    if args.phase == "authorize-runtime-start":
        phase_authorize_runtime_start(manifest, runner)
    elif args.phase == "authorize-runtime-restore":
        phase_authorize_runtime_restore(manifest, runner)
    elif args.phase == "authorize-web-start":
        phase_authorize_web_start(manifest, runner)
    elif args.phase == "begin-transaction":
        phase_begin_transaction(manifest, runner)
    elif args.phase == "ensure-stopped-for-rollback":
        phase_stop_for_migration(manifest, runner, allow_already_stopped=True)
    elif args.phase == "retire-legacy-overlays":
        phase_retire_legacy_overlays(manifest, runner)
    elif args.phase == "stop-for-migration":
        phase_stop_for_migration(manifest, runner)
    elif args.phase == "stop-for-migration-recovery":
        phase_stop_for_migration(manifest, runner, allow_already_stopped=True)
    elif args.phase == "install-primary-web":
        phase_install_primary_web(manifest, runner)
    elif args.phase == "remove-candidate-only-runtime":
        assert previous_manifest is not None
        phase_remove_candidate_only_runtime(manifest, previous_manifest, runner)
    elif args.phase == "release-runtime-guard":
        phase_release_runtime_guard(manifest, runner)
    elif args.phase == "install-enable-after-web-health":
        phase_install_enable_after_web_health(manifest, runner)
    elif args.phase == "verify":
        phase_verify(manifest, runner)
    elif args.phase == "verify-staged-runtime":
        phase_verify_staged_runtime(manifest, runner)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
