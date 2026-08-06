"""Complete fail-closed A05 APK inventory and security analysis."""

import binascii
import hashlib
import json
import os
import re
import struct
import tempfile
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from .commands import run
from .constants import APPLICATION_ID, REPO_ROOT, TOOLS
from .util import OracleError, canonical_json_bytes, sha256_file, write_new_or_replace

ROOT_DEX = re.compile(r"classes(?:[2-9]|[1-9][0-9]+)?\.dex\Z")
FORBIDDEN_NESTED_SUFFIXES = {
    ".apk", ".aab", ".aar", ".jar", ".zip", ".dex", ".class", ".so", ".a",
    ".o", ".obj", ".bc", ".dll", ".dylib", ".exe", ".wasm", ".pyc", ".oat",
    ".vdex", ".art", ".prof",
}
PROHIBITED_DEX_REFERENCES = (
    "android/webkit/WebView",
    "AdvertisingId",
    "analytics",
    "telemetry",
    "crash",
    "androidx/work",
    "android/app/job/JobScheduler",
    "android/app/AlarmManager",
    "android/content/ClipboardManager",
    "android/telephony/TelephonyManager",
    "Settings$Secure;->ANDROID_ID",
    "Build;->SERIAL",
)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _safe_zip_path(name: str) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or unicodedata.normalize("NFC", name) != name
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise OracleError(f"APK has unsafe/non-NFC ZIP path: {name!r}")


def _payload_magic(raw: bytes) -> list[str]:
    output = []
    prefixes = {
        "elf": (b"\x7fELF",),
        "macho": (
            bytes.fromhex("feedface"), bytes.fromhex("feedfacf"),
            bytes.fromhex("cefaedfe"), bytes.fromhex("cffaedfe"),
            bytes.fromhex("cafebabe"), bytes.fromhex("bebafeca"),
        ),
        "ar": (b"!<arch>\n",),
        "llvm_bitcode": (bytes.fromhex("4243c0de"),),
        "oat": (b"oat\n",),
        "vdex": (b"vdex",),
        "art": (b"art\n",),
        "wasm": (b"\x00asm",),
        "java_class": (bytes.fromhex("cafebabe"),),
        "lua": (b"\x1bLua",),
        "zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    }
    if len(raw) >= 8 and re.fullmatch(b"dex\\n[0-9]{3}\\x00", raw[:8]):
        output.append("dex")
    for label, signatures in prefixes.items():
        if any(raw.startswith(signature) for signature in signatures):
            output.append(label)
    if raw.startswith(b"MZ") and len(raw) >= 64:
        offset = int.from_bytes(raw[0x3C:0x40], "little")
        if offset + 4 <= len(raw) and raw[offset:offset + 4] == b"PE\x00\x00":
            output.append("pe")
    if raw.startswith(b"#!"):
        output.append("shebang")
    return sorted(set(output))


def _classification(path: str) -> str:
    if path == "AndroidManifest.xml":
        return "manifest"
    if path == "resources.arsc":
        return "resource_table"
    if path.startswith("res/"):
        return "compiled_resource"
    if ROOT_DEX.fullmatch(path):
        return "root_dex"
    if path.startswith("META-INF/"):
        return "signing_or_android_metadata"
    if path in ("stamp-cert-sha256",):
        return "signing_or_android_metadata"
    raise OracleError(f"unclassified APK entry: {path}")


def _android_descriptors(android_jar: Path) -> set[str]:
    with zipfile.ZipFile(android_jar) as archive:
        return {
            "L" + name[:-6] + ";"
            for name in archive.namelist()
            if name.endswith(".class") and not name.endswith("module-info.class")
        }


def _uleb128(raw: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 35, 7):
        if offset >= len(raw):
            raise OracleError("truncated DEX uleb128")
        byte = raw[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
    raise OracleError("overlong DEX uleb128")


def _parse_dex_ids(raw: bytes) -> dict[str, Any]:
    if len(raw) < 112 or not re.fullmatch(b"dex\\n[0-9]{3}\\x00", raw[:8]):
        raise OracleError("invalid/truncated DEX header")
    if struct.unpack_from("<I", raw, 32)[0] != len(raw):
        raise OracleError("DEX header file_size mismatch")
    if struct.unpack_from("<I", raw, 36)[0] != 112:
        raise OracleError("DEX header_size is not 112")
    if struct.unpack_from("<I", raw, 40)[0] != 0x12345678:
        raise OracleError("DEX endian_tag is not little-endian canonical")

    def table(pair_offset: int, width: int) -> tuple[int, int]:
        size, offset = struct.unpack_from("<II", raw, pair_offset)
        if size and (offset < 112 or offset + size * width > len(raw)):
            raise OracleError("DEX ID table escapes file")
        return size, offset

    string_count, string_offset = table(56, 4)
    type_count, type_offset = table(64, 4)
    proto_count, proto_offset = table(72, 12)
    field_count, field_offset = table(80, 8)
    method_count, method_offset = table(88, 8)
    class_count, class_offset = table(96, 32)
    strings = []
    string_raw = []
    for index in range(string_count):
        data_offset = struct.unpack_from("<I", raw, string_offset + index * 4)[0]
        _, cursor = _uleb128(raw, data_offset)
        end = raw.find(b"\0", cursor)
        if end < 0:
            raise OracleError("unterminated DEX string_data_item")
        encoded = raw[cursor:end]
        string_raw.append(encoded)
        try:
            strings.append(encoded.decode("utf-8", errors="strict"))
        except UnicodeDecodeError:
            strings.append(None)
    type_string_indexes = [
        struct.unpack_from("<I", raw, type_offset + index * 4)[0]
        for index in range(type_count)
    ]
    if any(index >= string_count for index in type_string_indexes):
        raise OracleError("DEX type_id has invalid descriptor_idx")
    descriptors = []
    for index in type_string_indexes:
        value = strings[index]
        if not isinstance(value, str):
            raise OracleError("DEX type descriptor is not strict UTF-8/ASCII")
        descriptors.append(value)

    def type_list(offset: int) -> list[str]:
        if offset == 0:
            return []
        if offset + 4 > len(raw):
            raise OracleError("DEX type_list offset escapes file")
        count = struct.unpack_from("<I", raw, offset)[0]
        if offset + 4 + count * 2 > len(raw):
            raise OracleError("DEX type_list is truncated")
        indexes = [
            struct.unpack_from("<H", raw, offset + 4 + index * 2)[0]
            for index in range(count)
        ]
        if any(index >= type_count for index in indexes):
            raise OracleError("DEX type_list has invalid type_idx")
        return [descriptors[index] for index in indexes]

    protos = []
    for index in range(proto_count):
        shorty_idx, return_idx, parameters_off = struct.unpack_from(
            "<III", raw, proto_offset + index * 12
        )
        if shorty_idx >= string_count or return_idx >= type_count:
            raise OracleError("DEX proto_id index is invalid")
        protos.append(
            {
                "shorty": strings[shorty_idx],
                "return_type": descriptors[return_idx],
                "parameter_types": type_list(parameters_off),
            }
        )
    fields = []
    for index in range(field_count):
        class_idx, type_idx, name_idx = struct.unpack_from(
            "<HHI", raw, field_offset + index * 8
        )
        if class_idx >= type_count or type_idx >= type_count or name_idx >= string_count:
            raise OracleError("DEX field_id index is invalid")
        fields.append(
            {
                "class": descriptors[class_idx],
                "type": descriptors[type_idx],
                "name": strings[name_idx],
            }
        )
    methods = []
    for index in range(method_count):
        class_idx, proto_idx, name_idx = struct.unpack_from(
            "<HHI", raw, method_offset + index * 8
        )
        if class_idx >= type_count or proto_idx >= proto_count or name_idx >= string_count:
            raise OracleError("DEX method_id index is invalid")
        methods.append(
            {
                "class": descriptors[class_idx],
                "prototype_index": proto_idx,
                "name": strings[name_idx],
            }
        )
    defined = []
    for index in range(class_count):
        class_idx = struct.unpack_from("<I", raw, class_offset + index * 32)[0]
        if class_idx >= type_count:
            raise OracleError("DEX class_def has invalid class_idx")
        defined.append(descriptors[class_idx])
    return {
        "header_counts": {
            "string_ids": string_count,
            "type_ids": type_count,
            "proto_ids": proto_count,
            "field_ids": field_count,
            "method_ids": method_count,
            "class_defs": class_count,
        },
        "strings": [
            {
                "index": index,
                "utf8": value,
                "encoded_sha256": _sha(string_raw[index]),
                "encoded_hex": string_raw[index].hex(),
            }
            for index, value in enumerate(strings)
        ],
        "types": descriptors,
        "prototypes": protos,
        "fields": fields,
        "methods": methods,
        "defined_classes": defined,
        "raw_ascii_security_scan": raw.decode("ascii", errors="ignore"),
    }


def _tool(name: str) -> Path:
    path, expected = TOOLS[name]
    if not path.is_file() or sha256_file(path) != expected:
        raise OracleError(f"frozen tool mismatch before A05: {name}")
    return path


def _java_environment() -> dict[str, str]:
    from .numeric_reference import JBR_HOME, JBR_JAVA_SHA256

    java = JBR_HOME / "bin" / "java"
    if not java.is_file() or sha256_file(java) != JBR_JAVA_SHA256:
        raise OracleError("JBR Java mismatch before apkanalyzer")
    return {**os.environ, "JAVA_HOME": str(JBR_HOME)}


def analyze_apk_security(apk: Path, output_root: Path) -> dict[str, Any]:
    """Analyze only the explicitly supplied APK; never discovers project outputs."""
    apk = apk.resolve()
    if not apk.is_file():
        raise OracleError(f"A05 APK does not exist: {apk}")
    output_root.mkdir(parents=True, exist_ok=True)
    analyzer = _tool("apkanalyzer")
    aapt2 = _tool("aapt2")
    dexdump = _tool("dexdump")
    env = _java_environment()
    manifest_commands = {
        "application_id": ("manifest", "application-id"),
        "version_code": ("manifest", "version-code"),
        "version_name": ("manifest", "version-name"),
        "min_sdk": ("manifest", "min-sdk"),
        "target_sdk": ("manifest", "target-sdk"),
        "permissions": ("manifest", "permissions"),
        "debuggable": ("manifest", "debuggable"),
    }
    manifest_values = {
        key: run([analyzer, *suffix, apk], env=env, timeout=300.0).text().strip()
        for key, suffix in manifest_commands.items()
    }
    manifest_print = run(
        [analyzer, "manifest", "print", apk], env=env, timeout=300.0
    ).stdout
    manifest_xmltree = run(
        [aapt2, "dump", "xmltree", apk, "--file", "AndroidManifest.xml"],
        timeout=300.0,
    ).stdout
    manifest_text = manifest_print.decode("utf-8", errors="strict")
    if manifest_values != {
        "application_id": APPLICATION_ID,
        "version_code": "1",
        "version_name": "0.1.0",
        "min_sdk": "26",
        "target_sdk": "37",
        "permissions": "",
        "debuggable": "false",
    }:
        raise OracleError(f"A05 manifest identity/security mismatch: {manifest_values}")
    if not re.search(r'android:allowBackup="false"', manifest_text):
        raise OracleError("allowBackup is not explicitly false")
    if not re.search(r'android:usesCleartextTraffic="false"', manifest_text):
        raise OracleError("usesCleartextTraffic is not explicitly false")
    if "<uses-permission" in manifest_text:
        raise OracleError("manifest declares a permission")
    if any(tag in manifest_text for tag in ("<service", "<provider", "<receiver")):
        raise OracleError("manifest contains forbidden service/provider/receiver")
    activities = re.findall(
        r"<activity(?:-alias)?\b[\s\S]*?</activity(?:-alias)?>",
        manifest_text,
    )
    exported = [
        item for item in activities
        if re.search(r'android:exported="true"', item)
    ]
    if len(exported) != 1 or "android.intent.category.LAUNCHER" not in exported[0]:
        raise OracleError("only one exported launcher activity is allowed")

    zip_entries: list[dict[str, Any]] = []
    dex_payloads: dict[str, bytes] = {}
    seen: set[str] = set()
    with zipfile.ZipFile(apk) as first, zipfile.ZipFile(apk) as second:
        first_infos = first.infolist()
        second_infos = second.infolist()
        if [item.filename for item in first_infos] != [item.filename for item in second_infos]:
            raise OracleError("ZIP central directory changed between independent reads")
        for info, repeated in zip(first_infos, second_infos):
            path = info.filename
            _safe_zip_path(path.rstrip("/") if path.endswith("/") else path)
            if path in seen:
                raise OracleError(f"duplicate ZIP central-directory path: {path}")
            seen.add(path)
            if info.is_dir():
                zip_entries.append(
                    {
                        "path": path,
                        "zip_method": info.compress_type,
                        "compressed_size": info.compress_size,
                        "uncompressed_size": info.file_size,
                        "crc32": f"{info.CRC:08x}",
                        "sha256": _sha(b""),
                        "unix_mode": f"{(info.external_attr >> 16) & 0xFFFF:06o}",
                        "magic": [],
                        "classification": "android_packaging_metadata",
                    }
                )
                continue
            raw = first.read(info)
            raw_again = second.read(repeated)
            if raw != raw_again:
                raise OracleError(f"ZIP entry changed between reads: {path}")
            crc = binascii.crc32(raw) & 0xFFFFFFFF
            if (
                crc != info.CRC
                or len(raw) != info.file_size
                or info.compress_size != repeated.compress_size
            ):
                raise OracleError(f"ZIP size/CRC mismatch: {path}")
            magic = _payload_magic(raw)
            mode = (info.external_attr >> 16) & 0xFFFF
            classification = _classification(path)
            suffix = Path(path).suffix.casefold()
            if mode & 0o111:
                raise OracleError(f"executable Unix mode in APK: {path}")
            if suffix in FORBIDDEN_NESTED_SUFFIXES and classification != "root_dex":
                raise OracleError(f"nested precompiled/archive payload: {path}")
            prohibited_magic = set(magic) - ({"dex"} if classification == "root_dex" else set())
            if prohibited_magic:
                raise OracleError(f"prohibited payload magic {sorted(prohibited_magic)}: {path}")
            if classification == "root_dex":
                if magic != ["dex"]:
                    raise OracleError(f"root DEX has invalid magic: {path}")
                dex_payloads[path] = raw
            elif "dex" in magic:
                raise OracleError(f"non-root entry has DEX magic: {path}")
            zip_entries.append(
                {
                    "path": path,
                    "zip_method": info.compress_type,
                    "compressed_size": info.compress_size,
                    "uncompressed_size": info.file_size,
                    "crc32": f"{crc:08x}",
                    "sha256": _sha(raw),
                    "unix_mode": f"{mode:06o}",
                    "magic": magic,
                    "classification": classification,
                }
            )
    if not dex_payloads or "classes.dex" not in dex_payloads:
        raise OracleError("APK has no root classes.dex")

    android_jar = REPO_ROOT / "toolchain/android.jar"
    if not android_jar.is_file():
        # The lock validator resolves the environment path; no copy is required.
        from .constants import ANDROID_JAR
        android_jar = ANDROID_JAR
    allowed_external = _android_descriptors(android_jar)
    dex_reports = []
    dex_texts = []
    all_defined: set[str] = set()
    all_references: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="t01m-a05-dex-") as temp:
        temp_root = Path(temp)
        for name, raw in sorted(dex_payloads.items()):
            path = temp_root / name
            path.write_bytes(raw)
            dump = run([dexdump, "-d", "-f", path], timeout=300.0).stdout
            text = dump.decode("utf-8", errors="strict")
            dex_texts.append(text)
            binary_inventory = _parse_dex_ids(raw)
            if "Class descriptor" not in text or "DEX file header" not in text:
                raise OracleError(f"dexdump did not completely parse {name}")
            dexdump_defined = set(
                re.findall(r"Class descriptor\s*:\s*'([^']+)'", text)
            )
            defined = set(binary_inventory["defined_classes"])
            references = set(binary_inventory["types"])
            if defined != dexdump_defined:
                raise OracleError(f"binary DEX class_defs disagree with dexdump: {name}")
            if not defined:
                raise OracleError(f"dexdump found no defined class in {name}")
            if any(not item.startswith("Ldev/lilies/civilizationseed/") for item in defined):
                raise OracleError(f"non-application class is defined in {name}")
            all_defined.update(defined)
            all_references.update(references)
            security_ascii = binary_inventory.pop("raw_ascii_security_scan")
            dex_texts.append(security_ascii)
            dex_reports.append(
                {
                    "path": name,
                    "bytes": len(raw),
                    "sha256": _sha(raw),
                    "dexdump_sha256": _sha(dump),
                    "defined_classes": sorted(defined),
                    "descriptor_references": sorted(references),
                    "complete_id_inventory": binary_inventory,
                    "unparsed_id_sections": [],
                }
            )
    unresolved = []
    for descriptor in sorted(all_references - all_defined):
        normalized = descriptor.lstrip("[")
        if normalized.startswith("L") and normalized not in allowed_external:
            unresolved.append(normalized)
    if unresolved:
        raise OracleError(f"DEX has unresolved/third-party descriptors: {unresolved[:20]}")
    joined_references = "\n".join(sorted(all_references)) + "\n" + "\n".join(
        dex_texts
    )
    prohibited = [
        token for token in PROHIBITED_DEX_REFERENCES
        if token.casefold() in joined_references.casefold()
    ]
    if prohibited:
        raise OracleError(f"DEX has prohibited references: {prohibited}")

    resources_raw = run([aapt2, "dump", "resources", apk], timeout=300.0).stdout
    resources_text = resources_raw.decode("utf-8", errors="strict")
    if "Package" not in resources_text:
        raise OracleError("aapt2 did not parse resources.arsc completely")
    resource_files = sorted(
        item["path"] for item in zip_entries if item["path"].startswith("res/")
    )
    xml_reports = []
    for path in resource_files:
        if not path.endswith(".xml"):
            continue
        parsed = run(
            [aapt2, "dump", "xmltree", apk, "--file", path], timeout=300.0
        ).stdout
        if not parsed:
            raise OracleError(f"aapt2 produced no XML tree: {path}")
        xml_reports.append(
            {
                "path": path,
                "xmltree_sha256": _sha(parsed),
                "xmltree_utf8": parsed.decode("utf-8", errors="strict"),
            }
        )
    table_references = sorted(set(re.findall(r"res/[A-Za-z0-9_./-]+", resources_text)))
    missing_references = sorted(set(table_references) - set(resource_files))
    if missing_references:
        raise OracleError(f"resource table references missing ZIP files: {missing_references}")
    if any(path not in resources_text for path in resource_files):
        raise OracleError("a res/ ZIP entry is absent from complete resource table")

    package_analysis = {
        "schema_version": 1,
        "case_id": "A05",
        "apk": {"path_identity": apk.name, "bytes": apk.stat().st_size, "sha256": sha256_file(apk)},
        "manifest": manifest_values,
        "manifest_print_sha256": _sha(manifest_print),
        "manifest_print_utf8": manifest_text,
        "manifest_xmltree_sha256": _sha(manifest_xmltree),
        "manifest_xmltree_utf8": manifest_xmltree.decode("utf-8", errors="strict"),
        "allow_backup": False,
        "uses_cleartext_traffic": False,
        "exported_launcher_count": 1,
        "service_provider_receiver_count": 0,
        "permission_set": [],
        "result": "pass",
    }
    resource_inventory = {
        "schema_version": 1,
        "resources_arsc_sha256": _sha(resources_raw),
        "complete_resources_dump_utf8": resources_text,
        "resource_table_file_references": table_references,
        "compiled_resource_files": resource_files,
        "compiled_xml": xml_reports,
        "result": "pass",
    }
    dex_scan = {
        "schema_version": 1,
        "root_dex_entries": dex_reports,
        "defined_classes": sorted(all_defined),
        "external_descriptors": sorted(all_references - all_defined),
        "unresolved_external_descriptors": [],
        "prohibited_references": [],
        "result": "pass",
    }
    inventory = {
        "schema_version": 1,
        "central_directory_entry_count": len(zip_entries),
        "entries": zip_entries,
        "second_independent_read": "pass",
        "unclassified_entries": [],
        "prohibited_payloads": [],
        "result": "pass",
    }
    documents = {
        "package-analysis.json": package_analysis,
        "apk-entry-inventory.json": inventory,
        "resource-inventory.json": resource_inventory,
        "dex-reference-scan.json": dex_scan,
    }
    for name, value in documents.items():
        write_new_or_replace(output_root / name, canonical_json_bytes(value))
    write_new_or_replace(
        output_root / "permission-dump.txt",
        (
            "# apkanalyzer manifest permissions\n"
            + manifest_values["permissions"]
            + "\n"
        ).encode("utf-8"),
    )
    return {
        "schema_version": 1,
        "case_id": "A05",
        "outputs": {
            name: {
                "path": name,
                "sha256": sha256_file(output_root / name),
            }
            for name in documents
        },
        "result": "pass",
    }
