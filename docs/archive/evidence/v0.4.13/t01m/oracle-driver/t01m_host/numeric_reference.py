"""Bound JBR 21 adapter for the acceptance-oracle NumericReference scope."""

import hashlib
import os
import struct
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

from .constants import JBR_HOME, JBR_HOME_ENV
from .png import PngImage
from .util import OracleError, sha256_file

Box = tuple[int, int, int, int]

JBR_JAVA = JBR_HOME / "bin" / "java"
JBR_RELEASE = JBR_HOME / "release"
JBR_JAVA_SHA256 = "9896ec30ee47df849281391af74338770c715f0bed0ca03d3e570209e4fc9b2b"
JBR_RELEASE_SHA256 = "677ea38b692944be1377af7a21fef8089337a1b4de3f8c6d5f505f88ca6f6815"
REFERENCE_ROOT = Path(__file__).resolve().parent.parent / "numeric-reference"
REFERENCE_FILES = {
    "NumericReference.java": "664a5c36cc0607ab6156679fdf17729f0f85fcbfa8781df2c7b069b72ce0d8d7",
    "NumericReference.class": "839a089cfc36f6cc1b5894712c787196fce0eb794e9d8e662df00a5680fb2e8c",
    "NumericReference$Box.class": "67965a5fa41a1a45e203053b396fb60e897e626ce9559636a3ad9507a4a25b6c",
    "NumericReference$CharacterResult.class": "ad1d7608e12e53f363d772c9f227abd72c7639319a4f52e700167bd73f717918",
    "NumericReference$PixelScore.class": "0e73071e7d64a64e3ec1d17d4fb698148cb14a4998de55fe734e8254ba415873",
}
INPUT_MAGIC = 0x5430314D
OUTPUT_MAGIC = 0x4E524546
VERSION = 1


def validate_numeric_reference() -> dict[str, Any]:
    expected = {
        JBR_JAVA: JBR_JAVA_SHA256,
        JBR_RELEASE: JBR_RELEASE_SHA256,
        **{
            REFERENCE_ROOT / name: digest
            for name, digest in REFERENCE_FILES.items()
        },
    }
    observed = {}
    for path, digest in expected.items():
        if not path.is_file() or sha256_file(path) != digest:
            raise OracleError(f"NumericReference binding mismatch: {path}")
        if path.is_relative_to(JBR_HOME):
            identity = f"{JBR_HOME_ENV}:{path.relative_to(JBR_HOME).as_posix()}"
        elif path.is_relative_to(REFERENCE_ROOT):
            identity = f"oracle:{path.relative_to(REFERENCE_ROOT.parent).as_posix()}"
        else:
            raise OracleError(f"NumericReference path has no frozen identity: {path}")
        observed[identity] = digest
    return {
        "runtime": "JBR 21 Java strict floating point and java.lang.StrictMath",
        "files": observed,
    }


def _invoke(mode: str, payload: bytes = b"") -> bytes:
    validate_numeric_reference()
    result = subprocess.run(
        [str(JBR_JAVA), "-cp", str(REFERENCE_ROOT), "NumericReference", mode],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300.0,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-2000:]
        raise OracleError(f"NumericReference {mode} failed: {detail}")
    return result.stdout


class _Reader:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.offset = 0

    def take(self, fmt: str):
        size = struct.calcsize(fmt)
        if self.offset + size > len(self.raw):
            raise OracleError("truncated NumericReference output")
        values = struct.unpack_from(fmt, self.raw, self.offset)
        self.offset += size
        return values[0] if len(values) == 1 else values

    def finish(self) -> None:
        if self.offset != len(self.raw):
            raise OracleError("trailing NumericReference output")


def _value(bits: int) -> tuple[float, str]:
    if bits & 0x7FF0000000000000 == 0x7FF0000000000000:
        raise OracleError("non-finite NumericReference output")
    value = struct.unpack(">d", struct.pack(">Q", bits))[0]
    if value == 0.0:
        bits = 0
        value = 0.0
    return value, f"{bits:016x}"


def _header(reader: _Reader) -> tuple[tuple[float, ...], tuple[str, ...]]:
    if reader.take(">I") != OUTPUT_MAGIC or reader.take(">I") != VERSION:
        raise OracleError("NumericReference output identity mismatch")
    if reader.take(">I") != 256:
        raise OracleError("NumericReference sRGB lookup is incomplete")
    values = []
    raw_bits = []
    for _ in range(256):
        value, encoded = _value(reader.take(">Q"))
        values.append(value)
        raw_bits.append(encoded)
    return tuple(values), tuple(raw_bits)


@lru_cache(maxsize=1)
def srgb_lookup() -> tuple[tuple[float, ...], tuple[str, ...]]:
    reader = _Reader(_invoke("lookup"))
    values, raw_bits = _header(reader)
    reader.finish()
    return values, raw_bits


def linear_rgb(pixel: Sequence[int]) -> tuple[float, float, float]:
    if len(pixel) < 3 or any(
        not isinstance(channel, int) or not 0 <= channel <= 255
        for channel in pixel[:3]
    ):
        raise OracleError("RGB pixel requires three 8-bit integer channels")
    lookup, _ = srgb_lookup()
    return lookup[pixel[0]], lookup[pixel[1]], lookup[pixel[2]]


def _image_bytes(image: PngImage) -> bytes:
    raw = bytearray(image.width * image.height * 3)
    offset = 0
    for row in image.pixels:
        for pixel in row:
            raw[offset : offset + 3] = bytes(pixel)
            offset += 3
    return bytes(raw)


def measure_text_contrast(
    image: PngImage,
    boxes: Sequence[Sequence[float]],
) -> dict[str, Any]:
    payload = bytearray()
    payload.extend(struct.pack(">IIiii", INPUT_MAGIC, VERSION, image.width, image.height,
                               image.width * image.height * 3))
    payload.extend(_image_bytes(image))
    payload.extend(struct.pack(">i", len(boxes)))
    for box in boxes:
        if len(box) != 4:
            raise OracleError("character box requires four coordinates")
        payload.extend(struct.pack(">dddd", *(float(value) for value in box)))
    reader = _Reader(_invoke("contrast", bytes(payload)))
    _, lookup_bits = _header(reader)
    count = reader.take(">i")
    if count != len(boxes):
        raise OracleError("NumericReference character count mismatch")
    characters = []
    for index in range(count):
        pixel_box = [
            reader.take(">i"),
            reader.take(">i"),
            reader.take(">i"),
            reader.take(">i"),
        ]
        ring_count = reader.take(">i")
        glyph_count = reader.take(">i")
        core_count = reader.take(">i")
        coefficients = []
        coefficient_bits = []
        for _channel in range(3):
            channel = []
            channel_bits = []
            for _coefficient in range(4):
                value, bits = _value(reader.take(">Q"))
                channel.append(value)
                channel_bits.append(bits)
            coefficients.append(channel)
            coefficient_bits.append(channel_bits)
        minimum_ratio, minimum_ratio_bits = _value(reader.take(">Q"))
        passed = bool(reader.take(">?"))
        selected_count = reader.take(">i")
        if selected_count != core_count:
            raise OracleError("NumericReference core count mismatch")
        selected = []
        for _selected in range(selected_count):
            x = reader.take(">i")
            y = reader.take(">i")
            actual = []
            actual_bits = []
            background = []
            background_bits = []
            for _channel in range(3):
                value, bits = _value(reader.take(">Q"))
                actual.append(value)
                actual_bits.append(bits)
            for _channel in range(3):
                value, bits = _value(reader.take(">Q"))
                background.append(value)
                background_bits.append(bits)
            distance, distance_bits = _value(reader.take(">Q"))
            ratio, ratio_bits = _value(reader.take(">Q"))
            selected.append(
                {
                    "x": x,
                    "y": y,
                    "actual_linear_rgb": actual,
                    "actual_linear_rgb_raw_bits": actual_bits,
                    "background_linear_rgb": background,
                    "background_linear_rgb_raw_bits": background_bits,
                    "oklab_distance": distance,
                    "oklab_distance_raw_bits": distance_bits,
                    "contrast_ratio": ratio,
                    "contrast_ratio_raw_bits": ratio_bits,
                }
            )
        characters.append(
            {
                "pixel_box": pixel_box,
                "input_box_raw_bits": [
                    f"{struct.unpack('>Q', struct.pack('>d', float(value)))[0]:016x}"
                    for value in boxes[index]
                ],
                "ring_pixels": ring_count,
                "glyph_pixels": glyph_count,
                "core_count": core_count,
                "background_coefficients": coefficients,
                "background_coefficients_raw_bits": coefficient_bits,
                "selected_actual_pixels": selected,
                "minimum_contrast_ratio": minimum_ratio,
                "minimum_contrast_ratio_raw_bits": minimum_ratio_bits,
                "pass": passed,
            }
        )
    reader.finish()
    return {
        "numeric_reference": validate_numeric_reference(),
        "input_rgb_sha256": hashlib.sha256(_image_bytes(image)).hexdigest(),
        "srgb_q_to_linear_raw_bits": list(lookup_bits),
        "characters": characters,
    }
