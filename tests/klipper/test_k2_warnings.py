from __future__ import annotations

import importlib
import sys
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from cartographer.adapters.k2.warnings import DISCONNECTED_WARNING, clear_disconnected_warning

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


def test_removes_only_matching_runtime_warnings_and_refreshes_status() -> None:
    host = Mock()
    stale = {"type": "runtime_warning", "message": DISCONNECTED_WARNING}
    calibration = {"type": "runtime_warning", "message": "Incompatible scan model"}
    unrelated = {"type": "runtime_warning", "message": "Other MCU disconnected"}
    deprecated = {"type": "deprecated_option", "message": DISCONNECTED_WARNING}
    host.runtime_warnings = [stale, calibration, stale.copy(), unrelated, deprecated]
    clear_disconnected_warning(host)
    assert host.runtime_warnings == [calibration, unrelated, deprecated]
    host._rebuild_status_warnings.assert_called_once_with()
    clear_disconnected_warning(host)
    host._rebuild_status_warnings.assert_called_once_with()


@pytest.mark.parametrize("version", [None, "5.1.0"])
@pytest.mark.parametrize("validation_fails", [False, True])
def test_entry_point_clears_only_after_connected_validation(
    mocker: MockerFixture, version: str | None, validation_fails: bool
) -> None:
    mocker.patch.dict(sys.modules, {"extras.thermistor": Mock(), "greenlet": Mock()})
    extra = importlib.import_module("cartographer.extra")
    adapters = Mock()
    adapters.mcu.get_mcu_version.return_value = version
    integrator = Mock()
    cartographer = Mock()
    cartographer.macros = []
    _ = mocker.patch.object(extra, "init_runtime", return_value=(adapters, integrator))
    _ = mocker.patch.object(extra, "PrinterCartographer", return_value=cartographer)
    _ = extra.load_config(Mock())
    callback = adapters.mcu.register_reconnect_callback.call_args.args[0]
    if validation_fails:
        cartographer.validate_and_load_models.side_effect = RuntimeError("validation failed")
        with pytest.raises(RuntimeError, match="validation failed"):
            callback()
    else:
        callback()
    cartographer.validate_and_load_models.assert_called_once_with()
    if version is not None and not validation_fails:
        adapters.on_reconnect_models_validated.assert_called_once_with()
    else:
        adapters.on_reconnect_models_validated.assert_not_called()
