from __future__ import annotations

from dataclasses import dataclass

from relic.chunky.core.definitions import Version

version = Version(1)


@dataclass
class ChunkMeta:
    name: str
    version: int
