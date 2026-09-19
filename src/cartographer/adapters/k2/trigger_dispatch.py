from __future__ import annotations

from typing import TYPE_CHECKING

import chelper
import mcu
from mcu import TriggerDispatch
from typing_extensions import override

if TYPE_CHECKING:
    from reactor import ReactorCompletion


class K2TriggerDispatch(TriggerDispatch):
    """Keep the K2 fork's transport timeouts local to each dispatch start."""

    @override
    def start(self, print_time: float) -> ReactorCompletion:
        old_timeout = mcu.TRSYNC_TIMEOUT
        old_single_timeout = mcu.TRSYNC_SINGLE_MCU_TIMEOUT
        try:
            mcu.TRSYNC_TIMEOUT = 0.025 * 8
            mcu.TRSYNC_SINGLE_MCU_TIMEOUT = 0.250 * 8
            return super().start(print_time)
        finally:
            mcu.TRSYNC_TIMEOUT = old_timeout
            mcu.TRSYNC_SINGLE_MCU_TIMEOUT = old_single_timeout

    def reinit_after_reconnect(self) -> None:
        """Rebind the existing trsync OIDs to the new serialqueue."""
        ffi_main, ffi_lib = chelper.get_ffi()
        self._trdispatch = ffi_main.gc(ffi_lib.trdispatch_alloc(), ffi_lib.free)
        for trsync in self._trsyncs:
            host = trsync.get_mcu()
            trsync._trdispatch = self._trdispatch
            trsync._trdispatch_mcu = ffi_main.gc(
                ffi_lib.trdispatch_mcu_alloc(
                    self._trdispatch,
                    host._serial.serialqueue,
                    trsync.get_command_queue(),
                    trsync.get_oid() & 0xFFFFFFFF,
                    host.lookup_command_tag("trsync_set_timeout oid=%c clock=%u") & 0xFFFFFFFF,
                    host.lookup_command_tag("trsync_trigger oid=%c reason=%c") & 0xFFFFFFFF,
                    host.lookup_command_tag("trsync_state oid=%c can_trigger=%c trigger_reason=%c clock=%u")
                    & 0xFFFFFFFF,
                ),
                ffi_lib.free,
            )
