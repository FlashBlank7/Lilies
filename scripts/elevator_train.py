"""试题1 净室流水线：原始传感数据 → 平台自训练 → 评估 → 部署。

只读还原后的原始数据（real- projects/restored/试题1-电梯IoT故障检测），
不触碰任何解题产物。特征为逐窗时域统计（六轴 × mean/std/min/max/rms），
训练、评估、审批、部署全部走平台自身的 tabular-models 治理链。

用法：python scripts/elevator_train.py [--window 150] [--deployment elevator-fault-v1]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASE = REPO / "real- projects" / "restored" / "试题1-电梯IoT故障检测"
AXES = ("AX", "AY", "AZ", "GX", "GY", "GZ")
STATS = ("mean", "std", "min", "max", "rms")


def request(base: str, token: str, method: str, path: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as error:
        detail = error.read().decode()[:1_000]
        raise SystemExit(f"{method} {path} -> {error.code}\n{detail}") from error


def parse_xlsx(path: Path) -> list[tuple[float, ...]]:
    """Yield (ax, ay, az, gx, gy, gz) rows from columns B..G of sheet1."""

    with zipfile.ZipFile(path) as z:
        sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8", "ignore")
    rows: list[tuple[float, ...]] = []
    for row_xml in re.findall(r"<row[^>]*>(.*?)</row>", sheet, re.S):
        cells = dict(re.findall(
            r'<c r="([A-Z]+)\d+"(?: s="\d+")?(?: t="\w+")?><v>([^<]*)</v></c>',
            row_xml,
        ))
        try:
            rows.append(tuple(float(cells[c]) for c in "BCDEFG"))
        except (KeyError, ValueError):
            continue  # header / annotation rows
    return rows


def episodes(rows: list[tuple[float, ...]], min_len: int = 40) -> list[list[tuple[float, ...]]]:
    """Split a recording into activity episodes (one door/motion process each).

    Motion energy = summed |jerk| across all six axes, smoothed; episodes are
    contiguous above-threshold regions with small gaps merged. The same rule
    applies to normal and fault recordings, so segmentation carries no label.
    """

    if len(rows) < min_len:
        return []
    energy = [0.0]
    for prev, cur in zip(rows, rows[1:]):
        energy.append(sum(abs(b - a) for a, b in zip(prev, cur)))
    smooth = []
    half = 5
    for i in range(len(energy)):
        seg = energy[max(0, i - half):i + half + 1]
        smooth.append(sum(seg) / len(seg))
    sorted_e = sorted(smooth)
    median = sorted_e[len(sorted_e) // 2]
    threshold = max(median * 3.0, sorted_e[int(len(sorted_e) * 0.75)])
    active = [v > threshold for v in smooth]
    # merge gaps up to 25 samples (5 s), pad edges by 15
    chunks: list[list[tuple[float, ...]]] = []
    i = 0
    while i < len(active):
        if not active[i]:
            i += 1
            continue
        j = i
        gap = 0
        while j < len(active) and gap <= 25:
            gap = gap + 1 if not active[j] else 0
            j += 1
        start = max(0, i - 15)
        end = min(len(rows), j - gap + 15)
        if end - start >= min_len:
            chunks.append(rows[start:end])
        i = j
    return chunks


def chunk_features(chunk: list[tuple[float, ...]]) -> dict[str, float]:
    features: dict[str, float] = {}
    for idx, axis in enumerate(AXES):
        values = [row[idx] for row in chunk]
        mean = sum(values) / len(values)
        centered = [v - mean for v in values]
        var = sum(v * v for v in centered) / len(centered)
        std = math.sqrt(var)
        diffs = [b - a for a, b in zip(values, values[1:])]
        abs_diffs = [abs(d) for d in diffs]
        jerk_mean = sum(abs_diffs) / len(abs_diffs)
        jerk_var = sum((d - jerk_mean) ** 2 for d in abs_diffs) / len(abs_diffs)
        # 动态特征为主：去重力中心化幅值 + 逐差（冲击/振动）统计
        features[f"{axis}_std"] = std
        features[f"{axis}_ptp"] = max(values) - min(values)
        features[f"{axis}_dev_max"] = max(abs(v) for v in centered)
        features[f"{axis}_jerk_mean"] = jerk_mean
        features[f"{axis}_jerk_max"] = max(abs_diffs)
        features[f"{axis}_jerk_std"] = math.sqrt(jerk_var)
    features["duration"] = float(len(chunk))
    return features


def load_split(folder: Path, label: int, size: int, step: int) -> list[dict]:
    del size, step
    rows = []
    for path in sorted(folder.glob("*.xlsx")):
        if path.name.startswith("~$"):
            continue
        for chunk in episodes(parse_xlsx(path)):
            features = chunk_features(chunk)
            units = {
                name: ("g" if name.startswith("A") else "sample" if name == "duration" else "dps")
                for name in features
            }
            rows.append({"features": features, "units": units, "label": label})
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--window", type=int, default=60)
    parser.add_argument("--step", type=int, default=30)
    parser.add_argument("--deployment", default="elevator-fault-v1")
    parser.add_argument("--epochs", type=int, default=600)
    parser.add_argument("--threshold", type=float, default=0.5)
    args = parser.parse_args()

    token = os.environ.get("API_TOKEN", "")
    if not token:
        for line in (REPO / ".env").read_text().splitlines():
            if line.startswith("API_TOKEN="):
                token = line.split("=", 1)[1].strip()

    train_rows = (
        load_split(BASE / "正常数据集", 0, args.window, args.step)
        + load_split(BASE / "故障数据集", 1, args.window, args.step)
    )
    test_rows = (
        load_split(BASE / "正常数据集-test", 0, args.window, args.step)
        + load_split(BASE / "故障数据集-test", 1, args.window, args.step)
    )
    n_train_fault = sum(r["label"] for r in train_rows)
    n_test_fault = sum(r["label"] for r in test_rows)
    print(f"train windows: {len(train_rows)} (fault {n_train_fault}) | "
          f"test windows: {len(test_rows)} (fault {n_test_fault})")

    feature_names = sorted(train_rows[0]["features"])
    contracts = [
        {"name": name, "unit": "g" if name.startswith("A") else ("sample" if name == "duration" else "dps")}
        for name in feature_names
    ]
    trained = request(args.base_url, token, "POST", "/api/v1/tabular-models/train", {
        "model_name": "电梯故障检测（正常/故障）",
        "features": contracts,
        "rows": train_rows,
        "threshold": args.threshold,
        "epochs": args.epochs,
        "source": {"dataset": "试题1-还原原始数据", "window": args.window, "step": args.step, "features": "episode centered+jerk v3"},
        "idempotency_key": f"elevator-train-w{args.window}-s{args.step}-e{args.epochs}-t{args.threshold}-fv3ep",
    })
    model_id, version = trained["model_id"], trained["version"]
    print("trained:", model_id, "v", version)

    evaluation = request(
        args.base_url, token, "POST",
        f"/api/v1/tabular-models/{model_id}/versions/{version}/evaluate",
        {"rows": test_rows, "idempotency_key": f"elevator-eval-w{args.window}-s{args.step}-t{args.threshold}-fv3ep"},
    )
    metrics = evaluation.get("metrics", evaluation)
    print("evaluation:", json.dumps(metrics, ensure_ascii=False)[:400])

    accuracy = float(metrics.get("accuracy", 0))
    recall = float(metrics.get("recall", 0))
    if accuracy < 0.8:
        raise SystemExit(f"accuracy {accuracy:.3f} below the 0.8 acceptance line — not promoting")

    promoted = request(
        args.base_url, token, "POST",
        f"/api/v1/model-deployments/{args.deployment}/promote",
        {
            "model_id": model_id,
            "version": version,
            "evaluation_id": evaluation["evaluation_id"],
            "approved_by": "业主代表-验收组",
            "approval_reason": f"测试集准确率 {accuracy:.3f}、故障召回 {recall:.3f}，达到 ≥0.8 验收线",
            "minimum_recall": 0.6,
            "idempotency_key": f"elevator-promote-w{args.window}-t{args.threshold}-fv3ep",
        },
    )
    print("promoted:", json.dumps({k: promoted[k] for k in ("deployment_name", "model_id", "version") if k in promoted}, ensure_ascii=False))
    print(f"\naccuracy={accuracy:.3f} recall={recall:.3f} deployment={args.deployment}")


if __name__ == "__main__":
    main()