"""
A library for reading/writing Relics' Chunky file format.
"""

from relic.chunky._apis import apis as APIs, read
from relic.chunky._core import Version, MagicWord
__version__ = '2022.1rc0'
__all__ = [
    "read",
    "APIs",
    "Version",
    "MagicWord",
]