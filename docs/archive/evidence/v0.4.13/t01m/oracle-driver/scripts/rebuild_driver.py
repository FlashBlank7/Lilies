#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
JBR = Path(os.environ.get("T01M_JBR_HOME", "/__t01m_missing_jbr__"))
JAVA = JBR / "bin" / "java"
JAVAC = JBR / "bin" / "javac"
SDK = Path(
    os.environ.get(
        "T01M_ANDROID_SDK_ROOT",
        os.environ.get(
            "ANDROID_SDK_ROOT",
            os.environ.get("ANDROID_HOME", "/__t01m_missing_android_sdk__"),
        ),
    )
)
PLATFORM = SDK / "platforms" / "android-37.0"
ANDROID_JAR = PLATFORM / "android.jar"
UIAUTOMATOR_JAR = PLATFORM / "uiautomator.jar"
BUILD_TOOLS = SDK / "build-tools" / "37.0.0"
AAPT2 = BUILD_TOOLS / "aapt2"
ZIPALIGN = BUILD_TOOLS / "zipalign"
D8_JAR = BUILD_TOOLS / "lib" / "d8.jar"
APKSIGNER_JAR = BUILD_TOOLS / "lib" / "apksigner.jar"
SOURCE = ROOT / "src/dev/lilies/t01m/oracle/T01MOracleInstrumentation.java"
MANIFEST = ROOT / "AndroidManifest.xml"
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def run(*argv: object) -> None:
    normalized = [str(item) for item in argv]
    if not Path(normalized[0]).is_absolute():
        raise RuntimeError("rebuild commands require an absolute executable")
    subprocess.run(normalized, check=True)


def locked_signing_digests() -> tuple[str, str]:
    with (ROOT / "oracle-lock.json").open("r", encoding="utf-8") as source:
        signing = json.load(source)["signing"]
    return signing["private_key_sha256"], signing["certificate_sha256"]


def validate_toolchain() -> dict[str, str]:
    with (ROOT / "oracle-lock.json").open("r", encoding="utf-8") as source:
        locked = json.load(source)["toolchain"]
    bindings = {
        "T01M_JBR_HOME:bin/java": (JAVA, "jbr_java_sha256"),
        "T01M_JBR_HOME:bin/javac": (JAVAC, "jbr_javac_sha256"),
        "T01M_ANDROID_SDK_ROOT:platforms/android-37.0/android.jar": (
            ANDROID_JAR,
            "android_jar_sha256",
        ),
        "T01M_ANDROID_SDK_ROOT:platforms/android-37.0/uiautomator.jar": (
            UIAUTOMATOR_JAR,
            "uiautomator_jar_sha256",
        ),
        "T01M_ANDROID_SDK_ROOT:build-tools/37.0.0/aapt2": (
            AAPT2,
            "aapt2_sha256",
        ),
        "T01M_ANDROID_SDK_ROOT:build-tools/37.0.0/zipalign": (
            ZIPALIGN,
            "zipalign_sha256",
        ),
        "T01M_ANDROID_SDK_ROOT:build-tools/37.0.0/lib/d8.jar": (
            D8_JAR,
            "d8_jar_sha256",
        ),
        "T01M_ANDROID_SDK_ROOT:build-tools/37.0.0/lib/apksigner.jar": (
            APKSIGNER_JAR,
            "apksigner_jar_sha256",
        ),
    }
    observed = {}
    for identity, (path, key) in bindings.items():
        if not path.is_file() or digest(path) != locked[key]:
            raise RuntimeError(f"frozen toolchain digest mismatch: {identity}")
        observed[identity] = locked[key]
    return observed


def deterministic_repack(base: Path, dex: Path, destination: Path) -> None:
    entries = []
    with zipfile.ZipFile(base) as source:
        for item in source.infolist():
            if item.is_dir():
                continue
            entries.append((item.filename, source.read(item), item.compress_type))
    entries.append(("classes.dex", dex.read_bytes(), zipfile.ZIP_DEFLATED))
    entries.sort(key=lambda item: item[0])
    with zipfile.ZipFile(destination, "w", allowZip64=False) as output:
        for name, raw, compression in entries:
            info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
            info.compress_type = compression
            info.create_system = 0
            info.external_attr = 0
            output.writestr(info, raw, compresslevel=9)


def build(key: Path, cert: Path, output: Path) -> dict[str, str]:
    if key.is_relative_to(ROOT) or cert.is_relative_to(ROOT):
        raise RuntimeError("signing key/certificate must remain outside the repository")
    expected_key, expected_cert = locked_signing_digests()
    if digest(key) != expected_key or digest(cert) != expected_cert:
        raise RuntimeError("external signing key/certificate digest mismatch")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="t01m-driver-build-") as directory:
        root = Path(directory)
        classes = root / "classes"
        dex = root / "dex"
        classes.mkdir()
        dex.mkdir()
        run(
            JAVAC,
            "-encoding",
            "UTF-8",
            "--release",
            "17",
            "-classpath",
            os.pathsep.join((str(ANDROID_JAR), str(UIAUTOMATOR_JAR))),
            "-d",
            classes,
            SOURCE,
        )
        class_files = sorted(
            (classes / "dev/lilies/t01m/oracle").glob("*.class")
        )
        if not class_files:
            raise RuntimeError("javac emitted no frozen driver classes")
        run(
            JAVA,
            "-cp",
            D8_JAR,
            "com.android.tools.r8.D8",
            "--debug",
            "--min-api",
            "26",
            "--lib",
            ANDROID_JAR,
            "--output",
            dex,
            *class_files,
        )
        base = root / "base.apk"
        run(
            AAPT2,
            "link",
            "-o",
            base,
            "-I",
            ANDROID_JAR,
            "--manifest",
            MANIFEST,
        )
        raw = root / "raw.apk"
        aligned = root / "aligned.apk"
        deterministic_repack(base, dex / "classes.dex", raw)
        run(ZIPALIGN, "-f", "4", raw, aligned)
        signed = root / "signed.apk"
        run(
            JAVA,
            "-jar",
            APKSIGNER_JAR,
            "sign",
            "--key",
            key,
            "--cert",
            cert,
            "--v1-signing-enabled",
            "false",
            "--v2-signing-enabled",
            "true",
            "--v3-signing-enabled",
            "false",
            "--v4-signing-enabled",
            "false",
            "--out",
            signed,
            aligned,
        )
        run(JAVA, "-jar", APKSIGNER_JAR, "verify", "--verbose", signed)
        descriptor, temporary_text = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
        )
        temporary = Path(temporary_text)
        try:
            with os.fdopen(descriptor, "wb") as destination:
                with signed.open("rb") as source:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        destination.write(block)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, output)
            directory = os.open(output.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary.exists():
                temporary.unlink()
        return {
            "driver_java_sha256": digest(SOURCE),
            "classes_dex_sha256": digest(dex / "classes.dex"),
            "apk_sha256": digest(output),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--verify-against", type=Path)
    args = parser.parse_args()
    key_text = os.environ.get("T01M_SIGNING_KEY_PATH")
    cert_text = os.environ.get("T01M_SIGNING_CERT_PATH")
    if not key_text or not cert_text:
        raise RuntimeError(
            "T01M_SIGNING_KEY_PATH and T01M_SIGNING_CERT_PATH are required"
        )
    key = Path(key_text).resolve()
    cert = Path(cert_text).resolve()
    toolchain = validate_toolchain()
    first = build(key, cert, args.output.resolve())
    with tempfile.TemporaryDirectory(prefix="t01m-driver-second-build-") as temporary:
        second_path = Path(temporary) / "second.apk"
        second = build(key, cert, second_path)
        if first != second:
            raise RuntimeError("two independent driver rebuild bindings differ")
    result = {
        "schema_version": 1,
        "output_identity": args.output.name,
        "environment_identities": {
            "android_sdk": "T01M_ANDROID_SDK_ROOT",
            "jbr": "T01M_JBR_HOME",
            "signing_key": "T01M_SIGNING_KEY_PATH",
            "signing_certificate": "T01M_SIGNING_CERT_PATH",
        },
        "toolchain_bindings": toolchain,
        "first_build": first,
        "second_build": second,
        "two_rebuilds_byte_identical": True,
        "signing_key_sha256": digest(key),
        "signing_certificate_sha256": digest(cert),
        "result": "pass",
    }
    if args.verify_against is not None:
        reference = args.verify_against.resolve()
        result["reference_sha256"] = digest(reference)
        if first["apk_sha256"] != result["reference_sha256"]:
            raise RuntimeError("rebuilt driver APK differs from reference")
        result["byte_identical"] = True
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
