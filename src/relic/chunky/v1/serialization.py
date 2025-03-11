from dataclasses import dataclass
from struct import Struct
from typing import BinaryIO

from relic.chunky.core.errors import ChunkNameError
from relic.chunky.core.protocols import StreamSerializer
from relic.chunky.core.serialization import (
    ChunkTypeSerializer,
    chunk_type_serializer,
    ChunkFourCCSerializer,
    chunk_cc_serializer,
)

from relic.chunky.v1.definitions import version as version_1p1, ChunkHeader
from relic.chunky.v1.filesystem import ChunkyFSSerializer


@dataclass
class ChunkHeaderSerializer(StreamSerializer[ChunkHeader]):
    chunk_type_serializer: ChunkTypeSerializer
    chunk_cc_serializer: ChunkFourCCSerializer
    layout: Struct

    def size(self) -> int:
        return 4 + 4 + self.layout.size

    def unpack(self, stream: BinaryIO) -> ChunkHeader:
        chunk_type = self.chunk_type_serializer.unpack(stream)
        chunk_cc = self.chunk_cc_serializer.unpack(stream)
        version, size, name_size = self.layout.unpack(stream.read(self.layout.size))
        name_buffer = stream.read(name_size)
        try:
            name = name_buffer.rstrip(b"\0").decode("ascii")
        except UnicodeDecodeError as exc:
            raise ChunkNameError(name_buffer) from exc
        return ChunkHeader(chunk_type, chunk_cc, version, size, name)

    def pack(self, stream: BinaryIO, packable: ChunkHeader) -> int:
        written = 0
        written += self.chunk_type_serializer.pack(stream, packable.type)
        written += self.chunk_cc_serializer.pack(stream, packable.cc)
        name_buffer = packable.name.encode("ascii") + b"\0"
        args = packable.version, packable.size, len(name_buffer)
        written += stream.write(self.layout.pack(*args))
        written += stream.write(name_buffer)
        return written


chunk_header_serializer = ChunkHeaderSerializer(
    chunk_type_serializer, chunk_cc_serializer, Struct("<3L")
)




chunky_fs_serializer = ChunkyFSSerializer(
    version=version_1p1,
    chunk_header_serializer=chunk_header_serializer,
)

__all__ = [
    "chunky_fs_serializer",
]
