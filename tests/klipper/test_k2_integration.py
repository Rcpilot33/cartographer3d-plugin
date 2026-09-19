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
    mocker.patch.dict(sys.modules, {"extras.thermistor": Mock()})
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
