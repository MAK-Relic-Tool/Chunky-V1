from typing import Type

from relic.chunky.core import _abc, protocols
from relic.chunky.v1.core import Chunky, DataChunk, ChunkMeta, RawChunky, FolderChunk, version
from relic.chunky.v1._serializer import api_serializer

__version__ = "1.0.0"


def _create_api():
    return _abc.API(version, Chunky, FolderChunk, DataChunk, Type[None], ChunkMeta, api_serializer)


API: protocols.API[Chunky, FolderChunk, DataChunk, Type[None], ChunkMeta] = _create_api()
