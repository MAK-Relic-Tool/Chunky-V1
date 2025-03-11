from __future__ import annotations
import itertools
import os
from dataclasses import dataclass
from io import BytesIO
from os.path import splitext
from typing import BinaryIO, Generic, Callable, Dict, Tuple, Any, Optional

import fs.path
from fs import ResourceType
from fs.base import FS
from fs.errors import ResourceNotFound, DirectoryExpected
from fs.info import Info
from relic.chunky.core.definitions import ChunkFourCC, ChunkType, Version, _validate_magic_word, MagicWord
from relic.chunky.core.errors import VersionMismatchError
from relic.chunky.core.filesystem import ChunkyFSHandler
from relic.chunky.core.protocols import StreamSerializer
from relic.chunky.core.serialization import TChunkyHeader, TChunkHeader, _ESSENCE
from relic.core.errors import RelicToolError
from relic.core.lazyio import BinaryWrapper
from relic.core.serialization import BinaryWindow

from relic.chunky.v1.definitions import ChunkHeader
from typing import Iterator


class _Entry:
    def __init__(self, resource_type: ResourceType, name: str, fourcc: ChunkFourCC | None, version: int | None,
                 openable: bool = False) -> None:
        self._resource_type = resource_type
        self._name = name

        self._4cc = fourcc
        self._version = version
        self._children: Optional[Dict[str, _Entry]] = {} if resource_type == ResourceType.directory else None
        self._openable = openable

    @property
    def children(self) -> Optional[Dict[str, _Entry]]:
        return self._children

    def _fail_if_not_dir(self):
        if self._resource_type != ResourceType.directory:
            raise DirectoryExpected(self._name)

    def _children_null_error(self):
        return RelicToolError("Directory object invalid, children was not initialized!")

    def child(self, path: str):
        self._fail_if_not_dir()
        if self._children is None:
            raise self._children_null_error()

        node = self._children.get(path)
        if node is None:
            raise ResourceNotFound(path)
        return node

    def remove_child(self, name: str, rtype: ResourceType | None = None):
        self._fail_if_not_dir()
        if self._children is None:
            raise self._children_null_error()

        if name not in self._children:
            raise fs.errors.ResourceNotFound(name)
        if rtype is not None and self._children[name]._resource_type != rtype:
            if rtype == ResourceType.directory:
                raise fs.errors.DirectoryExpected(name)
            elif rtype == ResourceType.file:
                raise fs.errors.FileExpected(name)
            else:
                raise fs.errors.ResourceError(name)
        self._children[name].close()
        del self._children[name]

    @property
    def name(self) -> str:
        return self._name

    @property
    def size(self) -> int:
        raise NotImplementedError()

    def close(self):
        raise NotImplementedError()

    def get_info(self, namespaces: list[str] | None = None):
        info = {}
        info["basic"] = self._get_info_basic()
        if namespaces and "details" in namespaces:
            info["details"] = self._get_info_details()
        if namespaces and "essence" in namespaces:
            info["essence"] = self._get_info_essence()
        if namespaces and "lazy" in namespaces:
            info["lazy"] = self._get_info_lazy()
        return Info(info)

    def set_info(self, info: dict[str, dict[str, Any]]):
        if "basic" in info:
            self._name = info["basic"].get("name", self._name)
        if "essence" in info:
            self._version = info["essence"].get("version", self._version)
            cc = info["essence"].get("4cc", self._4cc.code if self._4cc is not None else None)
            self._4cc = ChunkFourCC(cc) if cc is not None else None

    def _get_info_basic(self):
        return {"name": self.name, "is_dir": self._resource_type == ResourceType.directory}

    def _get_info_essence(self):
        return {"4cc": self._4cc.code, "version": self._version}

    def _get_info_details(self):
        return {"accessed": None, "created": None, "metadata_changed": None, "modified": None,
                "size": self.size, "type": self._resource_type}

    def _get_info_lazy(self):
        return {"pointer": None}

    def openbin(self) -> BinaryIO:
        raise NotImplementedError

    def listdir(self):
        self._fail_if_not_dir()
        return list(self._children.keys())

    def add_child(self, child: '_Entry'):
        self._fail_if_not_dir()
        if self._children is None:
            raise self._children_null_error()

        if child.name in self._children:
            raise fs.errors.ResourceError(child.name)
        self._children[child.name] = child

    @property
    def openable(self) -> bool:
        return self._openable


class _MemEntry(_Entry):
    def __init__(self, resource_type: ResourceType, name: str, fourcc: ChunkFourCC | None, version: int | None):
        super().__init__(resource_type, name, fourcc, version, openable=resource_type is ResourceType.file)
        self._handle = BytesIO() if resource_type is ResourceType.file else None

    @property
    def size(self) -> int:
        if self._handle is not None:
            return len(self._handle.getbuffer())
        return 0

    def close(self):
        if self._handle is not None:
            self._handle.close()

    def openbin(self) -> BinaryIO:
        if not self.openable:
            raise fs.errors.OperationFailed("Directory is not mapped to a lazy file.")
        if self._handle is None:
            raise RelicToolError(f"{self.name}'s handle was not assigned!")

        self._handle.seek(0)  # FIXME: This is a disaster waiting to happen
        return BinaryWrapper(self._handle, close_parent=False)


class _LazyEntry(_Entry):
    def __init__(self, resource_type, name: str, fourcc: ChunkFourCC | None, version: int | None, fp: BinaryIO,
                 start: int, size: int, parent_backreference: _Entry | None = None):
        super().__init__(resource_type, name, fourcc, version, openable=True)
        self._fp = fp
        self._fp_ptr = None
        self._start = start
        self._size = size
        self._parent = parent_backreference

    @property
    def name(self) -> str:
        return self._name

    @property
    def size(self):
        return self._size

    def _mark_unopenable(self):
        self._openable = False
        if self._parent is not None and hasattr(self._parent, "_mark_unopenable"):
            self._parent._mark_unopenable()

    def add_child(self, child: '_Entry'):
        # Modifying the structure breaks openability
        super().add_child(child)
        self._mark_unopenable()

    def remove_child(self, path: str, rtype: ResourceType | None = None):
        # Modifying the structure breaks openability
        super().remove_child(path, rtype)
        self._mark_unopenable()

    def _create_child(self, name: str, type: ChunkType, fourcc: ChunkFourCC | None, version: int | None, sub_start: int,
                      sub_size: int):
        self._fail_if_not_dir()
        if self._children is None:
            raise self._children_null_error()

        rtype = ResourceType.directory if type == ChunkType.Folder else ResourceType.file
        safe_base_name, ext = splitext(name.replace("\\", "-").replace("/", "-"))
        fourcc_ext = ("." + fourcc.code) if fourcc is not None else ""
        safe_name = safe_base_name + ext + fourcc_ext
        n = 1
        while safe_name in self._children:
            safe_name = safe_base_name + ("-" if safe_base_name != "" else "") + str(n) + ext + fourcc_ext
            n += 1

        child = _LazyEntry(rtype, safe_name, fourcc, version, self._fp, self._start + sub_start, sub_size)

        self._children[child.name] = child
        return child

    def _get_info_lazy(self):
        if self._fp_ptr is None:
            with self.openbin() as tmp:
                old = self._fp.tell()
                tmp.seek(0, os.SEEK_SET)
                self._fp_ptr = self._fp.tell()
                self._fp.seek(old)
        return {"pointer": self._fp_ptr}

    def openbin(self) -> BinaryIO:
        if not self.openable:
            raise fs.errors.OperationFailed("Folder structure has been modified, cannot be opened directly.")
        # We allow dir to be open as a bin;
        # A good use case comparison; lets say a for some reason, SGA's were a file and directory in some FS
        # we want to open them as binary files to unpack, but our file system can also natively see the files inside
        return BinaryWindow(self._fp, self._start, self._size)


class LazyChunkyFS(FS):
    def __init__(self, fp: BinaryIO | None, own_fp: bool = False, writable: bool = False):
        super().__init__()
        self._own_fp = own_fp and fp is not None
        self._fp = fp
        self._root: _Entry = _MemEntry(ResourceType.directory, "", None, None)
        self._writable = writable

    def close(self) -> None:
        if self._own_fp and self._fp is not None:
            self._fp.close()

    def _get_node(self, path: str, parent: bool = False) -> Tuple[_Entry, str]:
        parts = fs.path.parts(path)

        remaining = "/"
        if len(parts) > 0 and parent:
            remaining = parts[-1]
            parts = parts[:-1]

        if len(parts) > 0 and parts[0] == "/":
            parts = parts[1:]

        cur = self._root
        for part in parts:
            cur = cur.child(part)
        return cur, remaining

    def listdir(self, path):
        node, _ = self._get_node(path)
        return node.listdir()

    def makedir(self, path, permissions=None, recreate=False):
        node, subpath = self._get_node(path, parent=True)
        if node.has_child(subpath):
            if not recreate:
                raise fs.errors.DirectoryExists(path)
        else:
            node.add_child(_MemEntry(ResourceType.file, subpath, None, None))

    def openbin(self, path, mode="r", buffering=-1, **options):
        node, _ = self._get_node(path)
        return node.openbin()

    def remove(self, path):
        node, subpath = self._get_node(path, parent=True)
        node.remove_child(subpath, resource_type=ResourceType.file)

    def removedir(self, path):
        node, subpath = self._get_node(path, parent=True)
        node.remove_child(subpath, resource_type=ResourceType.directory)

    def setinfo(self, path, info):
        node, _ = self._get_node(path)
        node.set_info(info)

    def getinfo(self, path, namespaces=None):
        node, _ = self._get_node(path)
        return node.get_info(namespaces=namespaces)

    def getmeta(self, namespace: str = "standard") -> dict[str, object]:
        from relic.chunky.v1.definitions import version
        if namespace == "essence":
            return {"version":
                {
                    "major": version.major,
                    "minor": version.minor,
                }
            }
        return super().getmeta(namespace)

    def setmeta(self, meta, ns: str) -> None:
        raise NotImplementedError


@dataclass
class ChunkyFSSerializer(ChunkyFSHandler, Generic[TChunkyHeader, TChunkHeader]):
    version: Version
    chunk_header_serializer: StreamSerializer[TChunkHeader]

    def read(self, stream: BinaryIO) -> LazyChunkyFS:
        _validate_magic_word(MagicWord, stream, True)

        version = Version.unpack(stream)
        if version != self.version:
            raise VersionMismatchError(version, self.version)

        fs = LazyChunkyFS(stream)

        fs._root = self.unpack_root(stream)
        return fs

    def unpack_root(self, fp: BinaryIO):
        now = fp.tell()
        end = fp.seek(0, os.SEEK_END)
        fp.seek(now, os.SEEK_SET)
        entry = _LazyEntry(ResourceType.directory, "", None, None, fp, now, end - now)
        self.unpack_chunk_collection(entry)
        return entry

    def unpack_chunk_collection(self, entry: _LazyEntry):
        with entry.openbin() as window:
            while True:
                check_now = window.tell()
                if check_now >= entry._size:
                    break
                header: ChunkHeader = self.chunk_header_serializer.unpack(window)
                now = window.tell()
                new = entry._create_child(header.name, header.type, header.cc, header.version, now, header.size)
                window.seek(now + header.size, os.SEEK_SET)
            entry._children_null_error()
            for child in entry._children.values():
                if child._resource_type == ResourceType.directory:
                    self.unpack_chunk_collection(child)

    def pack_root(self, fp: BinaryIO, entry: _Entry) -> int:
        if entry.openable:
            with entry.openbin() as h:
                return fp.write(h.read())
        else:
            if entry._children is None:
                raise RelicToolError("Root chunk was not a folder!")

            size = 0
            for child in entry._children.values():  # should only be one
                size += self.pack_entry(fp, child)
            return size

    def pack_entry(self, fp: BinaryIO, entry: _Entry) -> int:
        # Lazy Directories will automagically write all contents to the stream
        if entry.openable:
            ctype = ChunkType.Folder if entry._resource_type is ResourceType.directory else ChunkType.Data
            header = ChunkHeader(ctype, entry._4cc, entry._version, entry.size, entry._name)
            wrote = self.chunk_header_serializer.pack(fp, header)

            with entry.openbin() as h:
                wrote += fp.write(h.read())

            return wrote

        if entry._resource_type == ResourceType.file:
            raise NotImplementedError("File was not openable!")

        write_back = fp.tell()
        header = ChunkHeader(ChunkType.Folder, entry._4cc, entry._version, 0, entry.name)
        wrote = self.chunk_header_serializer.pack(fp, header)
        size = 0
        if entry._children is None:
            raise entry._children_null_error()

        for child in entry._children.values():
            size += self.pack_entry(fp, child)
        header.size = size
        now = fp.tell()
        fp.seek(write_back, os.SEEK_SET)
        self.chunk_header_serializer.pack(fp, header)
        fp.seek(now, os.SEEK_CUR)
        return wrote + size

    def write(self, stream: BinaryIO, fs: LazyChunkyFS, path: str = "/") -> int:
        written: int = MagicWord.write_magic_word(stream)
        # TODO, some warning for chunky meta not matching serializer version?
        #   It will definitely fail if all chunks dont get updated metadata for missing fields, so maybe irrelevant?
        written += self.version.pack(stream)
        # Write the header
        if path == "/":
            root, _ = fs._get_node(path)
            written += self.pack_root(stream, root)
        else:
            node, _ = fs._get_node(path)
            written += self.pack_entry(stream, node)
        return written


def rglob_cc(f: FS, *cc: ChunkFourCC) -> Iterator[str]:
    codes = [c.code for c in cc]
    for step in f.walk(namespaces=["essence"]):
        path, dirs, files = step
        info: Info
        for info in itertools.chain(dirs, files):
            if info.get("essence", "4cc") in codes:
                yield fs.path.join(path, info.name)
