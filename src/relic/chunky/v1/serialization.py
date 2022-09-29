from dataclasses import dataclass
from typing import BinaryIO, Dict
from serialization_tools.structx import Struct

from relic.core.errors import MismatchError
from relic.chunky.core.definitions import ChunkType, MagicWord, Version, ChunkFourCC
from relic.chunky.core.errors import ChunkNameError, VersionMismatchError
from relic.chunky.core.filesystem import ChunkyFSHandler, ChunkyFS
from relic.chunky.core.protocols import StreamSerializer
from relic.chunky.core.serialization import (
    ChunkTypeSerializer,
    chunk_type_serializer,
    ChunkFourCCSerializer,
    chunk_cc_serializer,
)

from relic.chunky.v1.definitions import version as version_1p1


@dataclass
class _ChunkHeader:
    type: ChunkType
    cc: ChunkFourCC
    version: int
    size: int
    name: str


@dataclass
class ChunkHeaderSerializer(StreamSerializer[_ChunkHeader]):
    chunk_type_serializer: ChunkTypeSerializer
    chunk_cc_serializer: ChunkFourCCSerializer
    layout: Struct

    def unpack(self, stream: BinaryIO) -> _ChunkHeader:
        chunk_type = self.chunk_type_serializer.unpack(stream)
        chunk_cc = self.chunk_cc_serializer.unpack(stream)
        version, size, name_size = self.layout.unpack_stream(stream)
        name_buffer = stream.read(name_size)
        try:
            name = name_buffer.rstrip(b"\0").decode("ascii")
        except UnicodeDecodeError as e:
            raise ChunkNameError(name_buffer) from e
        return _ChunkHeader(chunk_type, chunk_cc, version, size, name)

    def pack(self, stream: BinaryIO, packable: _ChunkHeader) -> int:
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


@dataclass
class ChunkyCollectionHandler:
    header_serializer: ChunkHeaderSerializer

    def _header2meta(self, header: _ChunkHeader) -> Dict[str, Dict[str, object]]:
        return {
            "essence": {
                "name": header.name,
                "version": header.version,
                "4cc": str(header.cc),
            }
        }

    def _slugifyname(self, name: str):
        # Any chunk which references the EssenceFS typically names themselves the full path to the references asset
        #   unfortunately; that's a BAD name in the ChunkyFS; so we need to convert it to a safe ChunkyFS name
        return name.replace("/", "-").replace("\\", "-")

    def _unpack_data(self, fs: ChunkyFS, stream: BinaryIO, header: _ChunkHeader):
        safe_name = self._slugifyname(header.name)
        path = f"{safe_name}.{header.cc.code}"
        metadata = self._header2meta(header)
        data = stream.read(header.size)
        with fs.open(path, "wb") as handle:
            handle.write(data)
        fs.setinfo(path, metadata)

    def _unpack_folder(self, fs: ChunkyFS, stream: BinaryIO, header: _ChunkHeader):
        # Folders shouldn't need to be slugged, but why risk it?
        safe_name = self._slugifyname(header.name)
        path = f"{safe_name}.{header.cc.code}"
        metadata = self._header2meta(header)
        start, size = stream.tell(), header.size
        sub_fs = fs.makedir(path)
        sub_fs.setinfo("/", metadata)
        self.unpack_chunk_collection(sub_fs, stream, start, start + size)

    def unpack_chunk(self, fs: ChunkyFS, stream: BinaryIO):
        header = self.header_serializer.unpack(stream)
        if header.type == ChunkType.Data:
            return self._unpack_data(fs, stream, header)
        elif header.type == ChunkType.Folder:
            return self._unpack_folder(fs, stream, header)

    def unpack_chunk_collection(
        self, fs: ChunkyFS, stream: BinaryIO, start: int, end: int
    ):
        stream.seek(start)
        # folders: List[FolderChunk] = []
        # data_chunks: List[RawDataChunk] = []
        while stream.tell() < end:
            self.unpack_chunk(fs, stream)
        if stream.tell() != end:
            # Either change msg name from `Chunk Size` to terminal or somethnig
            #   OR convert terminal positions to 'size' values (by subtracting start).
            raise MismatchError("Chunk Size", stream.tell() - start, end - start)


@dataclass
class ChunkyFSSerializer(ChunkyFSHandler):
    version: Version
    # _chunky_meta_serializer:StreamSerializer[] # NO META in v1.1
    chunk_serializer: ChunkyCollectionHandler

    def read(self, stream: BinaryIO) -> ChunkyFS:
        MagicWord.read_magic_word(stream)
        version = Version.unpack(stream)
        if version != self.version:
            raise VersionMismatchError(version, self.version)
        fs = ChunkyFS()
        essence_meta = {"version": {"major": version.major, "minor": version.minor}}
        fs.setmeta(essence_meta, "essence")
        # meta = None #
        start = stream.tell()
        stream.seek(0, 2)  # jump to end
        end = stream.tell()
        self.chunk_serializer.unpack_chunk_collection(fs, stream, start, end)
        return fs

    def write(self, stream: BinaryIO, chunky: ChunkyFS) -> int:
        raise NotImplementedError
        written = 0
        written += MagicWord.write_magic_word(stream)
        # writing is so much easier than reading
        for folder in chunky.folders:
            written += self.chunk_serializer.pack(stream, folder)
        for file in chunky.folders:
            written += self.chunk_serializer.pack(stream, file)
        return written


chunky_collection_handler = ChunkyCollectionHandler(chunk_header_serializer)

chunky_fs_serializer = ChunkyFSSerializer(version_1p1, chunky_collection_handler)


__all__ = [
    "ChunkyFSSerializer",
    "chunky_fs_serializer",
]
