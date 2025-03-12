from __future__ import annotations

import itertools
from typing import Iterator

import fs.path
from fs.base import FS
from fs.info import Info
from relic.chunky.core.definitions import ChunkFourCC


# @dataclass
# class ChunkyFSSerializer(ChunkyFSHandler, Generic[TChunkyHeader, TChunkHeader]):
#     version: Version
#     chunk_header_serializer: StreamSerializer[TChunkHeader]
#
#     def read(self, stream: BinaryIO) -> LazyChunkyFS:
#         _validate_magic_word(MagicWord, stream, True)
#
#         version = Version.unpack(stream)
#         if version != self.version:
#             raise VersionMismatchError(version, self.version)
#
#         fs = LazyChunkyFS(stream)
#
#         fs._root = self.unpack_root(stream)
#         return fs
#
#     def unpack_root(self, fp: BinaryIO) -> _LazyEntry:
#         now = fp.tell()
#         end = fp.seek(0, os.SEEK_END)
#         fp.seek(now, os.SEEK_SET)
#         entry = _LazyEntry(ResourceType.directory, "", None, None, fp, now, end - now)
#         self.unpack_chunk_collection(entry)
#         return entry
#
#     def unpack_chunk_collection(self, entry: _LazyEntry) -> None:
#         with entry.openbin() as window:
#             while True:
#                 check_now = window.tell()
#                 if check_now >= entry._size:
#                     break
#                 header: ChunkHeader = self.chunk_header_serializer.unpack(window)
#                 now = window.tell()
#                 new = entry._create_child(header.name, header.type, header.cc, header.version, now, header.size)
#                 window.seek(now + header.size, os.SEEK_SET)
#             entry._children_null_error()
#             for child in entry._children.values():
#                 if child._resource_type == ResourceType.directory:
#                     self.unpack_chunk_collection(child)
#
#     def pack_root(self, fp: BinaryIO, entry: _Entry) -> int:
#         if entry.openable:
#             with entry.openbin() as h:
#                 return fp.write(h.read())
#         else:
#             if entry._children is None:
#                 raise RelicToolError("Root chunk was not a folder!")
#
#             size = 0
#             for child in entry._children.values():  # should only be one
#                 size += self.pack_entry(fp, child)
#             return size
#
#     def pack_entry(self, fp: BinaryIO, entry: _Entry) -> int:
#         # Lazy Directories will automagically write all contents to the stream
#         if entry.openable:
#             ctype = ChunkType.Folder if entry._resource_type is ResourceType.directory else ChunkType.Data
#             header = ChunkHeader(ctype, entry._4cc, entry._version, entry.size, entry._name)
#             wrote = self.chunk_header_serializer.pack(fp, header)
#
#             with entry.openbin() as h:
#                 wrote += fp.write(h.read())
#
#             return wrote
#
#         if entry._resource_type == ResourceType.file:
#             raise NotImplementedError("File was not openable!")
#
#         write_back = fp.tell()
#         header = ChunkHeader(ChunkType.Folder, entry._4cc, entry._version, 0, entry.name)
#         wrote = self.chunk_header_serializer.pack(fp, header)
#         size = 0
#         if entry._children is None:
#             raise entry._children_null_error()
#
#         for child in entry._children.values():
#             size += self.pack_entry(fp, child)
#         header.size = size
#         now = fp.tell()
#         fp.seek(write_back, os.SEEK_SET)
#         self.chunk_header_serializer.pack(fp, header)
#         fp.seek(now, os.SEEK_CUR)
#         return wrote + size
#
#     def write(self, stream: BinaryIO, fs: LazyChunkyFS, path: str = "/") -> int:
#         written: int = MagicWord.write_magic_word(stream)
#         # TODO, some warning for chunky meta not matching serializer version?
#         #   It will definitely fail if all chunks dont get updated metadata for missing fields, so maybe irrelevant?
#         written += self.version.pack(stream)
#         # Write the header
#         if path == "/":
#             root, _ = fs._get_node(path)
#             written += self.pack_root(stream, root)
#         else:
#             node, _ = fs._get_node(path)
#             written += self.pack_entry(stream, node)
#         return written


def rglob_cc(f: FS, *cc: ChunkFourCC) -> Iterator[str]:
    codes = [c.code for c in cc]
    for step in f.walk(namespaces=["essence"]):
        path, dirs, files = step
        info: Info
        for info in itertools.chain(dirs, files):
            if info.get("essence", "4cc") in codes:
                yield fs.path.join(path, info.name)
