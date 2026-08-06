import struct
import unittest
import zlib

from t01m_host.png import decode_png


def _chunk(kind, data):
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


class PngTests(unittest.TestCase):
    def test_strict_rgb_decode(self):
        header = struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0)
        scanline = b"\x00\xff\x00\x00\x00\xff\x00"
        raw = (
            b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(scanline))
            + _chunk(b"IEND", b"")
        )
        image = decode_png(raw)
        self.assertEqual(image.width, 2)
        self.assertEqual(image.pixels[0], ((255, 0, 0), (0, 255, 0)))


if __name__ == "__main__":
    unittest.main()
