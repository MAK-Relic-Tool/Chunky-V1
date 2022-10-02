"""
Code used to serialize Chunkies to/from Streams and Filesystems.
"""
from dataclasses import dataclass
from typing import BinaryIO, Dict, cast

from fs.base import FS
from serialization_tools.structx import Struct
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
from relic.core.errors import MismatchError


@dataclass
class _ChunkHeader:
    type: ChunkType
    four_cc: ChunkFourCC
    version: int
    size: int
    name: str


@dataclass
class ChunkHeaderSerializer(StreamSerializer[_ChunkHeader]):
    """Unpacks / Packs V1.1 Chunky Headers"""

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
        except UnicodeDecodeError as exc:
            raise ChunkNameError(name_buffer) from exc
        return _ChunkHeader(chunk_type, chunk_cc, version, size, name)

    def pack(self, stream: BinaryIO, packable: _ChunkHeader) -> int:
        written = 0
        written += self.chunk_type_serializer.pack(stream, packable.type)
        name_buffer = packable.name.encode("ascii")
        args = packable.four_cc, packable.version, packable.type, len(name_buffer)
        written += self.layout.pack(args)
        written += stream.write(name_buffer)
        return written


chunk_header_serializer = ChunkHeaderSerializer(
    chunk_type_serializer, chunk_cc_serializer, Struct("<3L")
)


@dataclass
class ChunkyCollectionHandler:
    """
    Unpacks / Packs Chunky Streams into / from Chunky Filesystems
    """

    header_serializer: ChunkHeaderSerializer

    @staticmethod
    def _header2meta(header: _ChunkHeader) -> Dict[str, Dict[str, object]]:
        return {
            "essence": {
                "name": header.name,
                "version": header.version,
                "4cc": str(header.four_cc),
            }
        }

    @staticmethod
    def _meta2header(meta: Dict[str, Dict[str, object]]) -> _ChunkHeader:
        essence: Dict[str, object] = meta["essence"]
        fourcc: str = cast(str, essence["4cc"])
        version: int = cast(int, essence["version"])
        name: str = cast(str, essence["name"])
        return _ChunkHeader(None, ChunkFourCC(fourcc), version, None, name)  # type: ignore

    @staticmethod
    def _slugifyname(name: str) -> str:
        # Any chunk which references the EssenceFS typically names themselves the full path to the references asset
        #   unfortunately; that's a BAD name in the ChunkyFS; so we need to convert it to a safe ChunkyFS name
        return name.replace("/", "-").replace("\\", "-")

    def _pack_data(self, filesystem: FS, path: str, stream: BinaryIO) -> int:
        info = cast(
            Dict[str, Dict[str, object]], filesystem.getinfo(path, ["essence"]).raw
        )
        header = self._meta2header(info)
        with filesystem.open(path, "rb") as handle:
            data = handle.read()
        header.type = ChunkType.Data
        header.size = len(data)

        written = 0
        written += self.header_serializer.pack(stream, header)
        written += stream.write(data)
        return written

    def _unpack_data(
        self, filesystem: FS, stream: BinaryIO, header: _ChunkHeader
    ) -> None:
        safe_name = self._slugifyname(header.name)
        path = f"{safe_name}.{header.four_cc.code}"
        metadata = self._header2meta(header)
        data = stream.read(header.size)
        with filesystem.open(path, "wb") as handle:
            handle.write(data)
        filesystem.setinfo(path, metadata)

    def _pack_folder(self, filesystem: FS, stream: BinaryIO) -> int:
        info = cast(
            Dict[str, Dict[str, object]], filesystem.getinfo("/", ["essence"]).raw
        )
        header = self._meta2header(info)
        header.type = ChunkType.Folder
        header.size = 0
        write_back = stream.tell()

        written = 0
        written += self.header_serializer.pack(stream, header)
        header.size = self.pack_chunk_collection(filesystem, stream)
        written += header.size

        now = stream.tell()
        stream.seek(write_back)
        self.header_serializer.pack(stream, header)
        stream.seek(now)

        return written

    def _unpack_folder(
        self, filesystem: FS, stream: BinaryIO, header: _ChunkHeader
    ) -> None:
        # Folders shouldn't need to be slugged, but why risk it?
        safe_name = self._slugifyname(header.name)
        path = f"{safe_name}.{header.four_cc.code}"
        metadata = self._header2meta(header)
        start, size = stream.tell(), header.size
        dir_fs = filesystem.makedir(path)
        dir_fs.setinfo("/", metadata)
        self.unpack_chunk_collection(dir_fs, stream, start, start + size)

    def unpack_chunk(self, filesystem: FS, stream: BinaryIO) -> None:
        """
        Unpacks a chunk from the stream, and inserts it into the filesystem.
        """
        header = self.header_serializer.unpack(stream)
        if header.type == ChunkType.Data:
            return self._unpack_data(filesystem, stream, header)
        if header.type == ChunkType.Folder:
            return self._unpack_folder(filesystem, stream, header)
        raise NotImplementedError(
            "This shouldn't be reached, and I didn't write a proper exception yet."
        )  # TODO proper exception

    def pack_chunk(self, parent_fs: FS, path: str, stream: BinaryIO) -> int:
        """
        Packs the chunk from the filesystem at the given path into the stream.
        If path points to a file, a Data Chunk is created and packed.
        If path points to a folder, a Folder Chunk is created and packed.
        """
        info = parent_fs.getinfo(path)
        if info.is_dir:
            sub_fs = parent_fs.opendir(path)
            return self._pack_folder(sub_fs, stream)
        return self._pack_data(parent_fs, path, stream)

    def unpack_chunk_collection(
        self, filesystem: FS, stream: BinaryIO, start: int, end: int
    ) -> None:
        """
        Unpacks child chunks from the stream, and adds them to the filesystem.
        """
        stream.seek(start)
        # folders: List[FolderChunk] = []
        # data_chunks: List[RawDataChunk] = []
        while stream.tell() < end:
            self.unpack_chunk(filesystem, stream)
        if stream.tell() != end:
            # Either change msg name from `Chunk Size` to terminal or somethnig
            #   OR convert terminal positions to 'size' values (by subtracting start).
            raise MismatchError("Chunk Size", stream.tell() - start, end - start)

    def pack_chunk_collection(self, filesystem: FS, stream: BinaryIO) -> int:
        """
        Packs the children of the root chunk.
        The root chunk is located at "/", relative to the filesystem's root.
        I.E. If a directory SubFS is passed in, that directory is the root chunk, and it's children will be packed.
        """
        written = 0
        for path in filesystem.listdir("/"):
            written += self.pack_chunk(filesystem, path, stream)
        return written


@dataclass
class ChunkyFSSerializer(ChunkyFSHandler):
    """
    A handler which contains logic to read/write Chunky V1 streams and construct ChunkyFS objects.
    """

    version: Version
    # _chunky_meta_serializer:StreamSerializer[] # NO META in v1.1
    chunk_serializer: ChunkyCollectionHandler

    def read(self, stream: BinaryIO) -> ChunkyFS:
        MagicWord.read_magic_word(stream)
        version = Version.unpack(stream)
        if version != self.version:
            raise VersionMismatchError(version, self.version)
        chunky_fs = ChunkyFS()
        essence_meta = {"version": {"major": version.major, "minor": version.minor}}
        chunky_fs.setmeta(essence_meta, "essence")
        # meta = None #
        start = stream.tell()
        stream.seek(0, 2)  # jump to end
        end = stream.tell()
        self.chunk_serializer.unpack_chunk_collection(chunky_fs, stream, start, end)
        return chunky_fs

    def write(self, stream: BinaryIO, chunky_fs: ChunkyFS) -> int:
        written = 0
        written += MagicWord.write_magic_word(stream)
        written += self.version.pack(stream)
        written += self.chunk_serializer.pack_chunk_collection(chunky_fs, stream)
        return written


chunky_collection_handler = ChunkyCollectionHandler(chunk_header_serializer)

chunky_fs_serializer = ChunkyFSSerializer(version_1p1, chunky_collection_handler)

__all__ = [
    "ChunkyFSSerializer",
    "chunky_fs_serializer",
]
