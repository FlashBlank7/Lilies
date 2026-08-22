#!/usr/bin/env bash
# 产品隔离荷载脚本 —— 把 Lilies 平台源码按【白名单】复制到产品目录（如 UTOO/modules/lilies），
# 并生成可审计的 SNAPSHOT_MANIFEST.json。
#
# 隔离契约（绝不进入产品快照）：
#   data/ workspaces/ .env* 「real- projects/」 docs/ benchmarks/ tests/ tmp/
#   artifacts/ references/ examples/ templates/ scripts/ 以及任何数据库/密钥/客户材料。
# 白名单之外的一切默认不复制——新增顶层目录不会静默流入产品。
#
# 用法：
#   scripts/vendor_snapshot.sh <target-dir>            # 荷载（覆盖 target 内容，先确认再跑）
#   scripts/vendor_snapshot.sh --verify <target-dir>   # 校验 target 与其 manifest 是否漂移
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"

MODE="vendor"
if [[ "${1:-}" == "--verify" ]]; then
  MODE="verify"
  shift
fi
TARGET="${1:?usage: vendor_snapshot.sh [--verify] <target-dir>}"

manifest_tree_hash() {
  # 对目录树做确定性哈希：排除构建产物（node_modules/.next），
  # 逐文件 sha256 后再整体 sha256。
  python3 - "$1" <<'PY'
import hashlib, os, sys
root = sys.argv[1]
entries = []
for base, dirs, files in os.walk(root):
    dirs[:] = sorted(d for d in dirs if d not in ("node_modules", ".next", "__pycache__"))
    for name in sorted(files):
        if name == "SNAPSHOT_MANIFEST.json":
            continue
        path = os.path.join(base, name)
        rel = os.path.relpath(path, root)
        digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
        entries.append(f"{rel}\t{digest}")
print(hashlib.sha256("\n".join(entries).encode()).hexdigest())
print(len(entries))
PY
}

if [[ "${MODE}" == "verify" ]]; then
  MANIFEST="${TARGET}/SNAPSHOT_MANIFEST.json"
  [[ -f "${MANIFEST}" ]] || { echo "no manifest: ${MANIFEST}" >&2; exit 1; }
  TREE_OUTPUT="$(manifest_tree_hash "${TARGET}")"
  ACTUAL_HASH="$(head -1 <<<"${TREE_OUTPUT}")"
  ACTUAL_COUNT="$(tail -1 <<<"${TREE_OUTPUT}")"
  EXPECTED_HASH="$(python3 -c "import json;print(json.load(open('${MANIFEST}'))['tree_sha256'])")"
  if [[ "${ACTUAL_HASH}" == "${EXPECTED_HASH}" ]]; then
    echo "OK: snapshot matches manifest (${ACTUAL_COUNT} files, ${ACTUAL_HASH:0:12})"
  else
    echo "DRIFT: snapshot tree ${ACTUAL_HASH:0:12} != manifest ${EXPECTED_HASH:0:12}" >&2
    exit 1
  fi
  exit 0
fi

# ── 荷载模式 ──────────────────────────────────────────────────────
# 白名单：产品运行所需的最小集合。
ALLOWLIST_FILES=(LICENSE README.md AGENTS.md pyproject.toml)
FRONTEND_ITEMS=(app lib public package.json package-lock.json next.config.ts tsconfig.json next-env.d.ts)

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/lilies-vendor.XXXXXX")"
trap 'rm -rf "${STAGE}"' EXIT

for item in "${ALLOWLIST_FILES[@]}"; do
  [[ -f "${SOURCE_ROOT}/${item}" ]] && cp "${SOURCE_ROOT}/${item}" "${STAGE}/"
done
mkdir -p "${STAGE}/platform/backend/src" "${STAGE}/platform/frontend"
cp -R "${SOURCE_ROOT}/platform/backend/src/agent_platform" "${STAGE}/platform/backend/src/"
for item in "${FRONTEND_ITEMS[@]}"; do
  src="${SOURCE_ROOT}/platform/frontend/${item}"
  [[ -e "${src}" ]] && cp -R "${src}" "${STAGE}/platform/frontend/"
done

# 先清构建缓存（cp -R 会带上 __pycache__），再跑违禁守卫
find "${STAGE}" \( -name __pycache__ -type d \) -prune -exec rm -rf {} +
find "${STAGE}" -name '*.pyc' -delete

# 复制后守卫：禁止名单出现在暂存区即失败（白名单出 bug 时的最后防线）
for forbidden in .env data workspaces "real- projects" docs benchmarks tmp artifacts __pycache__; do
  if find "${STAGE}" -name "${forbidden}" | grep -q .; then
    echo "FORBIDDEN content staged: ${forbidden}" >&2
    exit 1
  fi
done

# 生成 manifest（含来源 commit、脏文件清单、树哈希）
TREE_OUTPUT="$(manifest_tree_hash "${STAGE}")"
TREE_HASH="$(head -1 <<<"${TREE_OUTPUT}")"
FILE_COUNT="$(tail -1 <<<"${TREE_OUTPUT}")"
GIT_COMMIT="$(git -C "${SOURCE_ROOT}" rev-parse HEAD)"
GIT_BRANCH="$(git -C "${SOURCE_ROOT}" rev-parse --abbrev-ref HEAD)"
DIRTY="$(git -C "${SOURCE_ROOT}" status --porcelain | head -50)"
python3 - "$STAGE" <<PY
import json, sys, datetime
manifest = {
    "source_repo": "${SOURCE_ROOT}",
    "source_commit": "${GIT_COMMIT}",
    "source_branch": "${GIT_BRANCH}",
    "source_dirty_files": """${DIRTY}""".splitlines(),
    "vendored_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "tree_sha256": "${TREE_HASH}",
    "file_count": int("${FILE_COUNT}"),
    "note": "Allowlist vendor via scripts/vendor_snapshot.sh; verify with --verify.",
}
json.dump(manifest, open(sys.argv[1] + "/SNAPSHOT_MANIFEST.json", "w"), ensure_ascii=False, indent=2)
PY

if [[ -n "${DIRTY}" ]]; then
  echo "WARNING: source working tree is dirty — manifest records the dirty file list," >&2
  echo "         but a clean tagged commit is the recommended vendor source." >&2
fi

mkdir -p "${TARGET}"
# 保留目标自有文件（如 UTOO_INTEGRATION.md），只替换白名单内容
for item in "${ALLOWLIST_FILES[@]}" platform SNAPSHOT_MANIFEST.json; do
  rm -rf "${TARGET:?}/${item}"
  [[ -e "${STAGE}/${item}" ]] && cp -R "${STAGE}/${item}" "${TARGET}/"
done

echo "Vendored ${FILE_COUNT} files to ${TARGET} (tree ${TREE_HASH:0:12}, source ${GIT_COMMIT:0:8})"
