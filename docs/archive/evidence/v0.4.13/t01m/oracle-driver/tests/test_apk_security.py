import struct
import unittest

from t01m_host.apk_security import (
    _classification,
    _parse_dex_ids,
    _payload_magic,
    _safe_zip_path,
)
from t01m_host.util import OracleError


class ApkSecurityTests(unittest.TestCase):
    def test_rejects_unsafe_non_nfc_and_unclassified_entries(self):
        for name in ("/absolute", "../escape", "a\\b", "e\u0301.xml"):
            with self.subTest(name=name), self.assertRaises(OracleError):
                _safe_zip_path(name)
        with self.assertRaises(OracleError):
            _classification("assets/unknown.bin")

    def test_magic_scanner_finds_payloads_even_under_innocent_names(self):
        self.assertEqual(_payload_magic(b"dex\n038\0payload"), ["dex"])
        self.assertEqual(_payload_magic(b"\x7fELFpayload"), ["elf"])
        self.assertEqual(_payload_magic(b"PK\x03\x04payload"), ["zip"])
        pe = bytearray(80)
        pe[:2] = b"MZ"
        pe[0x3C:0x40] = struct.pack("<I", 64)
        pe[64:68] = b"PE\0\0"
        self.assertEqual(_payload_magic(bytes(pe)), ["pe"])

    def test_dex_inventory_fails_closed_on_bad_size(self):
        raw = bytearray(112)
        raw[:8] = b"dex\n038\0"
        struct.pack_into("<I", raw, 32, 999)
        struct.pack_into("<I", raw, 36, 112)
        struct.pack_into("<I", raw, 40, 0x12345678)
        with self.assertRaisesRegex(OracleError, "file_size"):
            _parse_dex_ids(bytes(raw))


if __name__ == "__main__":
    unittest.main()
