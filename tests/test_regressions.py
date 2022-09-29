"""
Tests which ensures releases do not break backwards-compatibility by failing to expose modules/names
"""

import importlib
from typing import List, Iterable, Tuple

import pytest

core__all__ = [
    "definitions",
    "serialization",
]

ROOT = "relic.chunky.v1"


@pytest.mark.parametrize("submodule", core__all__)
def test_import_module(submodule: str):
    try:
        importlib.import_module(f"{ROOT}.{submodule}")
    except ImportError:
        raise AssertionError(f"{submodule} is no longer exposed!")


definitions__all__ = [
    "version"
]
serialization__all__ = [
    "ChunkyFSSerializer",
    "chunky_fs_serializer",
]


def module_imports_helper(submodule: str, all: List[str]) -> Iterable[Tuple[str, str]]:
    return zip([submodule] * len(all), all)


@pytest.mark.parametrize(
    ["submodule", "attribute"],
    [
        *module_imports_helper("definitions", definitions__all__),
        *module_imports_helper("serialization", serialization__all__),
    ],
)
def test_module_imports(submodule: str, attribute: str):
    module = importlib.import_module(f"{ROOT}.{submodule}")
    _ = getattr(module, attribute)
