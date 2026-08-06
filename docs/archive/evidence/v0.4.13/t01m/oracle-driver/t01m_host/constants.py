import os
from pathlib import Path

TASK_ID = "V04-13-T01M"
ORACLE_ID = "T01M-CIVILIZATION-SEED-ANDROID-ORACLE-v1"
BRIEF_ID = "T01M-CIVILIZATION-SEED-ANDROID-v1"
APPLICATION_ID = "dev.lilies.civilizationseed"
DRIVER_PACKAGE = "dev.lilies.t01m.oracle"
INSTRUMENTATION = (
    "dev.lilies.t01m.oracle/"
    "dev.lilies.t01m.oracle.T01MOracleInstrumentation"
)
AVD_NAME = "T01M_API37_ARM64"
API_LEVEL = 37
ABI = "arm64-v8a"
VERSION_CODE = "1"
VERSION_NAME = "0.1.0"

REPO_ROOT = Path(__file__).resolve().parent.parent
ANDROID_SDK_ROOT_ENV = "T01M_ANDROID_SDK_ROOT"
JBR_HOME_ENV = "T01M_JBR_HOME"
BRIEF_PATH_ENV = "T01M_PROJECT_BRIEF_PATH"
ACCEPTANCE_ORACLE_PATH_ENV = "T01M_ACCEPTANCE_ORACLE_PATH"
ANDROID_SDK_ROOT = Path(
    os.environ.get(
        ANDROID_SDK_ROOT_ENV,
        os.environ.get(
            "ANDROID_SDK_ROOT",
            os.environ.get("ANDROID_HOME", "/__t01m_missing_android_sdk__"),
        ),
    )
)
JBR_HOME = Path(os.environ.get(JBR_HOME_ENV, "/__t01m_missing_jbr__"))
GIT = Path(os.environ.get("T01M_GIT", "/usr/bin/git"))
DRIVER_APK = REPO_ROOT / "dist" / "t01m-external-oracle.apk"
FLOW_CONFIG = REPO_ROOT / "config" / "a06-flow.json"
REDUCED_MOTION_CONFIG = REPO_ROOT / "config" / "a09-reduced-motion.json"
ACCESSIBILITY_CONFIG = REPO_ROOT / "config" / "a08-accessibility.json"

TOOLS = {
    "adb": (
        ANDROID_SDK_ROOT / "platform-tools/adb",
        "9fdf861259dc807937b13afdd5f053c7fda9f3b7726933fe0e0f45130ecb8dc7",
    ),
    "aapt2": (
        ANDROID_SDK_ROOT / "build-tools/37.0.0/aapt2",
        "13a206c0b022ba3b92f21b6f142f3a4b2d0f3bb1ac0bddfa820ee2c6b00c4c99",
    ),
    "apkanalyzer": (
        ANDROID_SDK_ROOT / "cmdline-tools/22.0/bin/apkanalyzer",
        "b549ff6c84f22e1339e1bc88854747a3867522e8f9d7676b5809f3372fcdae57",
    ),
    "dexdump": (
        ANDROID_SDK_ROOT / "build-tools/37.0.0/dexdump",
        "0328e326271bcb69350eb32ffc8053eb1e9de9cc245641a78ce322e5c4855c31",
    ),
}
ANDROID_JAR = ANDROID_SDK_ROOT / "platforms/android-37.0/android.jar"

DRIVER_SHA256 = "47f0568de6855f8bf893d76328cfacb1f354d0c7f4c416f224a35f649e893120"
TALKBACK_SERVICE = (
    "com.google.android.marvin.talkback/"
    "com.google.android.marvin.talkback.TalkBackService"
)
TALKBACK_APK_SHA256 = (
    "bb98c6f7904429485f67e082d45f7274faeb6670fb284bab1aa655fb3d7885f9"
)

CONTROL_FILES = {
    "evidence-leaves.json",
    "evidence-manifest.json",
    "independent-review.json",
    "closure-envelope.json",
}
REQUIRED_SCREENSHOTS = [
    "01_onboarding.png",
    "02_empty_library.png",
    "03_created_seeds.png",
    "04_restoring_detail.png",
    "05_restored_filter.png",
    "06_font_scale_200.png",
    "07_reduced_motion_a.png",
    "08_reduced_motion_b.png",
]
REQUIRED_ARTIFACT_FILES = [
    "oracle-result.json",
    "package-analysis.json",
    "apk-entry-inventory.json",
    "resource-inventory.json",
    "permission-dump.txt",
    "dex-reference-scan.json",
    "shared-storage-before-1.json",
    "shared-storage-before-2.json",
    "shared-storage-after-1.json",
    "shared-storage-after-2.json",
    "shared-storage-diff.json",
    "ui-flow-trace.json",
    "persistence-trace.json",
    "accessibility-report.json",
    "talkback-focus-trace.json",
    "text-character-boxes.json",
    "contrast-report.json",
    "motion-report.json",
    "runtime-clean-report.json",
    "sanitized-logcat.txt",
]
REQUIRED_ARTIFACT_DIRECTORIES = [
    "contrast-masks",
    "motion-frames",
    "motion-masks",
    "reduced-motion-frames",
    "ui-hierarchy",
    "screenshots",
]
ALL_CASE_IDS = [f"A{index:02d}" for index in range(1, 11)]
