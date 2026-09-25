from __future__ import annotations

import importlib
import sys
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from cartographer.adapters.klipper.endstop import KlipperEndstop
from cartographer.interfaces.errors import McuDisconnectedError
from cartographer.runtime.environment import Environment, detect_environment

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

    from pytest_mock import MockerFixture


@pytest.fixture
def carto_mcu(mocker: MockerFixture) -> type:
    mocker.patch.dict(sys.modules, {"extras.thermistor": Mock(), "greenlet": Mock()})
    return importlib.import_module("cartographer.mcu.mcu").CartographerMcu


@pytest.fixture
def k2_module(mocker: MockerFixture) -> Iterator[ModuleType]:
    """Load the adapter against a fake host, never a firmware installation."""
    host_module = sys.modules["mcu"]
    fake_base = type("FakeDispatch", (), {"__init__": lambda self, _host: None})
    mocker.patch.object(host_module, "TriggerDispatch", fake_base)
    mocker.patch.dict(sys.modules, {"chelper": Mock()})
    names = ("cartographer.adapters.k2.trigger_dispatch", "cartographer.adapters.k2.mcu_platform")
    for name in names:
        sys.modules.pop(name, None)
    module = importlib.import_module(names[1])
    yield module
    for name in names:
        sys.modules.pop(name, None)


def test_k2_detected_without_changing_kalico_precedence(mocker: MockerFixture) -> None:
    k2_host = type("MCU", (), {"recon_mcu": lambda self: None, "reset_to_initial_state": lambda self: None})
    mocker.patch.object(sys.modules["mcu"], "MCU", k2_host)
    klippy = Mock(spec=["APP_NAME"])
    klippy.APP_NAME = "Creality"
    mocker.patch.dict(sys.modules, {"klippy": klippy})
    assert detect_environment(None) == Environment.K2
    klippy.APP_NAME = "Kalico"
    assert detect_environment(None) == Environment.Kalico


@pytest.mark.parametrize("failure", [False, True])
def test_k2_timeout_restored_even_when_start_fails(k2_module: ModuleType, mocker: MockerFixture, failure: bool) -> None:
    dispatch_class = k2_module.K2TriggerDispatch
    host_module = sys.modules["mcu"]
    mocker.patch.object(host_module, "TRSYNC_TIMEOUT", 0.031, create=True)
    mocker.patch.object(host_module, "TRSYNC_SINGLE_MCU_TIMEOUT", 0.3, create=True)

    def start(_self: object, _time: float) -> str:
        assert host_module.TRSYNC_TIMEOUT == 0.2
        assert host_module.TRSYNC_SINGLE_MCU_TIMEOUT == 2.0
        if failure:
            raise RuntimeError
        return "completion"

    mocker.patch.object(dispatch_class.__bases__[0], "start", start, create=True)
    dispatch = dispatch_class(Mock())
    if failure:
        with pytest.raises(RuntimeError):
            dispatch.start(1.0)
    else:
        assert dispatch.start(1.0) == "completion"
    assert host_module.TRSYNC_TIMEOUT == 0.031
    assert host_module.TRSYNC_SINGLE_MCU_TIMEOUT == 0.3


@pytest.mark.parametrize("failure", [False, True])
def test_reconnect_rebuilds_before_callbacks_and_blocks_failure(
    k2_module: ModuleType,
    mocker: MockerFixture,
    failure: bool,
) -> None:
    config = Mock()
    host = Mock(is_non_critical=True, non_critical_disconnected=False)
    host.get_non_critical_reconnect_event_name.return_value = "reconnected"
    host.get_non_critical_disconnect_event_name.return_value = "disconnected"
    mocker.patch("cartographer.adapters.klipper_like.mcu_platform._mcu_module.get_printer_mcu", return_value=host)
    platform = k2_module.K2McuPlatform(config, "cartographer")
    events: dict[str, object] = {}
    config.get_printer().register_event_handler.side_effect = events.__setitem__
    order: list[str] = []
    platform.register_config_callback(lambda: order.append("commands"))
    dispatch = platform.create_trigger_dispatch()

    def rebind() -> None:
        order.append("dispatch")
        if failure:
            raise RuntimeError

    mocker.patch.object(dispatch, "reinit_after_reconnect", side_effect=rebind)
    platform.register_lifecycle_handlers(
        on_identify=Mock(),
        on_connect=Mock(),
        on_shutdown=Mock(),
        on_reconnect=lambda: order.append("models"),
        on_disconnect=Mock(),
    )
    callback = events["reconnected"]
    assert callable(callback)
    callback()
    assert order == (["commands", "dispatch"] if failure else ["commands", "dispatch", "models"])
    assert platform.is_disconnected() is failure


@pytest.mark.parametrize("mode", ["scan", "touch"])
def test_disconnected_homing_never_arms_dispatch(carto_mcu: type, mode: str) -> None:
    platform = Mock()
    platform.is_disconnected.return_value = True
    mcu = carto_mcu(platform, Mock())
    with pytest.raises(McuDisconnectedError):
        if mode == "scan":
            mcu.start_homing_scan(1.0, 1000.0)
        else:
            mcu.start_homing_touch(1.0, 2000)
    platform.create_trigger_dispatch.return_value.start.assert_not_called()


def test_query_disconnected_endstop_is_fail_closed() -> None:
    mcu = Mock()
    mcu.is_disconnected.return_value = True
    probe = Mock()
    assert KlipperEndstop(mcu, probe).query_endstop(0.0) == 1
    probe.query_is_triggered.assert_not_called()


def test_stop_homing_still_disarms_after_disconnect(carto_mcu: type) -> None:
    platform = Mock()
    platform.is_disconnected.return_value = True
    mcu = carto_mcu(platform, Mock())
    with pytest.raises(McuDisconnectedError):
        mcu.stop_homing(1.0)
    platform.create_trigger_dispatch.return_value.stop.assert_called_once()


def test_stop_homing_disarms_even_when_wait_fails(carto_mcu: type) -> None:
    platform = Mock()
    platform.is_disconnected.return_value = True
    failure = McuDisconnectedError()
    dispatch = platform.create_trigger_dispatch.return_value
    dispatch.wait_end.side_effect = failure
    mcu = carto_mcu(platform, Mock())
    with pytest.raises(McuDisconnectedError) as caught:
        mcu.stop_homing(1.0)
    assert caught.value is failure
    dispatch.stop.assert_called_once()


def test_k2_valid_trigger_finalizes_dispatch_before_disarming_firmware(
    carto_mcu: type, mocker: MockerFixture
) -> None:
    mocker.patch.object(sys.modules["mcu"].MCU_trsync, "REASON_ENDSTOP_HIT", 1)
    order: list[str] = []
    platform = Mock()
    platform.is_disconnected.return_value = False
    dispatch = platform.create_trigger_dispatch.return_value
    dispatch.stop_before_mcu_homing_disarm = True
    dispatch.wait_end.side_effect = lambda _time: order.append("wait")
    dispatch.stop.side_effect = lambda: order.append("dispatch") or 1
    mcu = carto_mcu(platform, Mock())
    mcu._commands = Mock()
    mcu._commands.send_stop_home.side_effect = lambda: order.append("firmware")

    assert mcu.stop_homing(1.0) == 1.0
    assert order == ["wait", "dispatch", "firmware"]


def test_k2_disconnect_during_trigger_cleanup_never_disarms_after_failed_dispatch(carto_mcu: type) -> None:
    platform = Mock()
    platform.is_disconnected.return_value = False
    dispatch = platform.create_trigger_dispatch.return_value
    dispatch.stop_before_mcu_homing_disarm = True
    failure = McuDisconnectedError()
    dispatch.stop.side_effect = failure
    mcu = carto_mcu(platform, Mock())
    mcu._commands = Mock()

    with pytest.raises(McuDisconnectedError) as caught:
        mcu.stop_homing(1.0)

    assert caught.value is failure
    mcu._commands.send_stop_home.assert_not_called()


@pytest.mark.parametrize("failure_index", [0, 1])
@pytest.mark.parametrize("already_disconnected", [False, True])
def test_k2_stop_cleans_every_participant_after_failure(
    k2_module: ModuleType, mocker: MockerFixture, failure_index: int, already_disconnected: bool
) -> None:
    ffi = Mock()
    mocker.patch("chelper.get_ffi", return_value=(Mock(), ffi))
    dispatch = k2_module.K2TriggerDispatch(Mock())
    dispatch._trdispatch = object()
    participants = [Mock(), Mock(), Mock()]
    dispatch._trsyncs = participants
    for participant in participants:
        participant.get_mcu().non_critical_disconnected = False
        participant.stop.return_value = 1
        participant.get_steppers.return_value = [Mock(), Mock()]
    failed = participants[failure_index]
    failure = RuntimeError("serial closed during stop")
    failed.get_mcu().non_critical_disconnected = already_disconnected
    failed.stop.side_effect = failure
    expected_error = McuDisconnectedError if already_disconnected else RuntimeError
    with pytest.raises(expected_error) as caught:
        dispatch.stop()
    if not already_disconnected:
        assert caught.value is failure
    else:
        failed.stop.assert_not_called()
    for index, participant in enumerate(participants):
        if index != failure_index:
            participant.stop.assert_called_once()
    for stepper in failed.get_steppers():
        stepper.note_homing_end.assert_called_once()
    assert failed._trigger_completion is None
    failed.get_mcu().register_response.assert_called_once_with(None, "trsync_state", failed.get_oid())
    participants[0].get_mcu().get_printer().invoke_shutdown.assert_called_once()
    ffi.trdispatch_stop.assert_called_once_with(dispatch._trdispatch)


@pytest.mark.parametrize("timeout", [False, True])
def test_k2_stop_keeps_success_and_timeout_distinct(
    k2_module: ModuleType, mocker: MockerFixture, timeout: bool
) -> None:
    mocker.patch("chelper.get_ffi", return_value=(Mock(), Mock()))
    mocker.patch.object(sys.modules["mcu"].MCU_trsync, "REASON_COMMS_TIMEOUT", 2)
    dispatch = k2_module.K2TriggerDispatch(Mock())
    dispatch._trdispatch = object()
    dispatch._trsyncs = [Mock(), Mock()]
    for participant in dispatch._trsyncs:
        participant.get_mcu().non_critical_disconnected = False
        participant.stop.return_value = 1
    dispatch._trsyncs[1].stop.return_value = 2 if timeout else 3
    if timeout:
        with pytest.raises(RuntimeError, match="Communication timeout"):
            dispatch.stop()
        dispatch._trsyncs[0].get_mcu().get_printer().invoke_shutdown.assert_called_once()
    else:
        assert dispatch.stop() == 1
        dispatch._trsyncs[0].get_mcu().get_printer().invoke_shutdown.assert_not_called()
    for participant in dispatch._trsyncs:
        participant.stop.assert_called_once()


def test_k2_stop_attempts_all_cleanup_even_after_ffi_and_local_failures(
    k2_module: ModuleType, mocker: MockerFixture
) -> None:
    ffi = Mock()
    failure = RuntimeError("ffi stop failed")
    ffi.trdispatch_stop.side_effect = failure
    mocker.patch("chelper.get_ffi", return_value=(Mock(), ffi))
    dispatch = k2_module.K2TriggerDispatch(Mock())
    dispatch._trdispatch = object()
    dispatch._trsyncs = [Mock(), Mock()]
    for participant in dispatch._trsyncs:
        participant.get_mcu().non_critical_disconnected = False
        participant.stop.side_effect = RuntimeError("serial failure")
        participant.get_mcu().register_response.side_effect = RuntimeError("unregister failure")
        participant.get_steppers.return_value = [Mock(), Mock()]
        participant.get_steppers()[0].note_homing_end.side_effect = RuntimeError("stepper failure")
    with pytest.raises(RuntimeError) as caught:
        dispatch.stop()
    assert caught.value is failure
    for participant in dispatch._trsyncs:
        participant.stop.assert_called_once()
        for stepper in participant.get_steppers():
            stepper.note_homing_end.assert_called_once()


@pytest.mark.parametrize("active_session", [False, True])
def test_disconnected_session_entry_rejected_and_reconnect_allowed(
    carto_mcu: type,
    mocker: MockerFixture,
    active_session: bool,
) -> None:
    platform = Mock()
    platform.is_disconnected.return_value = False
    mcu = carto_mcu(platform, Mock())
    start_streaming = mocker.patch.object(mcu, "start_streaming")
    mocker.patch.object(mcu, "stop_streaming")
    existing = mcu.start_session() if active_session else None

    platform.is_disconnected.return_value = True
    platform.register_lifecycle_handlers.call_args.kwargs["on_disconnect"]()
    with pytest.raises(McuDisconnectedError):
        mcu.start_session()
    assert start_streaming.call_count == int(active_session)

    if existing is not None:
        with existing, pytest.raises(McuDisconnectedError):
            existing.wait_for(lambda _samples: False)

    platform.is_disconnected.return_value = False
    with mcu.start_session() as recovered:
        assert recovered.get_items() == []
    assert start_streaming.call_count == int(active_session) + 1


def test_reconnect_stops_stale_stream_before_callbacks(carto_mcu: type, mocker: MockerFixture) -> None:
    platform = Mock()
    platform.is_disconnected.return_value = False
    mcu = carto_mcu(platform, Mock())
    order: list[str] = []
    mocker.patch.object(mcu, "stop_streaming", side_effect=lambda: order.append("stop"))
    mcu.register_reconnect_callback(lambda: order.append("callback"))

    platform.register_lifecycle_handlers.call_args.kwargs["on_reconnect"]()

    assert order == ["stop", "callback"]
    platform.invoke_shutdown.assert_not_called()


def test_reconnect_stream_reset_failure_blocks_callbacks(carto_mcu: type, mocker: MockerFixture) -> None:
    platform = Mock()
    platform.is_disconnected.return_value = False
    mcu = carto_mcu(platform, Mock())
    callback = Mock()
    failure = RuntimeError("stream reset failed")
    mocker.patch.object(mcu, "stop_streaming", side_effect=failure)
    mcu.register_reconnect_callback(callback)

    platform.register_lifecycle_handlers.call_args.kwargs["on_reconnect"]()

    callback.assert_not_called()
    platform.invoke_shutdown.assert_called_once_with("Cartographer MCU reconnect failed: stream reset failed")


def test_disconnect_disables_immediate_processing(carto_mcu: type, mocker: MockerFixture) -> None:
    platform = Mock()
    platform.is_disconnected.return_value = False
    mcu = carto_mcu(platform, Mock())
    set_immediate = mocker.patch.object(mcu._async_processor, "set_immediate")

    platform.register_lifecycle_handlers.call_args.kwargs["on_disconnect"]()

    set_immediate.assert_called_once_with(False)
