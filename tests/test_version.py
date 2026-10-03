from __future__ import annotations

import builtins
import runpy
from pathlib import Path
from typing import TYPE_CHECKING

import cartographer

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def test_source_checkout_uses_released_fallback_version(monkeypatch: MonkeyPatch) -> None:
    original_import = builtins.__import__

    def import_without_generated_version(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "cartographer._version":
            raise ImportError(name)
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_generated_version)
    version_file = Path(cartographer.__file__).with_name("__version__.py")
    namespace = runpy.run_path(str(version_file))

    assert namespace["__version__"] == "1.10.1b1+k2.1"
