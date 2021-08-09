from typing import BinaryIO

from relic.chunk_formats.Dow.wtp.info_chunk import WtpInfoChunk
from relic.chunk_formats.Dow.wtp.ptld_chunk import PtldChunk
from relic.file_formats.dxt import build_dow_tga_gray_header


def create_mask_image(stream: BinaryIO, chunk: PtldChunk, info: WtpInfoChunk):
    data = chunk.image
    header = build_dow_tga_gray_header(info.width, info.height)
    stream.write(header)
    stream.write(data)
