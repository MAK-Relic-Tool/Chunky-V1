import os
from dataclasses import dataclass
from typing import BinaryIO, Generic, Callable, Dict, Iterable, Optional

import fs.path
from fs import ResourceType
from fs.base import FS
from fs.errors import ResourceNotFound, DirectoryExpected, DirectoryExists, FileExists
from fs.info import Info
from relic.chunky.core.definitions import ChunkFourCC, ChunkType, Version, _validate_magic_word, MagicWord
from relic.chunky.core.errors import VersionMismatchError
from relic.chunky.core.filesystem import ChunkyFSHandler
from relic.chunky.core.protocols import StreamSerializer
from relic.chunky.core.serialization import TChunkyHeader, TChunkHeader, ChunkCollectionHandler, _ESSENCE, \
    default_slugify_parts
from relic.core.errors import MismatchError
from relic.core.serialization import BinaryWindow

from relic.chunky.v1.definitions import ChunkHeader


class _LazyEntry:
    def __init__(
            self,
            resource_type,
            name: str,
            fourCC: ChunkFourCC|None, # Is None on root entry
            version:int|None,
            fp: BinaryIO,
            start: int,
            size: int
    ):
        self._resource_type = resource_type
        self._4cc = fourCC
        self._name = name
        self._fp = fp
        self._fp_ptr = None
        self._start = start
        self._size = size
        self._version = version
        self._children: dict[str, _LazyEntry] = {}

    @property
    def name(self) -> str:
        if self._4cc is not None:
            return self._name + "." + self._4cc.code
        else:
            return self._name

    def _fail_if_not_dir(self):
        if self._resource_type != ResourceType.directory:
            raise DirectoryExpected(self._name)

    def child(self, path: str):
        self._fail_if_not_dir()

        node = self._children.get(path)
        if node is None:
            raise ResourceNotFound(path)
        return node

    def get_info(self, namespaces: list[str] | None = None) -> Info:
        info = {}
        basic = {"name": self.name, "is_dir": self._resource_type == ResourceType.directory}
        info["basic"] = basic
        if namespaces and "details" in namespaces:
            details = {"accessed": None, "created": None, "metadata_changed": None, "modified": None,
                       "size": self._size, "type": self._resource_type}
            info["details"] = details
        if namespaces and "essence" in namespaces:
            essence = {"4cc":self._4cc.code,"version":self._version}
            info["essence"] = essence
        if namespaces and "lazy" in namespaces:
            if self._fp_ptr is None:
                with self.openbin() as tmp:
                    old = self._fp.tell()
                    tmp.seek(0,os.SEEK_SET)
                    self._fp_ptr = self._fp.tell()
                    self._fp.seek(old)
            lazy = {"pointer":self._fp_ptr}
            info["lazy"] = lazy
        return Info(info)

    def add_child(self, name: str, type: ChunkType, fourcc: ChunkFourCC|None, version:int|None, sub_start: int, sub_size: int):
        rtype = ResourceType.directory if type == ChunkType.Folder else ResourceType.file
        safe_name = safe_base_name = name.replace("\\", "-").replace("/","-")
        ext = ("." + fourcc.code) if fourcc is not None else ""
        n = 1
        while safe_name + ext in self._children:
            safe_name = safe_base_name + ("-" if safe_base_name != "" else "") + str(n)
            n += 1

        child = _LazyEntry(rtype, safe_name, fourcc,   version, self._fp,self._start + sub_start, sub_size)

        self._children[child.name] = child
        return child

    def listdir(self):
        self._fail_if_not_dir()
        return list(self._children.keys())

    def openbin(self) -> BinaryIO:
        # We allow dir to be open as a bin;
        # A good use case comparison; lets say a for some reason, SGA's were a file and directory in some FS
        # we want to open them as binary files to unpack, but our file system can also natively see the files inside
        return BinaryWindow(self._fp, self._start, self._size)


class LazyChunkyFS(FS):
    def __init__(self, fp: BinaryIO):
        super().__init__()
        self._fp = fp
        now = fp.tell()
        end = fp.seek(0, os.SEEK_END)
        fp.seek(now, os.SEEK_SET)
        self._root = None

    def close(self):  # type: () -> None
        pass
        # self._fp.close()

    def _get_node(self, path: str, parent: bool = False):
        parts = fs.path.parts(path)

        if len(parts) > 0 and parent:
            parts = parts[:-1]

        if len(parts) > 0 and parts[0] == "/":
            parts = parts[1:]

        cur = self._root
        for part in parts:
            cur = cur.child(part)
        return cur

    def listdir(self, path):
        node = self._get_node(path)
        return node.listdir()

    def makedir(self, path, permissions=None, recreate=False):
        raise fs.errors.OperationFailed("Cannot make new directory in lazy FS")

    def openbin(self, path, mode="r", buffering=-1, **options):
        return self._get_node(path).openbin()

    def remove(self, path):
        raise fs.errors.OperationFailed("Cannot remove file in lazy FS")

    def removedir(self, path):
        raise fs.errors.OperationFailed("Cannot remove directory in lazy FS")

    def setinfo(self, path, info):
        raise fs.errors.OperationFailed("Cannot set info in lazy FS")

    def getinfo(self, path, namespaces=None):
        return self._get_node(path).get_info(namespaces=namespaces)

    def setmeta(self, meta, _ESSENCE):
        pass  # TODO


@dataclass
class ChunkyFSSerializer(ChunkyFSHandler, Generic[TChunkyHeader, TChunkHeader]):
    version: Version
    header_serializer: StreamSerializer[TChunkyHeader]
    chunk_header_serializer: StreamSerializer[TChunkHeader]
    header2meta: Callable[[TChunkyHeader], Dict[str, object]]
    meta2header: Callable[[Dict[str, object]], TChunkyHeader]

    def read(self, stream: BinaryIO) -> LazyChunkyFS:
        _validate_magic_word(MagicWord, stream, True)

        version = Version.unpack(stream)
        if version != self.version:
            raise VersionMismatchError(version, self.version)
        header = self.header_serializer.unpack(stream)
        meta = self.header2meta(header)
        meta["version"] = {
            "major": version.major,
            "minor": version.minor,
        }  # manually inject version into metadata

        fs = LazyChunkyFS(stream)
        fs.setmeta(meta, _ESSENCE)

        # Read all chunks into the FS, from start byte to end byte
        fs._root = self.unpack_root(stream, )
        # return created fs
        return fs

    def unpack_root(self, fp: BinaryIO):
        now = fp.tell()
        end = fp.seek(0,os.SEEK_END)
        fp.seek(now, os.SEEK_SET)
        entry = _LazyEntry(ResourceType.directory, "", None, None, fp, now, end-now)
        self.unpack_chunk_collection(entry)
        return entry

    def unpack_chunk_collection(self, entry: _LazyEntry):
        with entry.openbin() as window:
            while True:
                now = window.tell()
                if now >= entry._size:
                    break
                header:ChunkHeader = self.chunk_header_serializer.unpack(window)
                now = window.tell()
                new = entry.add_child(header.name, header.type, header.cc, header.version, now, header.size)
                window.seek(now + header.size, os.SEEK_SET)
            for child in entry._children.values():
                if child._resource_type == ResourceType.directory:
                    self.unpack_chunk_collection(child)

    def write(self, stream: BinaryIO, fs: LazyChunkyFS) -> int:
        raise NotImplementedError
        written: int = MagicWord.write_magic_word(stream)
        # TODO, some warning for chunky meta not matching serializer version?
        #   It will definitely fail if all chunks dont get updated metadata for missing fields, so maybe irrelevant?
        written += self.version.pack(stream)
        # Write the header
        meta = fs.getmeta(_ESSENCE)
        header = self.meta2header(meta)  # type: ignore
        written = self.header_serializer.pack(stream, header)
        # Write chunks from the FS into the stream
        written += self.chunk_serializer.pack_chunk_collection(fs, stream)
        return written
