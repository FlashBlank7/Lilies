"""电梯原题盲测·独立复核器：她说的不算，复算的才算。

两头验收的"另一头"：
1. 用**她写的** feature_extract.py（在应用工作区）对 test 原始数据独立提特征
   —— 在沙盒镜像里跑，与她的开发环境一致；
2. 把特征逐行打进**她部署的**模型推理端点；
3. 按 test 文件名标签算总体准确率与分故障类型识别率，对照题面验收线（总体 ≥80%）。

用法：
  python scripts/elevator_exam_verify.py --app <application_id> --deployment <name>
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _token() -> str:
    token = os.environ.get("API_TOKEN", "")
    if not token:
        for line in (REPO / ".env").read_text().splitlines():
            if line.startswith("API_TOKEN="):
                token = line.split("=", 1)[1].strip()
    return token


def _predict(base: str, token: str, deployment: str, features: dict) -> dict:
    payload = json.dumps({"features": features}).encode()
    request = urllib.request.Request(
        f"{base}/api/v1/model-deployments/{deployment}/predict",
        data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True, help="application_id（工作区目录名）")
    parser.add_argument("--deployment", required=True, help="她部署的模型服务名")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--workspace-root", default=os.environ.get("WORKSPACE_ROOT", "workspaces"))
    args = parser.parse_args()

    workspace = (REPO / args.workspace_root / args.app).resolve()
    script = workspace / "feature_extract.py"
    if not script.is_file():
        print(f"✗ 她没有交出 feature_extract.py（{script}）——可复现性要求未满足")
        return 1

    # 1. 沙盒里独立复算 test 特征（正常 label=0，故障 label=1，按目录注入）
    out_file = "verify_test_features.jsonl"
    runs = [("data/正常数据集-test", 0), ("data/故障数据集-test", 1)]
    (workspace / out_file).unlink(missing_ok=True)
    for rel_dir, label in runs:
        cmd = [
            "docker", "run", "--rm", "--network", "none",
            "-v", f"{workspace}:/workspace", "-w", "/workspace",
            "agent-platform-sandbox:latest",
            "python3", "feature_extract.py", rel_dir,
            "--label", str(label), "--append", out_file,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            # 兼容：她的脚本可能不支持 --label/--append 约定，回退为
            # "目录 → stdout JSONL"，标签由复核器按目录补
            cmd = [
                "docker", "run", "--rm", "--network", "none",
                "-v", f"{workspace}:/workspace", "-w", "/workspace",
                "agent-platform-sandbox:latest",
                "python3", "feature_extract.py", rel_dir,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if result.returncode != 0:
                print(f"✗ 特征脚本在 {rel_dir} 上运行失败：\n{result.stderr[-800:]}")
                return 1
            with (workspace / out_file).open("a", encoding="utf-8") as handle:
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if not line or not line.startswith("{"):
                        continue
                    row = json.loads(line)
                    row["label"] = label
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    rows = []
    with (workspace / out_file).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    if len(rows) < 4:
        print(f"✗ 复算特征只有 {len(rows)} 行——脚本输出约定不符或 test 数据没被处理")
        return 1

    # 2. 逐行打她部署的模型
    token = _token()
    correct = 0
    by_type: dict[str, list[bool]] = {}
    for row in rows:
        prediction = _predict(args.base_url, token, args.deployment, row["features"])
        predicted = 1 if prediction.get("prediction") in (1, True, "fault", "1") else 0
        truth = int(row["label"])
        hit = predicted == truth
        correct += hit
        kind = str(row.get("source", row.get("file", "未知")))
        by_type.setdefault("故障" if truth else "正常", []).append(hit)

    total = len(rows)
    accuracy = correct / total
    print("═" * 52)
    print("独立复核（她的特征脚本 + 她部署的模型 + test 集真值）")
    print("═" * 52)
    print(f"样本 {total} | 命中 {correct} | 总体准确率 {accuracy:.1%}")
    for kind, hits in sorted(by_type.items()):
        print(f"  {kind}: {sum(hits)}/{len(hits)} = {sum(hits)/len(hits):.1%}")
    line = "✓ 达到题面验收线（≥80%）" if accuracy >= 0.80 else "✗ 未达题面验收线（≥80%）"
    print(line)
    return 0 if accuracy >= 0.80 else 1


if __name__ == "__main__":
    sys.exit(main())
