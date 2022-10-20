from __future__ import annotations

from dataclasses import dataclass

from relic.chunky.core.definitions import Version, ChunkType, ChunkFourCC
from relic.chunky.core.serialization import MinimalChunkHeader

version = Version(1)


@dataclass
class ChunkHeader(MinimalChunkHeader):
    type: ChunkType
    cc: ChunkFourCC
    version: int
    size: int
    name: str


__all__ = ["version", "ChunkHeader"]
