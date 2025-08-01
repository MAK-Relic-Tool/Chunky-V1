from __future__ import annotations

import contextlib
import logging
import os
from io import BytesIO
from typing import (
    Optional,
    Dict,
    Any,
    BinaryIO,
    Tuple,
    Mapping,
    Collection,
    cast,
    Type,
    Generator,
)

import fs.errors
import fs.path
from fs import ResourceType
from fs.base import FS
from fs.errors import DirectoryExpected, ResourceNotFound
from fs.info import Info
from fs.subfs import SubFS
from relic.chunky.core.chunkyfs import ChunkyFS
from relic.chunky.core.definitions import ChunkFourCC, ChunkType, Version, MAGIC_WORD
from relic.chunky.core.serialization import (
    VersionSerializer,
    ChunkTypeSerializer,
    ChunkFourCCSerializer,
)
from relic.core.errors import RelicToolError
from relic.core.lazyio import BinaryWindow, BinaryWrapper, chunk_copy

from relic.chunky.v1.serialization import ChunkyFileV1, ChunkV1, ChunkHeaderV1

from relic.chunky.v1.definitions import version as version_1p1

logger = logging.getLogger(__file__)


class _Entry:
    def __init__(
        self,
        resource_type: ResourceType,
        name: str,
        fourcc: ChunkFourCC | None,
        version: int | None,
        openable: bool = False,
        header_name:str=None
    ) -> None:
        self._resource_type = resource_type
        self._name = name
        self._header_name = header_name

        self._4cc = fourcc
        self._version = version
        self._children: Optional[Dict[str, _Entry]] = (
            {} if resource_type == ResourceType.directory else None
        )
        self._can_open = openable

    @property
    def children(self) -> Optional[Dict[str, _Entry]]:
        return self._children

    def _fail_if_not_dir(self) -> None:
        if self._resource_type != ResourceType.directory:
            raise DirectoryExpected(self._name)

    def _children_null_error(self) -> RelicToolError:
        return RelicToolError("Directory object invalid, children was not initialized!")

    def child(self, path: str) -> _Entry:
        self._fail_if_not_dir()
        if self._children is None:
            raise self._children_null_error()

        node = self._children.get(path)
        if node is None:
            raise ResourceNotFound(path)
        return node

    def remove_child(self, name: str, rtype: ResourceType | None = None) -> None:
        self._fail_if_not_dir()
        if self._children is None:
            raise self._children_null_error()

        if name not in self._children:
            raise fs.errors.ResourceNotFound(name)
        if rtype is not None and self._children[name]._resource_type != rtype:
            if rtype == ResourceType.directory:
                raise fs.errors.DirectoryExpected(name)
            if rtype == ResourceType.file:
                raise fs.errors.FileExpected(name)
            raise fs.errors.ResourceError(name)
        self._children[name].close()
        del self._children[name]

    @property
    def name(self) -> str:
        return self._name

    @property
    def size(self) -> int:
        raise NotImplementedError()

    def close(self) -> None:
        raise NotImplementedError()

    def get_info(self, namespaces: Collection[str] | None = None) -> Info:
        info = {}
        info["basic"] = self._get_info_basic()
        if namespaces and "details" in namespaces:
            info["details"] = self._get_info_details()
        if namespaces and "essence" in namespaces:
            info["essence"] = self._get_info_essence()
        if namespaces and "lazy" in namespaces:
            info["lazy"] = self._get_info_lazy()
        return Info(info)

    def set_info(self, info: Mapping[str, Mapping[str, Any]]) -> None:
        if "basic" in info:
            self._name = info["basic"].get("name", self._name)
        if "essence" in info:
            self._version = info["essence"].get("version", self._version)
            cc = info["essence"].get(
                "4cc", self._4cc.code if self._4cc is not None else None
            )
            self._4cc = ChunkFourCC(cc) if cc is not None else None
            self._header_name = info["essence"].get("name", self._name)

    def _get_info_basic(self) -> dict[str, object]:
        return {
            "name": self.name,
            "is_dir": self._resource_type == ResourceType.directory,
        }

    def _get_info_essence(self) -> dict[str, object]:
        return {
            "4cc": self._4cc.code if self._4cc is not None else None,
            "version": self._version,
        }

    def _get_info_details(self) -> dict[str, object]:
        return {
            "accessed": None,
            "created": None,
            "metadata_changed": None,
            "modified": None,
            "size": self.size,
            "type": self._resource_type,
        }

    def _get_info_lazy(self) -> dict[str, object]:
        return {"pointer": None}

    def openbin(self) -> BinaryIO:
        raise NotImplementedError

    def listdir(self) -> list[str]:
        self._fail_if_not_dir()
        if self._children is None:
            raise self._children_null_error()
        return list(self._children.keys())

    def add_child(self, child: "_Entry") -> None:
        self._fail_if_not_dir()
        if self._children is None:
            raise self._children_null_error()

        if child.name in self._children:
            raise fs.errors.ResourceError(child.name)
        self._children[child.name] = child

    @property
    def can_open(self) -> bool:
        return self._can_open

    def has_child(self, path: str) -> bool:
        self._fail_if_not_dir()
        if self._children is None:
            raise self._children_null_error()
        return path in self._children


class _MemEntry(_Entry):
    def __init__(
        self,
        resource_type: ResourceType,
        name: str,
        fourcc: ChunkFourCC | None,
        version: int | None,
        data: Optional[bytes] = None,
        header_name:Optional[str]=None
    ):
        super().__init__(
            resource_type,
            name,
            fourcc,
            version,
            openable=resource_type is ResourceType.file,
        )
        self._handle = (
            (BytesIO(data) if data is not None else BytesIO())
            if resource_type is ResourceType.file
            else None
        )

    @property
    def size(self) -> int:
        if self._handle is not None:
            return len(self._handle.getbuffer())
        return 0

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()

    def openbin(self) -> BinaryIO:
        if not self.can_open:
            raise fs.errors.OperationFailed("Directory is not mapped to a lazy file.")
        if self._handle is None:
            raise RelicToolError(f"{self.name}'s handle was not assigned!")

        self._handle.seek(0)  # FIXME: This is a disaster waiting to happen
        return BinaryWrapper(self._handle, close_parent=False)


class _LazyEntry(_Entry):
    def __init__(
        self,
        resource_type: ResourceType,
        name: str,
        fourcc: ChunkFourCC | None,
        version: int | None,
        fp: BinaryIO,
        blob_start: int,
        blob_size: int,
        parent_backreference: _Entry | None = None,
        header_name: Optional[str]=None

    ):
        super().__init__(resource_type, name, fourcc, version, openable=True, header_name=header_name)
        self._fp = fp
        self._fp_ptr: Optional[int] = None
        self._start = blob_start
        self._size = blob_size
        self._parent = parent_backreference

    @property
    def name(self) -> str:
        return self._name

    @property
    def size(self) -> int:
        return self._size

    def close(self) -> None:
        pass

    def _mark_unopenable(self) -> None:
        self._can_open = False
        if self._parent is not None and hasattr(self._parent, "_mark_unopenable"):
            self._parent._mark_unopenable()

    def add_child(self, child: "_Entry") -> None:
        # Modifying the structure breaks openability
        super().add_child(child)
        self._mark_unopenable()

    def remove_child(self, name: str, rtype: ResourceType | None = None) -> None:
        # Modifying the structure breaks openability
        super().remove_child(name, rtype)
        self._mark_unopenable()

    def _get_info_lazy(self) -> dict[str, object]:
        # couldnt we just seek the parent ptr to start and then add start?
        # Even then, this requires that _fp be the absolute ptr to the file
        if self._fp_ptr is None:
            with self.openbin() as tmp:
                old = self._fp.tell()
                tmp.seek(0, os.SEEK_SET)
                self._fp_ptr = self._fp.tell()
                self._fp.seek(old)
        return {"pointer": self._fp_ptr}

    def openbin(self) -> BinaryIO:
        if not self.can_open:
            raise fs.errors.OperationFailed(
                "Folder structure has been modified, cannot be opened directly."
            )
        # We allow dir to be open as a bin;
        # A good use case comparison; lets say a for some reason, SGA's were a file and directory in some FS
        # we want to open them as binary files to unpack, but our file system can also natively see the files inside
        return BinaryWindow(self._fp, self._start, self._size)

    def unlazy(self) -> _MemEntry:
        if self.children is None:  # Assume file
            with self.openbin() as tmp:
                data = tmp.read()
        else:
            data = None
        return _MemEntry(
            self._resource_type, self._name, self._4cc, self._version, data, self._header_name
        )


class ChunkyFileWriter:
    def __init__(
        self, handle: BinaryIO, safe_mode: bool, *, warn_multiple_roots: bool = True
    ):
        self._output = handle
        self._writer = handle if not safe_mode else BytesIO()
        self.warn_multiple_roots = warn_multiple_roots

    def __enter__(self) -> ChunkyFileWriter:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.flush()

    def flush(self) -> None:
        if self._writer is self._output:
            return
        chunk_copy(self._writer, self._output)

    def write_chunky_universal_header(self, version: Optional[Version] = None) -> int:
        if version is None:
            version = version_1p1

        written = MAGIC_WORD.write(self._writer)
        written += VersionSerializer.write(self._writer, version)
        return written

    def write_fs(self, chunkyfs: FS) -> None:
        self.write_chunky_universal_header()
        if isinstance(chunkyfs, ChunkyFSV1):
            self._write_fs_root_fast(chunkyfs._root)
        else:
            self._write_fs_root_generic(chunkyfs)

    def _write_fs_root_generic(self, chunkyfs: FS) -> None:
        root_children = chunkyfs.listdir("/")
        if self.warn_multiple_roots and len(root_children) > 1:
            logger.warning(
                "Multiple chunks were found in the root, this may mean an error occurred"
            )
        for child in root_children:
            self._write_fs_chunk_generic(chunkyfs, child)

    def _write_fs_chunk_generic(self, chunkyfs: FS, child: str) -> None:
        info: Info = chunkyfs.getinfo(child, ["essence"])
        name = info.get("essence","name")
        _fourcc = info.get("essence", "4cc")
        fourcc = ChunkFourCC(_fourcc)
        version = info.get("essence", "version")
        if info.is_file:
            with chunkyfs.openbin(child, "rb") as r:
                self._write_file_chunk(fourcc, name, version, r)
        else:
            with self._write_folder_chunk(fourcc, name, version):
                with chunkyfs.opendir(child) as subchunkyfs:
                    for subchild in subchunkyfs.listdir("/"):
                        self._write_fs_chunk_generic(subchunkyfs, subchild)

    def _write_chunk_header(
        self,
        chunk_type: ChunkType,
        cc: ChunkFourCC,
        name: str,
        version: int,
        size: Optional[int] = None,
    ) -> int:
        written = 0
        written += ChunkTypeSerializer.pack(self._writer, chunk_type)
        written += ChunkFourCCSerializer.pack(self._writer, cc)

        version_buffer = version.to_bytes(
            ChunkHeaderV1.Meta.VERSION[1], **ChunkHeaderV1.Meta.INT_FORMAT  # type: ignore
        )
        written += self._writer.write(version_buffer)

        size = size if size is not None else 0
        size_buffer = size.to_bytes(
            ChunkHeaderV1.Meta.NAME_SIZE[1], **ChunkHeaderV1.Meta.INT_FORMAT  # type: ignore
        )
        written += self._writer.write(size_buffer)

        name_buffer = (
            name + ("\0" if len(name) > 0 and name[-1] != "\0" else "")
        ).encode("ascii")
        name_size_buffer = len(name_buffer).to_bytes(
            ChunkHeaderV1.Meta.BLOB_SIZE[1], **ChunkHeaderV1.Meta.INT_FORMAT  # type: ignore
        )
        written += self._writer.write(name_size_buffer)
        written += self._writer.write(name_buffer)
        return written

    @contextlib.contextmanager
    def _auto_write_chunk_header(
        self, chunk_type: ChunkType, cc: ChunkFourCC, name: str, version: int
    ) -> Generator[None, None, None]:
        now = self._writer.tell()
        self._write_chunk_header(chunk_type, cc, name, version)
        blob_start = self._writer.tell()
        yield
        blob_end = self._writer.tell()
        self._writer.seek(now + ChunkHeaderV1.Meta.BLOB_SIZE[0])
        size = blob_end - blob_start
        size_buffer = size.to_bytes(
            ChunkHeaderV1.Meta.NAME_SIZE[1], **ChunkHeaderV1.Meta.INT_FORMAT  # type: ignore
        )
        self._writer.write(size_buffer)
        self._writer.seek(blob_end)

    @contextlib.contextmanager
    def _write_folder_chunk(
        self, cc: ChunkFourCC, name: str, version: int
    ) -> Generator[None, None, None]:
        with self._auto_write_chunk_header(ChunkType.FOLDER, cc, name, version):
            # Before yield will write dummy header
            yield
            # After yield will fix size in the header

    def _write_file_chunk(
        self, cc: ChunkFourCC, name: str, version: int, reader: BinaryIO
    ) -> None:
        with self._auto_write_chunk_header(ChunkType.DATA, cc, name, version):
            chunk_copy(reader, self._writer)

    def _write_fs_root_fast(self, entry: _Entry) -> None:
        if entry.children is None:
            self._write_fs_chunk_fast(
                entry
            )  # I guess we allow data chunk to be root? It never should be one
        else:
            if self.warn_multiple_roots and len(entry.children.values()) > 1:
                logger.warning(
                    "Multiple chunks were found in the root, this may mean an error occurred"
                )
            for child in entry.children.values():
                self._write_fs_chunk_fast(child)

    def _write_fs_chunk_fast(self, entry: _Entry) -> None:
        if entry.children is not None:
            if entry.can_open:  # Lazy Folder
                with entry.openbin() as r:
                    chunk_copy(r, self._writer)
            else:  # Mem Folder
                with self._write_folder_chunk(entry._4cc, entry._header_name, entry._version):  # type: ignore
                    for child in entry.children.values():
                        self._write_fs_chunk_fast(child)
        else:
            with entry.openbin() as r:
                self._write_file_chunk(entry._4cc, entry._header_name, entry._version, r)  # type: ignore


class ChunkyFSV1(ChunkyFS):
    def __init__(
        self,
        fp: BinaryIO | None,
        parse_handle: bool = False,
        in_memory: bool = False,
        own_fp: bool = False,
        writable: bool = False,
    ):
        super().__init__()
        self._own_fp = own_fp and fp is not None
        self._fp = fp
        self._update_fp = (
            own_fp and writable and fp is not None and not in_memory
        )  # Thats an UGLY doozy of a condition
        self._root: _Entry = _MemEntry(ResourceType.directory, "", None, None)
        self._writable = writable
        self._lazy_file: Optional[ChunkyFileV1] = None
        if parse_handle:
            if fp is None:
                raise RelicToolError("cannot parse a null handle!")
            self._lazy_file = ChunkyFileV1(fp)
            self._load_lazy(self._lazy_file)

            if in_memory:
                self._unlazy()

    def _load_lazy(self, file: ChunkyFileV1) -> None:
        def _load_lazy_entry(
            chunk: ChunkV1, parent: Optional[_Entry], cc_count_map:dict[ChunkFourCC,int], start:int
        ) -> _LazyEntry:
            header = chunk.header
            is_file = header.type == ChunkType.DATA

            fp = cast(BinaryIO, self._fp)
            cc_count = cc_count_map.get(header.cc, 0)
            cc_count_map[header.cc] = cc_count + 1
            safe_cc = header.cc.code.replace("\0","") # some CC are 3 character codes with a null character
            # we cant cheat by using blob_fp/blob_start/blob_size as is; lazy entry needs all ptrs to be relative to the root fp
            # we CAN cheat by using them to simplify our logic; blob_start + blob_size is the total size of the chunk
            _, blob_start, blob_size = chunk._blob_ptr
            entry = _LazyEntry(
                ResourceType.file if is_file else ResourceType.directory,
                f"{cc_count}.{safe_cc}",
                header.cc,
                header.version,
                fp,
                start + blob_start,
                blob_size,
                parent,
                header.name
            )
            if not is_file:
                child_cc_count_map = {}
                child_offset = 0
                for child_chunk in chunk.children:
                    child = _load_lazy_entry(
                        child_chunk,
                        entry, child_cc_count_map,
                        start + blob_start + child_offset # start is the start of this chunk, blob_start is the start of the blbo of this chunk (where we read the first child) and child_offset is the # of bytes read so far
                    )
                    entry.add_child(child)
                    child_offset += child_chunk.total_size
            return entry

        root_child = _load_lazy_entry(
            file.root,
            self._root, {},
            file.ROOT_START
        )
        self._root.add_child(root_child)

    def _unlazy(self) -> None:
        if self._lazy_file is None:
            return

        def unlazy_entry(_entry: _Entry) -> _Entry:
            if isinstance(_entry, _LazyEntry):
                return _entry.unlazy()

            if _entry.children is not None:
                for key, entry in _entry.children.items():
                    _entry.children[key] = unlazy_entry(entry)
            return _entry

        # self._lazy_file.header # header is None in v1, so we are lucky, other versions need to cache a mem copy of the header
        self._root = unlazy_entry(self._root)
        self._lazy_file = None

    def _get_node(self, path: str, parent: bool = False) -> Tuple[_Entry, str]:
        parts = fs.path.parts(path)

        remaining = "/"
        if len(parts) > 0 and parent:
            remaining = parts[-1]
            parts = parts[:-1]

        if len(parts) > 0 and parts[0] in ["/","./"]:
            parts = parts[1:]

        cur = self._root
        for part in parts:
            cur = cur.child(part)
        return cur, remaining

    def listdir(self, path: str) -> list[str]:
        node, _ = self._get_node(path)
        return node.listdir()

    def makedir(
        self, path: str, permissions: Optional[Any] = None, recreate: bool = False
    ) -> SubFS[FS]:
        node, subpath = self._get_node(path, parent=True)
        if node.has_child(subpath):
            if not recreate:
                raise fs.errors.DirectoryExists(path)
        else:
            node.add_child(_MemEntry(ResourceType.file, subpath, None, None))
        return self.opendir(path)

    def openbin(
        self, path: str, mode: str = "r", buffering: int = -1, **options: Any
    ) -> BinaryIO:
        node, _ = self._get_node(path)
        return node.openbin()

    def remove(self, path: str) -> None:
        node, subpath = self._get_node(path, parent=True)
        node.remove_child(subpath, ResourceType.file)

    def removedir(self, path: str) -> None:
        node, subpath = self._get_node(path, parent=True)
        node.remove_child(subpath, ResourceType.directory)

    def setinfo(self, path: str, info: Mapping[str, Mapping[str, object]]) -> None:
        node, _ = self._get_node(path)
        node.set_info(info)

    def getinfo(self, path: str, namespaces: Optional[Collection[str]] = None) -> Info:
        node, _ = self._get_node(path)
        return node.get_info(namespaces=namespaces)

    def getmeta(self, namespace: str = "standard") -> Mapping[str, object]:
        version = version_1p1

        if namespace == "essence":
            return {
                "version": {
                    "major": version.major,
                    "minor": version.minor,
                }
            }
        return super().getmeta(namespace)

    def setmeta(self, meta: Mapping[str, object], ns: str) -> None:
        raise NotImplementedError

    def __enter__(self) -> ChunkyFSV1:
        return self

    def __exit__(
        self, exc_type: Optional[Type[BaseException]], exc_val: Any, exc_tb: Any
    ) -> None:
        if self._update_fp:
            self.save(safe_write=True)
        self.close()
        super().__exit__(exc_type, exc_val, exc_tb)

    def close(self) -> None:
        if self._fp is not None and self._own_fp:
            self._fp.close()
        super().close()

    def save(self, out: Optional[BinaryIO] = None, safe_write: bool = False) -> None:
        if self._fp is None:
            raise RelicToolError("Failed to save, out/handle not specified!")
        if out is None:
            self._unlazy()
            out = self._fp
        with ChunkyFileWriter(out, safe_mode=safe_write) as writer:
            writer.write_fs(self)
