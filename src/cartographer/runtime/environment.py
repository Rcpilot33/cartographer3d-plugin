from __future__ import annotations

from enum import Enum


class Environment(Enum):
    Klipper = "klipper"
    Kalico = "kalico"
    KlipperV12 = "klipper_v12"
    K2 = "k2"


def detect_environment(config: object) -> Environment:
    del config
    try:
        from klippy import APP_NAME

        if APP_NAME == "Kalico":
            return Environment.Kalico
    except ImportError:
        pass

    try:
        from mcu import TriggerDispatch

        del TriggerDispatch
    except ImportError:
        return Environment.KlipperV12

    from mcu import MCU

    # The K2 port has a patched legacy MCU with this explicit reconnect API.
    # Inspect the class, not an instance with dynamic attribute lookup.
    if isinstance(MCU, type) and all(name in vars(MCU) for name in ("recon_mcu", "reset_to_initial_state")):
        return Environment.K2
    return Environment.Klipper
