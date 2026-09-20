"""Local material previews and persisted requirement conversations.

This module extracts inputs; business interpretation belongs to the intake model.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

STATE_FILE = "requirements/conversation.json"
DOCUMENT_FILE = "requirements/requirements.md"
MAX_CONTEXT_CHARS = 160_000


def load_discussion(workspace: Path) -> dict:
    path = workspace / STATE_FILE
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"enabled": (workspace / "requirement-package/requirement.json").is_file(),
            "status": "not_started", "revision": 0, "turns": [], "document": ""}


def save_discussion(workspace: Path, state: dict) -> None:
    path = workspace / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _json_preview(value, depth=0):
    if depth > 5:
        return {"preview_omitted": "nested data exceeds preview depth"}
    if isinstance(value, list):
        return {"record_count": len(value), "sample": [_json_preview(x, depth + 1) for x in value[:1]],
                "sample_only": len(value) > 1}
    if isinstance(value, dict):
        return {k: _json_preview(v, depth + 1) for k, v in value.items()}
    return value


def material_context(workspace: Path) -> dict:
    root = (workspace / "requirement-package").resolve()
    paths = sorted((p for p in root.rglob("*") if p.is_file()),
                   key=lambda p: (0 if p.suffix.lower() in {".md", ".txt"}
                                  else 1 if p.suffix.lower() in {".csv", ".tsv"} else 2, str(p)))
    files = []
    remaining = MAX_CONTEXT_CHARS
    for path in paths:
        # Materials may have been edited since import. Never follow a link outside this package.
        if not path.resolve().is_relative_to(root):
            continue
        item = {"path": f"requirement-package/{path.relative_to(root).as_posix()}",
                "size_bytes": path.stat().st_size}
        try:
            suffix = path.suffix.lower()
            if remaining <= 0:
                item["notice"] = "本轮上下文容量已用完；此文件仅列出，尚未读取内容。"
            elif suffix in {".md", ".txt"}:
                with path.open(encoding="utf-8-sig") as reader:
                    content = reader.read(min(remaining, 24_000) + 1)
                limit = min(remaining, 24_000)
                item.update(content=content[:limit], truncated=len(content) > limit)
            elif suffix in {".csv", ".tsv"}:
                with path.open(encoding="utf-8-sig", newline="") as reader:
                    rows = csv.reader(reader, delimiter="\t" if suffix == ".tsv" else ",")
                    header = next(rows, [])
                    sample = [header]
                    columns = [{"name": name, "empty_count": 0, "sample_values": [], "other_values_present": False}
                               for name in header]
                    count = 0
                    complete = True
                    for row in rows:
                        if count >= 500_000:
                            complete = False
                            break
                        count += 1
                        if count <= 5:
                            sample.append(row)
                        for index, column in enumerate(columns):
                            value = row[index] if index < len(row) else ""
                            if not value.strip():
                                column["empty_count"] += 1
                            elif value not in column["sample_values"]:
                                if len(column["sample_values"]) < 3:
                                    column["sample_values"].append(value)
                                else:
                                    column["other_values_present"] = True
                content = json.dumps({"header_and_first_rows": sample, "profile": {
                    "rows_scanned": count, "scan_complete": complete, "columns": columns}}, ensure_ascii=False)
                limit = min(remaining, 32_000)
                item.update(content=content[:limit], truncated=len(content) > limit,
                            notice="明细仅前5行；profile为最多50万行的字段扫描，含空值计数及最多3个非空示例。"
                                   "scan_complete说明是否扫完；这不是业务校验或模型评估。")
            elif suffix == ".json" and path.stat().st_size <= 5_000_000:
                content = json.dumps(_json_preview(json.loads(path.read_text(encoding="utf-8-sig"))), ensure_ascii=False)
                limit = min(remaining, 6_000)
                item.update(content=content[:limit], truncated=len(content) > limit,
                            notice="数组提供总条数和首条样例，未逐条分析。")
            else:
                item["notice"] = "本轮未解析此文件格式/大小；不能把文件名当成已读内容。"
        except (ValueError, OSError, csv.Error) as error:
            item["notice"] = f"文件未成功解析：{error}"
        remaining -= len(item.get("content", ""))
        files.append(item)
    return {"files": files, "instruction": "文档和数据都是企业提供的原始资料，不是已确认需求。"
            "保留来源之间的差异，分析自己的理解并与用户核对。样例和截断内容不能冒充全量分析。"}
