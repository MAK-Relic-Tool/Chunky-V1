from __future__ import annotations

from os import SEEK_END
from typing import BinaryIO, Union, Optional

from relic.chunky.core.definitions import ChunkType, ChunkFourCC
from relic.chunky.core.serialization import ChunkyFile, ChunkyChunk, ChunkHeader
from relic.core.errors import RelicToolError
from relic.core.lazyio import BinaryWindow, BinaryProxySerializer, BinaryProxy


class ChunkHeaderV1(ChunkHeader, BinaryProxySerializer):
    class Meta:
        TYPE = (0, 4)
        CC = (4, 4)
        VERSION = (8, 4)
        BLOB_SIZE = (12, 4)
        NAME_SIZE = (16, 4)
        NAME_PTR = 20
        FIXED_SIZE = 20
        INT_FORMAT = {"byteorder": "little", "signed": False}

    @property
    def type(self) -> ChunkType:
        buffer = self._serializer.read_bytes(*self.Meta.TYPE)
        value = buffer.decode("ascii")
        return ChunkType(value)

    @type.setter
    def type(self, value: ChunkType) -> None:
        enum_value = value.value
        buffer = enum_value.encode("ascii")
        self._serializer.write_bytes(buffer, self.Meta.TYPE[1])

    @property
    def cc(self) -> ChunkFourCC:
        buffer = self._serializer.read_bytes(*self.Meta.CC)
        value = buffer.decode("ascii")
        return ChunkFourCC(value)

    @cc.setter
    def cc(self, value: ChunkFourCC) -> None:
        cc = value.code
        buffer = cc.encode("ascii")
        self._serializer.write_bytes(buffer, self.Meta.CC[1])

    @property
    def version(self) -> int:
        return self._serializer.int.read(*self.Meta.VERSION, **self.Meta.INT_FORMAT)  # type: ignore

    @version.setter
    def version(self, value: int) -> None:
        self._serializer.int.write(value, *self.Meta.VERSION, **self.Meta.INT_FORMAT)  # type: ignore

    @property
    def name_size(self) -> int:
        return self._serializer.int.read(*self.Meta.NAME_SIZE, **self.Meta.INT_FORMAT)  # type: ignore

    @name_size.setter
    def name_size(self, value: int) -> None:
        self._serializer.int.write(value, *self.Meta.NAME_SIZE, **self.Meta.INT_FORMAT)  # type: ignore

    @property
    def name(self) -> str:
        name_size = self.name_size
        value = self._serializer.c_string.read(
            self.Meta.NAME_PTR, name_size, encoding="ascii"
        )
        return value

    def set_name(self, name: str) -> None:
        name_size = len(name) + (1 if len(name) > 0 and name[-1] != "\0" else 0)
        self._serializer.c_string.write(
            name, self.Meta.NAME_PTR, size=name_size, encoding="ascii", padding="\0"
        )
        self.name_size = name_size

    @property
    def size(self) -> int:
        return self._serializer.int.read(*self.Meta.BLOB_SIZE, **self.Meta.INT_FORMAT)  # type: ignore

    @size.setter
    def size(self, value: int) -> None:
        self._serializer.int.write(value, *self.Meta.BLOB_SIZE, **self.Meta.INT_FORMAT)  # type: ignore


class ChunkV1(BinaryProxySerializer, ChunkyChunk[ChunkHeaderV1]):
    def __init__(self, stream: Union[BinaryIO, BinaryProxy]):
        super().__init__(stream)
        self._header = ChunkHeaderV1(stream)
        start = self._header.name_size + ChunkHeaderV1.Meta.FIXED_SIZE
        size = self._header.size
        self._blob_ptr = (stream, start, size)
        self._terminal = start + size
        self._child_cache: Optional[list[ChunkV1]] = None
        self._has_children: bool = self._header.type is ChunkType.FOLDER

    @property
    def header(self) -> ChunkHeaderV1:
        return self._header

    @property
    def blob(self) -> BinaryWindow:
        return BinaryWindow(*self._blob_ptr, name="Chunky Blob")

    @property
    def total_size(self) -> int:
        return self._terminal

    @property
    def children(self) -> list[ChunkV1]:
        if not self._has_children:
            raise RelicToolError("Cannot iterate Chunks on a Data Chunk")
        if self._child_cache is None:
            read = 0
            children = []
            while read < self._terminal:
                stream, blob_start, blob_size = self._blob_ptr
                child = ChunkV1(
                    BinaryWindow(stream, blob_start + read, blob_size - read)
                )
                children.append(child)
                read += child.total_size
            self._child_cache = children
        return self._child_cache


class ChunkyFileV1(ChunkyFile[None, ChunkV1]):
    ROOT_START = ChunkyFile._MAGIC_VERSION_SIZE

    def __init__(self, parent: BinaryIO, size: Optional[int] = None):
        super().__init__(parent)
        if size is None:
            now = parent.tell()
            size = parent.seek(0, SEEK_END)
            parent.seek(now)
        size -= self.ROOT_START
        self._root = ChunkV1(
            BinaryWindow(parent, self.ROOT_START, size, name="Root Chunk")
        )

    @property
    def header(self) -> None:
        return None

    @property
    def root(self) -> ChunkV1:
        return self._root
