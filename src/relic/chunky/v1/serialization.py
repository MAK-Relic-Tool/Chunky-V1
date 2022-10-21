from dataclasses import dataclass
from typing import BinaryIO, Dict, cast
from serialization_tools.structx import Struct

from relic.chunky.core.definitions import ChunkFourCC
from relic.chunky.core.errors import ChunkNameError
from relic.chunky.core.protocols import StreamSerializer
from relic.chunky.core.serialization import (
    ChunkTypeSerializer,
    chunk_type_serializer,
    ChunkFourCCSerializer,
    chunk_cc_serializer,
    ChunkCollectionHandler, ChunkyFSSerializer
)

from relic.chunky.v1.definitions import version as version_1p1, ChunkHeader


@dataclass
class ChunkHeaderSerializer(StreamSerializer[ChunkHeader]):
    chunk_type_serializer: ChunkTypeSerializer
    chunk_cc_serializer: ChunkFourCCSerializer
    layout: Struct

    def unpack(self, stream: BinaryIO) -> ChunkHeader:
        chunk_type = self.chunk_type_serializer.unpack(stream)
        chunk_cc = self.chunk_cc_serializer.unpack(stream)
        version, size, name_size = self.layout.unpack_stream(stream)
        name_buffer = stream.read(name_size)
        try:
            name = name_buffer.rstrip(b"\0").decode("ascii")
        except UnicodeDecodeError as exc:
            raise ChunkNameError(name_buffer) from exc
        return ChunkHeader(chunk_type, chunk_cc, version, size, name)

    def pack(self, stream: BinaryIO, packable: ChunkHeader) -> int:
        written = 0
        written += self.chunk_type_serializer.pack(stream, packable.type)
        name_buffer = packable.name.encode("ascii")
        args = packable.cc, packable.version, packable.type, len(name_buffer)
        written += self.layout.pack(args)
        written += stream.write(name_buffer)
        return written


chunk_header_serializer = ChunkHeaderSerializer(
    chunk_type_serializer, chunk_cc_serializer, Struct("<3L")
)


class _NoneHeaderSerializer(StreamSerializer[None]):
    def unpack(self, stream: BinaryIO) -> None:
        return None

    def pack(self, stream: BinaryIO, packable: None) -> int:
        return 0


def _noneHeader2Meta(_: None) -> Dict[str, object]:
    return {}


def _noneMeta2Header(_: Dict[str, object]) -> None:
    return None


def _chunkHeader2meta(header: ChunkHeader) -> Dict[str, object]:
    return {
        "name": header.name,
        "version": header.version,
        "4cc": str(header.cc),
    }


def _meta2chunkHeader(meta: Dict[str, object]) -> ChunkHeader:
    fourcc: str = cast(str, meta["4cc"])
    version: int = cast(int, meta["version"])
    name: str = cast(str, meta["name"])
    return ChunkHeader(name=name, cc=ChunkFourCC(fourcc), version=version, type=None, size=None)  # type: ignore


_chunk_collection_handler = ChunkCollectionHandler(
    header_serializer=chunk_header_serializer,
    header2meta=_chunkHeader2meta,
    meta2header=_meta2chunkHeader
)

chunky_fs_serializer = ChunkyFSSerializer(
    version=version_1p1,
    chunk_serializer=_chunk_collection_handler,
    header_serializer=_NoneHeaderSerializer(),
    header2meta=_noneHeader2Meta,
    meta2header=_noneMeta2Header
)

__all__ = [
    "chunky_fs_serializer",
]
