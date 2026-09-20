from __future__ import annotations

import logging
from typing import Protocol, cast

logger = logging.getLogger(__name__)

DISCONNECTED_WARNING = (
    "[cartographer] MCU not connected. "
    "Models loaded without version validation - recalibrate if MCU firmware was updated."
)


class RuntimeWarnings(Protocol):
    runtime_warnings: list[dict[str, object]]

    def _rebuild_status_warnings(self) -> None: ...


def clear_disconnected_warning(configfile: object) -> None:
    """Best-effort removal of the stale startup warning, never blocking reconnect."""
    try:
        if not isinstance(getattr(configfile, "runtime_warnings", None), list) or not callable(
            getattr(configfile, "_rebuild_status_warnings", None)
        ):
            return
        host = cast("RuntimeWarnings", configfile)
        remaining = [
            warning
            for warning in host.runtime_warnings
            if not (warning.get("type") == "runtime_warning" and warning.get("message") == DISCONNECTED_WARNING)
        ]
        if len(remaining) != len(host.runtime_warnings):
            host.runtime_warnings[:] = remaining
            host._rebuild_status_warnings()
    except Exception:
        # Cosmetic host integration must not turn a successful reconnect into shutdown.
        logger.warning("Unable to clear Cartographer disconnected warning", exc_info=True)
