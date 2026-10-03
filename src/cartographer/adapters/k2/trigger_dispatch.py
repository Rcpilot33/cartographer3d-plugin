from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import chelper
import mcu
from mcu import TriggerDispatch
from typing_extensions import override

from cartographer.interfaces.errors import McuDisconnectedError

if TYPE_CHECKING:
    from reactor import ReactorCompletion

logger = logging.getLogger(__name__)


class K2TriggerDispatch(TriggerDispatch):
    """Keep the K2 fork's transport timeouts local to each dispatch start."""

    # V4 firmware stops servicing trigger-sync traffic as soon as it receives
    # cartographer_stop_home.  Finalize the cross-MCU dispatch first so the Z
    # steppers retain the real trigger position instead of timing out during
    # the subsequent cleanup query.
    stop_before_mcu_homing_disarm: bool = True

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

    @override
    def stop(self) -> int:
        """Disarm every participant even if the probe's serial connection is gone."""
        _, ffi_lib = chelper.get_ffi()
        failure: Exception | None = None
        results: list[int] = []
        try:
            ffi_lib.trdispatch_stop(self._trdispatch)
        except Exception as exc:
            failure = exc
        for trsync in self._trsyncs:
            host = trsync.get_mcu()
            try:
                if getattr(host, "non_critical_disconnected", False) is True:
                    raise McuDisconnectedError()
                results.append(trsync.stop())
            except Exception as exc:
                if failure is None:
                    failure = exc
                # The host stop method queries serial before notifying its
                # steppers. Finish local cleanup even when that query fails.
                trsync._trigger_completion = None
                try:
                    host.register_response(None, "trsync_state", trsync.get_oid())
                except Exception:
                    logger.exception("Failed to unregister K2 trigger-sync response")
                for stepper in trsync.get_steppers():
                    try:
                        stepper.note_homing_end()
                    except Exception:
                        logger.exception("Failed to finalize K2 stepper homing state")
        if failure is not None:
            # HomingMove cannot reconcile halted positions after home_wait
            # raises. Do not leave the printer Ready with stale coordinates.
            printer = self._trsyncs[0].get_mcu().get_printer()
            printer.invoke_shutdown("Cartographer homing communication/cleanup failure; restart and rehome")
            raise failure
        if mcu.MCU_trsync.REASON_COMMS_TIMEOUT in results:
            # Every participant completed its normal stop path, including
            # note_homing_end(). Return the reason so CartographerMcu can fail
            # this homing operation without escalating a clean timeout to a
            # printer shutdown.
            return mcu.MCU_trsync.REASON_COMMS_TIMEOUT
        return results[0]

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
