import json
import hashlib
import re
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

from .commands import run
from .constants import (
    ABI,
    API_LEVEL,
    APPLICATION_ID,
    AVD_NAME,
    ACCEPTANCE_ORACLE_PATH_ENV,
    ANDROID_SDK_ROOT,
    ANDROID_SDK_ROOT_ENV,
    BRIEF_PATH_ENV,
    DRIVER_APK,
    DRIVER_SHA256,
    TALKBACK_APK_SHA256,
    TALKBACK_SERVICE,
    TOOLS,
    VERSION_CODE,
    VERSION_NAME,
    REPO_ROOT,
)
from .util import OracleError, canonical_json_bytes, sha256_file


class AndroidDevice:
    def __init__(self, serial: str):
        if not serial or any(char.isspace() for char in serial):
            raise OracleError("a single explicit adb serial is required")
        self.serial = serial
        self.adb = TOOLS["adb"][0]

    def adb_cmd(self, *args: object, timeout: float = 120.0, check: bool = True):
        return run(
            [self.adb, "-s", self.serial, *args],
            timeout=timeout,
            check=check,
        )

    def shell(self, *args: object, timeout: float = 120.0, check: bool = True):
        return self.adb_cmd("shell", *args, timeout=timeout, check=check)

    def getprop(self, name: str) -> str:
        return self.shell("getprop", name).text().strip()

    def validate_identity(self) -> dict[str, Any]:
        devices = self.adb_cmd("devices", "-l").text().splitlines()
        matching = [line for line in devices if line.startswith(self.serial + "\tdevice")]
        if len(matching) != 1:
            raise OracleError("explicit serial is not one healthy adb device")
        observed = {
            "serial": self.serial,
            "avd_name": self.getprop("ro.boot.qemu.avd_name"),
            "api_level": int(self.getprop("ro.build.version.sdk")),
            "abi": self.getprop("ro.product.cpu.abi"),
        }
        expected = {
            "avd_name": AVD_NAME,
            "api_level": API_LEVEL,
            "abi": ABI,
        }
        for key, value in expected.items():
            if observed[key] != value:
                raise OracleError(
                    f"device {key} mismatch: expected {value!r}, observed {observed[key]!r}"
                )
        return observed

    def package_path(self, package: str) -> str:
        result = self.shell("pm", "path", package, check=False).text().strip()
        if not result.startswith("package:"):
            raise OracleError(f"package is not installed: {package}")
        return result.removeprefix("package:")

    def validate_driver_install(self) -> dict[str, Any]:
        remote_path = self.package_path("dev.lilies.t01m.oracle")
        remote = self.adb_cmd("exec-out", "cat", remote_path).stdout
        import hashlib

        digest = hashlib.sha256(remote).hexdigest()
        if digest != DRIVER_SHA256:
            raise OracleError("installed driver digest mismatch")
        return {"path": remote_path, "sha256": digest, "bytes": len(remote)}

    def install_driver(self) -> None:
        self.adb_cmd("install", "-r", "-t", DRIVER_APK)
        self.validate_driver_install()

    def install_target(self, apk: Path) -> None:
        self.adb_cmd("install", "-r", "-t", apk)
        self.validate_installed_target()

    def validate_installed_target(self) -> dict[str, str]:
        dump = self.shell("dumpsys", "package", APPLICATION_ID).text()
        patterns = {
            "version_code": r"\bversionCode=(\d+)",
            "version_name": r"\bversionName=([^\s]+)",
        }
        observed = {}
        for key, pattern in patterns.items():
            match = re.search(pattern, dump)
            if not match:
                raise OracleError(f"missing {key} in package dump")
            observed[key] = match.group(1)
        if observed != {"version_code": VERSION_CODE, "version_name": VERSION_NAME}:
            raise OracleError(f"installed target metadata mismatch: {observed}")
        return observed

    def clear_data(self) -> None:
        result = self.shell("pm", "clear", APPLICATION_ID).text().strip()
        if result != "Success":
            raise OracleError(f"pm clear did not report Success: {result!r}")

    def force_stop(self) -> None:
        self.shell("am", "force-stop", APPLICATION_ID)

    def launch(self) -> None:
        result = self.shell(
            "monkey",
            "-p",
            APPLICATION_ID,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        ).text()
        if "Events injected: 1" not in result:
            raise OracleError("launcher event was not injected")

    def set_offline(self) -> None:
        self.shell("settings", "put", "global", "airplane_mode_on", "1")
        self.shell(
            "am",
            "broadcast",
            "-a",
            "android.intent.action.AIRPLANE_MODE",
            "--ez",
            "state",
            "true",
        )
        self.shell("svc", "wifi", "disable")
        self.shell("svc", "data", "disable")
        observed = self.shell("settings", "get", "global", "airplane_mode_on").text().strip()
        if observed != "1":
            raise OracleError("airplane mode did not remain enabled")
        wifi = self.shell("cmd", "wifi", "status", check=False).text().strip()
        if "disabled" not in wifi.lower():
            raise OracleError(f"Wi-Fi did not remain disabled: {wifi!r}")
        mobile = self.shell(
            "settings", "get", "global", "mobile_data", check=False
        ).text().strip()
        if mobile not in {"0", "null", ""}:
            raise OracleError(f"mobile data did not remain disabled: {mobile!r}")

    def rotate(self, orientation: str) -> None:
        values = {"portrait": "0", "landscape": "1"}
        if orientation not in values:
            raise OracleError("orientation must be portrait or landscape")
        self.shell("settings", "put", "system", "accelerometer_rotation", "0")
        self.shell("settings", "put", "system", "user_rotation", values[orientation])

    def set_font_scale(self, scale: float) -> None:
        if scale not in (1.0, 2.0):
            raise OracleError("only frozen font scales 1.0 and 2.0 are allowed")
        self.shell("settings", "put", "system", "font_scale", f"{scale:.1f}")

    def display_density(self) -> float:
        output = self.shell("wm", "density").text()
        matches = re.findall(r"(?:Physical|Override) density:\s*(\d+)", output)
        if not matches:
            raise OracleError(f"wm density did not expose a density: {output!r}")
        dpi = int(matches[-1])
        if dpi <= 0:
            raise OracleError("display density must be positive")
        return dpi / 160.0

    def set_animation_scales(self, scale: float) -> None:
        if scale not in (0.0, 1.0):
            raise OracleError("animation scale must be 0 or 1")
        rendered = f"{scale:.1f}"
        for setting in (
            "animator_duration_scale",
            "transition_animation_scale",
            "window_animation_scale",
        ):
            self.shell("settings", "put", "global", setting, rendered)

    def set_talkback(self, enabled: bool) -> None:
        if enabled:
            self.shell(
                "settings",
                "put",
                "secure",
                "enabled_accessibility_services",
                TALKBACK_SERVICE,
            )
            self.shell("settings", "put", "secure", "accessibility_enabled", "1")
        else:
            self.shell(
                "settings", "put", "secure", "enabled_accessibility_services", ""
            )
            self.shell("settings", "put", "secure", "accessibility_enabled", "0")

    def validate_talkback(self) -> dict[str, Any]:
        path = self.shell(
            "pm", "path", "com.google.android.marvin.talkback"
        ).text().strip().removeprefix("package:")
        raw = self.adb_cmd("exec-out", "cat", path).stdout
        import hashlib

        digest = hashlib.sha256(raw).hexdigest()
        if digest != TALKBACK_APK_SHA256:
            raise OracleError("TalkBack system APK digest mismatch")
        state = self.shell("dumpsys", "accessibility").text()
        if "TalkBack" not in state or "touchExplorationEnabled=true" not in state:
            raise OracleError("frozen TalkBack is not bound with touch exploration")
        return {"system_apk_path": path, "sha256": digest}

    def snapshot_shared_storage(self) -> list[dict[str, Any]]:
        root = "/storage/emulated/0"
        nul = self.shell(
            "find", root, "-type", "f", "-print0", timeout=300.0
        ).stdout
        paths = nul.split(b"\0")
        if paths and paths[-1] == b"":
            paths.pop()
        decoded = []
        for raw in paths:
            path = raw.decode("utf-8", errors="strict")
            if not path.startswith(root + "/"):
                raise OracleError(f"shared-storage path escaped root: {path!r}")
            relative = path[len(root) + 1 :]
            if (
                not relative
                or "\\" in relative
                or any(part in ("", ".", "..") for part in relative.split("/"))
            ):
                raise OracleError(f"unsafe shared-storage path: {relative!r}")
            decoded.append((raw, relative))
        output = []
        import hashlib

        for raw, relative in sorted(decoded, key=lambda item: item[1]):
            remote = raw.decode("utf-8")
            size_text = self.shell("stat", "-c", "%s", remote).text().strip()
            content = self.adb_cmd("exec-out", "cat", remote, timeout=300.0).stdout
            size = int(size_text)
            if len(content) != size:
                raise OracleError(f"shared-storage file changed while reading: {relative}")
            output.append(
                {
                    "path": relative,
                    "size": size,
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        return output

    def snapshot_private_storage(self) -> dict[str, Any]:
        """Inventory target-owned app data through run-as without exposing host paths."""
        data_dir = self.shell(
            "run-as", APPLICATION_ID, "pwd", timeout=120.0
        ).text().strip()
        expected_suffix = "/" + APPLICATION_ID
        if not data_dir.startswith("/data/") or not data_dir.endswith(expected_suffix):
            raise OracleError(f"target data directory is not app-private: {data_dir!r}")
        raw_paths = self.shell(
            "run-as",
            APPLICATION_ID,
            "find",
            ".",
            "-type",
            "f",
            "-print0",
            timeout=300.0,
        ).stdout
        paths = raw_paths.split(b"\0")
        if paths and paths[-1] == b"":
            paths.pop()
        files = []
        for raw in sorted(paths):
            relative = raw.decode("utf-8", errors="strict").removeprefix("./")
            if (
                not relative
                or relative.startswith("/")
                or "\\" in relative
                or any(part in ("", ".", "..") for part in relative.split("/"))
            ):
                raise OracleError(f"unsafe app-private path: {relative!r}")
            content = self.adb_cmd(
                "exec-out",
                "run-as",
                APPLICATION_ID,
                "cat",
                "./" + relative,
                timeout=300.0,
            ).stdout
            files.append(
                {
                    "path": relative,
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        return {
            "path_class": "android_application_private_data",
            "application_id": APPLICATION_ID,
            "data_dir_suffix": expected_suffix,
            "files": files,
        }


def validate_toolchain() -> dict[str, Any]:
    observed = {}
    for name, (path, digest) in TOOLS.items():
        if not path.is_file():
            raise OracleError(f"frozen tool missing: {path}")
        actual = sha256_file(path)
        if actual != digest:
            raise OracleError(f"frozen tool digest mismatch: {name}")
        if not path.is_relative_to(ANDROID_SDK_ROOT):
            raise OracleError(f"frozen tool path escaped {ANDROID_SDK_ROOT_ENV}: {name}")
        observed[name] = {
            "identity": (
                f"{ANDROID_SDK_ROOT_ENV}:"
                f"{path.relative_to(ANDROID_SDK_ROOT).as_posix()}"
            ),
            "sha256": actual,
        }
    if sha256_file(DRIVER_APK) != DRIVER_SHA256:
        raise OracleError("host driver APK digest mismatch")
    from .numeric_reference import validate_numeric_reference

    observed["numeric_reference"] = validate_numeric_reference()
    return observed


def validate_oracle_lock() -> dict[str, Any]:
    lock_path = REPO_ROOT / "oracle-lock.json"
    with lock_path.open("r", encoding="utf-8") as source:
        lock = json.load(source)
    bindings = lock.get("frozen_contract_bindings", {})
    expected_bindings = {
        "project_brief_raw_sha256": (
            "990d76d6b893a73d51ea98dc67e494bed1aa88bf04a75f534bacfe995ae60dee"
        ),
        "acceptance_oracle_raw_sha256": (
            "52c7998a6b9f3d710f043e53f49d2176ed50bb8bf4bfc782d7ec1b5321a8bbe4"
        ),
        "case_ids_jq_compact_bytes_sha256": (
            "0a4177e533d9ee6eab47f70a7fce638c41becdeea73be18175f8b10fede4ce90"
        ),
    }
    for key, expected in expected_bindings.items():
        if bindings.get(key) != expected:
            raise OracleError(f"oracle lock contract binding mismatch: {key}")
    import os

    external_inputs = (
        (
            BRIEF_PATH_ENV,
            "project_brief_raw_sha256",
            "project_brief_path_identity",
        ),
        (
            ACCEPTANCE_ORACLE_PATH_ENV,
            "acceptance_oracle_raw_sha256",
            "acceptance_oracle_path_identity",
        ),
    )
    for environment_name, digest_key, identity_key in external_inputs:
        raw_path = os.environ.get(environment_name)
        if not raw_path:
            raise OracleError(f"required frozen input environment is unset: {environment_name}")
        path = Path(raw_path)
        if not path.is_file() or sha256_file(path) != bindings[digest_key]:
            raise OracleError(f"frozen input binding mismatch: {environment_name}")
        if bindings.get(identity_key) != environment_name:
            raise OracleError(f"oracle lock path identity mismatch: {identity_key}")
    expected_case_ids = [
        "A01-baseline",
        "A02-authorship-and-ledger",
        "A03-build-and-tests",
        "A04-reproducible-apk",
        "A05-package-security",
        "A06-black-box-flow",
        "A07-offline-and-persistence",
        "A08-accessibility",
        "A09-motion-and-visual",
        "A10-runtime-and-evidence",
    ]
    if bindings.get("case_ids") != expected_case_ids:
        raise OracleError("oracle lock A01-A10 case IDs mismatch")
    host = lock.get("host_controller", {})
    locked_entries = host.get("recursive_path_sha256")
    if not isinstance(locked_entries, list):
        raise OracleError("oracle lock lacks host recursive path/hash manifest")
    roots = host.get("manifest_roots")
    if roots != [
        "README.md",
        "config",
        "numeric-reference",
        "scripts",
        "t01m-host-oracle",
        "t01m_host",
        "tests",
    ]:
        raise OracleError("oracle lock host manifest roots mismatch")
    actual_entries = []
    for root_text in roots:
        root = REPO_ROOT / root_text
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in candidates:
            if path.is_symlink():
                raise OracleError(f"host controller manifest contains symlink: {path}")
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            actual_entries.append(
                {
                    "path": path.relative_to(REPO_ROOT).as_posix(),
                    "sha256": sha256_file(path),
                }
            )
    actual_entries.sort(key=lambda item: item["path"])
    if actual_entries != locked_entries:
        raise OracleError("host controller recursive path/hash manifest mismatch")
    manifest_digest = hashlib.sha256(canonical_json_bytes(actual_entries)).hexdigest()
    if host.get("canonical_manifest_sha256") != manifest_digest:
        raise OracleError("host controller canonical manifest digest mismatch")
    from .config import load_accessibility_contract, load_flow
    from .event_binding import load_reduced_motion_contract

    if (
        host.get("flow_step_count") != len(load_flow()["steps"])
        or host.get("accessibility_screen_count")
        != len(load_accessibility_contract()["screens"])
        or host.get("accessibility_font_scales")
        != load_accessibility_contract()["font_scales"]
        or host.get("reduced_motion_transition_count")
        != len(load_reduced_motion_contract()["transition_targets"])
    ):
        raise OracleError("oracle lock executable matrix metadata mismatch")
    source = lock.get("source", {})
    source_entries = source.get("recursive_path_sha256")
    if not isinstance(source_entries, list):
        raise OracleError("oracle lock lacks driver source path/hash manifest")
    actual_source_entries = []
    for path in sorted(
        (
            REPO_ROOT / "AndroidManifest.xml",
            *(
                path
                for path in (REPO_ROOT / "src").rglob("*")
                if path.is_file()
            ),
        ),
        key=lambda value: value.as_posix(),
    ):
        if path.is_symlink() or not path.is_file():
            raise OracleError(f"invalid driver source path: {path}")
        actual_source_entries.append(
            {
                "path": path.relative_to(REPO_ROOT).as_posix(),
                "sha256": sha256_file(path),
            }
        )
    if actual_source_entries != source_entries:
        raise OracleError("driver source recursive path/hash manifest mismatch")
    source_manifest_digest = hashlib.sha256(
        canonical_json_bytes(actual_source_entries)
    ).hexdigest()
    if source.get("canonical_manifest_sha256") != source_manifest_digest:
        raise OracleError("driver source canonical manifest digest mismatch")
    artifact = lock.get("artifact", {})
    if (
        artifact.get("relative_path") != "dist/t01m-external-oracle.apk"
        or artifact.get("permission_declarations") != []
        or artifact.get("sha256") != DRIVER_SHA256
        or artifact.get("sha256") != sha256_file(DRIVER_APK)
    ):
        raise OracleError("oracle lock driver artifact digest mismatch")
    signing = lock.get("signing", {})
    if (
        signing.get("v1_enabled") is not False
        or signing.get("v2_enabled") is not True
        or signing.get("v3_enabled") is not False
        or signing.get("v4_enabled") is not False
    ):
        raise OracleError("oracle lock driver signing mode is not v2-only")
    dex_binding = artifact.get("source_to_dex_binding", {})
    with zipfile.ZipFile(DRIVER_APK) as archive:
        names = archive.namelist()
        if names.count("classes.dex") != 1:
            raise OracleError("driver APK must contain exactly one classes.dex")
        dex_digest = hashlib.sha256(archive.read("classes.dex")).hexdigest()
    if dex_binding.get("classes_dex_sha256") != dex_digest:
        raise OracleError("oracle lock driver classes.dex binding mismatch")
    if dex_binding.get("source_manifest_sha256") != source_manifest_digest:
        raise OracleError("oracle lock source-to-DEX source binding mismatch")
    if dex_binding.get("apk_sha256") != artifact.get("sha256"):
        raise OracleError("oracle lock source-to-DEX APK binding mismatch")
    return {
        "oracle_lock_sha256": sha256_file(lock_path),
        "host_controller_manifest_sha256": manifest_digest,
        "driver_source_manifest_sha256": source_manifest_digest,
        "driver_classes_dex_sha256": dex_digest,
        "case_set_sha256": expected_bindings["case_ids_jq_compact_bytes_sha256"],
        "result": "pass",
    }


def validate_target_apk(apk: Path) -> dict[str, Any]:
    if not apk.is_file():
        raise OracleError(f"target APK does not exist: {apk}")
    analyzer = TOOLS["apkanalyzer"][0]
    values = {}
    commands = {
        "application_id": ("manifest", "application-id"),
        "version_code": ("manifest", "version-code"),
        "version_name": ("manifest", "version-name"),
        "min_sdk": ("manifest", "min-sdk"),
        "target_sdk": ("manifest", "target-sdk"),
        "permissions": ("manifest", "permissions"),
        "debuggable": ("manifest", "debuggable"),
    }
    for name, suffix in commands.items():
        values[name] = run([analyzer, *suffix, apk]).text().strip()
    expected = {
        "application_id": APPLICATION_ID,
        "version_code": VERSION_CODE,
        "version_name": VERSION_NAME,
        "min_sdk": "26",
        "target_sdk": "37",
        "permissions": "",
        "debuggable": "false",
    }
    for key, expected_value in expected.items():
        if values[key] != expected_value:
            raise OracleError(
                f"APK {key} mismatch: expected {expected_value!r}, got {values[key]!r}"
            )
    with zipfile.ZipFile(apk) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise OracleError("APK has duplicate ZIP entry names")
        dex_names = [
            name
            for name in names
            if re.fullmatch(r"classes(?:[2-9]|[1-9][0-9]+)?\.dex", name)
        ]
        if "classes.dex" not in dex_names:
            raise OracleError("APK has no root classes.dex")
        dex_reports = []
        with tempfile.TemporaryDirectory(prefix="t01m-dexdump-") as temporary:
            temp_root = Path(temporary)
            for dex_name in sorted(dex_names):
                raw = archive.read(dex_name)
                dex_path = temp_root / dex_name
                dex_path.write_bytes(raw)
                report = run(
                    [TOOLS["dexdump"][0], "-d", "-f", dex_path], timeout=300.0
                ).stdout
                if not report or b"Class descriptor" not in report:
                    raise OracleError(f"dexdump did not parse class definitions: {dex_name}")
                dex_reports.append(
                    {
                        "path": dex_name,
                        "bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "dexdump_output_sha256": hashlib.sha256(report).hexdigest(),
                    }
                )
    xmltree = run(
        [
            TOOLS["aapt2"][0],
            "dump",
            "xmltree",
            str(apk),
            "--file",
            "AndroidManifest.xml",
        ],
        timeout=300.0,
    ).stdout
    if b"dev.lilies.civilizationseed" not in xmltree:
        raise OracleError("aapt2 xmltree does not bind the target package")
    return {
        **values,
        "path": str(apk.resolve()),
        "bytes": apk.stat().st_size,
        "sha256": sha256_file(apk),
        "root_dex_entries": sorted(dex_names),
        "dexdump_reports": dex_reports,
        "aapt2_manifest_xmltree_sha256": hashlib.sha256(xmltree).hexdigest(),
    }
