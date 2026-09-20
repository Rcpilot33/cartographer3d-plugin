from __future__ import annotations

from typing import Protocol

DISCONNECTED_WARNING = (
    "[cartographer] MCU not connected. "
    "Models loaded without version validation - recalibrate if MCU firmware was updated."
)


class RuntimeWarnings(Protocol):
    runtime_warnings: list[dict[str, object]]

    def _rebuild_status_warnings(self) -> None: ...


def clear_disconnected_warning(configfile: RuntimeWarnings) -> None:
    """Remove only the stale startup warning, preserving all other warnings."""
    remaining = [
        warning
        for warning in configfile.runtime_warnings
        if not (warning.get("type") == "runtime_warning" and warning.get("message") == DISCONNECTED_WARNING)
    ]
    if len(remaining) != len(configfile.runtime_warnings):
        configfile.runtime_warnings[:] = remaining
        configfile._rebuild_status_warnings()
