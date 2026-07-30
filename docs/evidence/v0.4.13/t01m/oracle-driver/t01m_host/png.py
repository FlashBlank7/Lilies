"""Minimal strict decoder for opaque, non-interlaced 8-bit RGB/RGBA PNG evidence."""

import struct
import zlib
from dataclasses import dataclass

from .util import OracleError


@dataclass(frozen=True)
class PngImage:
    width: int
    height: int
    pixels: tuple[tuple[tuple[int, int, int], ...], ...]


def decode_png(raw: bytes) -> PngImage:
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise OracleError("not a PNG")
    offset = 8
    header = None
    compressed = bytearray()
    seen_end = False
    while offset < len(raw):
        if offset + 12 > len(raw):
            raise OracleError("truncated PNG chunk")
        length = struct.unpack(">I", raw[offset : offset + 4])[0]
        kind = raw[offset + 4 : offset + 8]
        data = raw[offset + 8 : offset + 8 + length]
        crc = struct.unpack(">I", raw[offset + 8 + length : offset + 12 + length])[0]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != crc:
            raise OracleError("PNG chunk CRC mismatch")
        offset += 12 + length
        if kind == b"IHDR":
            if header is not None or len(data) != 13:
                raise OracleError("invalid PNG IHDR")
            header = struct.unpack(">IIBBBBB", data)
        elif kind == b"IDAT":
            compressed.extend(data)
        elif kind == b"IEND":
            seen_end = True
            break
    if header is None or not seen_end:
        raise OracleError("PNG lacks IHDR/IEND")
    width, height, depth, color_type, compression, filtering, interlace = header
    if (
        depth != 8
        or color_type not in (2, 6)
        or compression != 0
        or filtering != 0
        or interlace != 0
        or width <= 0
        or height <= 0
    ):
        raise OracleError("PNG is not frozen opaque 8-bit RGB/RGBA format")
    channels = 3 if color_type == 2 else 4
    stride = width * channels
    inflated = zlib.decompress(bytes(compressed))
    if len(inflated) != height * (stride + 1):
        raise OracleError("PNG decompressed length mismatch")
    rows = []
    previous = bytearray(stride)
    position = 0
    for _ in range(height):
        filter_type = inflated[position]
        source = inflated[position + 1 : position + 1 + stride]
        position += stride + 1
        row = bytearray(stride)
        for index, value in enumerate(source):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            elif filter_type == 4:
                estimate = left + above - upper_left
                distances = (
                    abs(estimate - left),
                    abs(estimate - above),
                    abs(estimate - upper_left),
                )
                predictor = (left, above, upper_left)[distances.index(min(distances))]
            else:
                raise OracleError("unsupported PNG row filter")
            row[index] = (value + predictor) & 0xFF
        pixels = []
        for x in range(width):
            channels_value = row[x * channels : (x + 1) * channels]
            if channels == 4 and channels_value[3] != 255:
                raise OracleError("PNG contains non-opaque pixels")
            pixels.append(tuple(channels_value[:3]))
        rows.append(tuple(pixels))
        previous = row
    return PngImage(width, height, tuple(rows))
