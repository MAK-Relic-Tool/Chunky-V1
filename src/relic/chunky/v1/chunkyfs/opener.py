import os.path
from typing import BinaryIO, List

from fs.opener.parse import ParseResult
from relic.chunky.core.chunkyfs import ChunkyFsOpenerPlugin
from relic.chunky.core.definitions import Version

from relic.chunky.v1.chunkyfs.definitions import ChunkyFSV1
from relic.chunky.v1.definitions import version


class ChunkyFSV1Opener(ChunkyFsOpenerPlugin):
    _PROTO_GENERIC_V1 = "chunky-v1"

    _PROTOCOLS = [
        _PROTO_GENERIC_V1,
    ]
    _VERSIONS = [version]

    @property
    def protocols(self) -> List[str]:
        return self._PROTOCOLS

    @property
    def versions(self) -> List[Version]:
        return self._VERSIONS

    def __repr__(self) -> str:
        raise NotImplementedError

    def open_fs(
        self,
        fs_url: str,
        parse_result: ParseResult,
        writeable: bool,
        create: bool,
        cwd: str = ".",
    ) -> ChunkyFSV1:

        exists = os.path.exists(parse_result.resource)

        # Optimized case; open and parse
        if not exists:
            if not create:
                raise FileNotFoundError(parse_result.resource)
            with open(parse_result.resource, "x") as _:
                pass  # Do nothing; create the blank file

        fmode = "w+b" if writeable else "rb"
        try:
            handle: BinaryIO = open(parse_result.resource, fmode)  # type: ignore
            return ChunkyFSV1(
                handle,
                parse_handle=exists,
                #editable=writeable
            )
        except:
            handle.close()
            raise


__all__ = [
    "ChunkyFSV1Opener",
]
