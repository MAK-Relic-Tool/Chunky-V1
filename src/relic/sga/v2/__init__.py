from relic.sga import _abc
from relic.sga.v2._serializers import APISerializers
from relic.sga.v2.core import Archive, Drive, Folder, File, ArchiveMetadata, version


def _create_api():
    serializer = APISerializers()
    api = _abc.API(version, Archive, Drive, Folder, File, serializer)
    return api


API = _create_api()

__all__ = [
    "Archive",
    "Drive",
    "Folder",
    "File",
    "API",
    "version",
    "ArchiveMetadata"
]
