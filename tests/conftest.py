"""Make ``custom_components.dsp_switcher.api`` importable without Home Assistant.

``api.py`` and ``const.py`` deliberately import nothing from Home Assistant, so
the whole test suite runs against a bare ``pytest`` + ``aiohttp`` install. The
package is loaded by file path so importing it never pulls in the sibling
modules (``__init__.py`` of the integration does import Home Assistant).
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "dsp_switcher"


def _load(name: str) -> types.ModuleType:
    """Import one integration module under a synthetic package."""
    pkg_name = "dsp_switcher_pkg"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(COMPONENT)]
        sys.modules[pkg_name] = pkg
    full = f"{pkg_name}.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, COMPONENT / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


const = _load("const")
api = _load("api")
