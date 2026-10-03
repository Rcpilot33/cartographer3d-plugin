from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast, final

from typing_extensions import override

from cartographer.adapters.k2.trigger_dispatch import K2TriggerDispatch
from cartographer.adapters.klipper_like.mcu_platform import KlipperLikeMcuPlatform

if TYPE_CHECKING:
    from collections.abc import Callable

    from configfile import ConfigWrapper
    from mcu import MCU

    class K2Mcu(MCU):
        @property
        def is_non_critical(self) -> bool: ...
        @property
        def non_critical_disconnected(self) -> bool: ...

        def get_non_critical_reconnect_event_name(self) -> str: ...
        def get_non_critical_disconnect_event_name(self) -> str: ...


logger = logging.getLogger(__name__)


@final
class K2McuPlatform(KlipperLikeMcuPlatform):
    """Legacy K2 host transport with non-critical reconnect support."""

    def __init__(self, config: ConfigWrapper, mcu_name: str) -> None:
        super().__init__(config, mcu_name)
        self._k2_mcu = cast("K2Mcu", self._host_mcu)
        self._dispatch: K2TriggerDispatch | None = None
        self._config_callback: Callable[[], None] | None = None
        self._finalization_failed = False

    @override
    def create_trigger_dispatch(self) -> K2TriggerDispatch:
        self._dispatch = K2TriggerDispatch(self._host_mcu)
        return self._dispatch

    @override
    def register_data_response(self, handler: Callable[..., None], fmt: str, name: str) -> None:
        if hasattr(self._host_mcu, "register_serial_response"):
            _ = self._host_mcu.register_serial_response(handler, fmt)
        else:
            self._host_mcu.register_response(handler, name)

    @override
    def register_config_callback(self, callback: Callable[[], None]) -> None:
        self._config_callback = callback
        super().register_config_callback(callback)

    @override
    def register_lifecycle_handlers(
        self,
        *,
        on_identify: Callable[[], None],
        on_connect: Callable[[], None],
        on_shutdown: Callable[[], None],
        on_reconnect: Callable[[], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        self._printer.register_event_handler("klippy:mcu_identify", on_identify)
        self._printer.register_event_handler("klippy:connect", on_connect)
        self._printer.register_event_handler("klippy:shutdown", on_shutdown)
        if on_disconnect is not None:
            self._printer.register_event_handler(self._k2_mcu.get_non_critical_disconnect_event_name(), on_disconnect)
        if on_reconnect is not None:

            def finalize_reconnect() -> None:
                # K2 replaces serialqueue on reconnect. No previous command queue
                # or C dispatch allocation may be used with that new connection.
                self._finalization_failed = False
                try:
                    if self._k2_mcu.is_non_critical and self._k2_mcu.non_critical_disconnected:
                        msg = "Cartographer reconnect event fired while MCU still reports disconnected"
                        raise RuntimeError(msg)
                    if self._config_callback is not None:
                        self._config_callback()
                    if self._dispatch is not None:
                        self._dispatch.reinit_after_reconnect()
                except Exception:
                    # Do not throw into the host MCU's reconnect state machine.
                    # Keep all probe operations blocked until a clean retry/restart.
                    self._finalization_failed = True
                    logger.exception("Failed to finalize K2 Cartographer reconnect; probing remains disabled")
                    return
                on_reconnect()

            self._printer.register_event_handler(
                self._k2_mcu.get_non_critical_reconnect_event_name(), finalize_reconnect
            )

    @override
    def is_disconnected(self) -> bool:
        return self._finalization_failed or (self._k2_mcu.is_non_critical and self._k2_mcu.non_critical_disconnected)
