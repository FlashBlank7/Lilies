#!/usr/bin/env python3
"""Lilies 日本女偶像万字长文 — LLM直接从训练数据生成"""

from __future__ import annotations
import json, sys, time, itertools
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent / "platform" / "backend" / "src"))
from fastapi.testclient import TestClient
from agent_platform.api import create_app
from agent_platform.config import Settings

H = {"Authorization": "Bearer test-token-2024"}
_ctr = itertools.count()

def mut(c, aid, rev, op, data):
    r = c.post(f"/api/v1/applications/{aid}/draft", headers=H, json={
        "expected_revision": rev, "idempotency_key": f"idol-{next(_ctr)}",
        "op": op, "data": data,
    })
    if r.status_code != 200: raise RuntimeError(f"{op}: {r.text[:200]}")
    return r.json()["revision"]

SYSTEM_PROMPT = (
    "你是日本最顶尖的娱乐记者，拥有25年从业经验，"
    "对日本女性偶像行业有百科全书级的了解。"
    "你的写作风格：极其详尽、热情洋溢、数据驱动、充满洞察。"
    "你从不简短——每个段落至少250字，每个章节充分展开。"
    "你了解每个团体的历史、成员、代表曲、销量数据、综艺活动。"
    "输出纯Markdown格式，目标10000字以上。"
)

REPORT_PROMPT = (
    "请撰写一份关于日本女性偶像团体的超详尽综合报告（10000字以上，中文）。\n\n"
    "=== 必须包含的章节 ===\n\n"
    "## 📰 封面摘要\n"
    "撰写一段300-400字的精彩概述，概括当前日本女性偶像界的整体状况、"
    "主要趋势和本报告的精华内容。\n\n"
    "## 🎵 坂道系列团体最新动态\n"
    "### 乃木坂46\n"
    "详细报道600字以上：最新单曲信息、成员变动（毕业/加入）、"
    "近期演唱会、综艺节目出演、握手会活动、年度选拔结果。\n"
    "### 櫻坂46\n"
    "详细报道500字以上：改名后的发展轨迹、音乐风格演变、"
    "核心成员动态、最新作品信息。\n"
    "### 日向坂46\n"
    "详细报道500字以上：团体特色、综艺成就、成员人气排行、最新动态。\n\n"
    "## 🌸 AKB48集团最新动态\n"
    "详细报道600字以上：AKB48本店及姐妹团（SKE48/NMB48/HKT48/NGT48/STU48）"
    "的最新情况、剧场公演、总选举改革、新生代成员介绍。\n\n"
    "## 🎤 早安家族（Hello! Project）\n"
    "详细报道500字以上：早安少女组、ANGERME、Juice=Juice、"
    "Tsubaki Factory等团体的最新作品和活动。\n\n"
    "## 🌍 K-POP日本籍女性偶像\n"
    "详细报道600字以上：TWICE日本成员（Mina/Sana/Momo）、"
    "LE SSERAFIM（Kazuha/Sakura）、IVE（Rei）、"
    "NiziU、XG等团体中的日本成员动态与成就。\n\n"
    "## 📊 业界趋势深度分析\n"
    "撰写1000字以上的深度分析：\n"
    "- 2024-2025年日本偶像市场规模与变化\n"
    "- 粉丝经济：握手会、线上特典会、NFT、粉丝俱乐部模式\n"
    "- 国际化战略：K-POP冲击下的日本偶像海外拓展\n"
    "- 流媒体与社交媒体战略变迁\n"
    "- 新人培养体系的变化\n\n"
    "## ⭐ 本月值得关注的新人推荐\n"
    "推荐5-8位近期受关注的新人/新团体成员，每位100字以上，"
    "说明推荐理由和潜力。\n\n"
    "## 🔥 热点话题与社交媒体\n"
    "撰写800字以上：近期社交媒体（X/Instagram/TikTok）上的热门话题、"
    "粉丝讨论焦点、排行榜变动、有争议的事件。\n\n"
    "## 💬 粉丝社区动态\n"
    "撰写600字以上：粉丝应援活动、线上社区讨论热点、"
    "粉丝创作（同人/翻跳/翻唱）、应援广告等。\n\n"
    "## 🏆 编辑精选推荐\n"
    "精选5首近期必听的歌曲和5个必看的舞台表演，附推荐理由。\n\n"
    "## 📅 明日预告\n"
    "预告下一期报告将关注的话题和团体。\n\n"
    "## 📊 数据来源说明\n"
    "说明本报告的数据来源和搜集方法。\n\n"
    "=== 重要提醒 ===\n"
    "- 每个章节必须充分展开，至少达到要求的字数\n"
    "- 尽可能包含具体的人名、歌名、日期、数据\n"
    "- 使用丰富的Markdown格式：**粗体**、- 列表、> 引用、表格\n"
    "- 目标总字数：10000字以上\n"
    "- 现在开始撰写："
)

def main():
    tmp = TemporaryDirectory(); tp = Path(tmp.name)
    s = Settings(api_token="test-token-2024", data_dir=tp/"data", workspace_root=tp/"workspaces")
    s.prepare()
    (tp/"workspaces").mkdir(parents=True, exist_ok=True); (tp/"workspaces").chmod(0o777)
    app = create_app(settings=s)

    with TestClient(app) as client:
        print("█" * 70)
        print("  日本女偶像新闻 · 万字长文生成测试")
        print("█" * 70)

        app_id = client.post("/api/v1/applications", headers=H, json={
            "name": "偶像日报", "requirement": "Generate 10000+ char idol report.",
        }).json()["id"]
        rev = client.get(f"/api/v1/applications/{app_id}/draft", headers=H).json()["revision"]

        # Simple pipeline: Start → LLM → Template → End
        for node_data in [
            ("s", "start", "Start", {"inputs": []}),
            ("llm", "llm", "LLM", {
                "system": SYSTEM_PROMPT,
                "prompt": REPORT_PROMPT,
                "model": "deepseek-v4-pro",
            }),
            ("fmt", "template_transform", "Format", {
                "template": "# 🎤 日本女性アイドル総合日報\n\n> 本报告由 AI 自动生成 | 目标：10000字以上\n\n---\n\n{{ content }}",
                "variables": {"content": {"$ref": {"node_id": "llm", "path": ["text"]}}},
            }),
            ("e", "end", "Output", {
                "outputs": {
                    "report": {"$ref": {"node_id": "fmt", "path": ["text"]}},
                    "raw": {"$ref": {"node_id": "llm", "path": ["text"]}},
                },
            }),
        ]:
            rev = mut(client, app_id, rev, "add_node", {"node": {"id": node_data[0], "type": node_data[1], "title": node_data[2], "config": node_data[3]}})
        for src, tgt, sp in [("s","llm","output"), ("llm","fmt","text"), ("fmt","e","text")]:
            rev = mut(client, app_id, rev, "add_edge", {"edge": {"id": f"e_{src}_{tgt}", "source": src, "target": tgt, "source_port": sp, "target_port": "input"}})

        v = client.post(f"/api/v1/applications/{app_id}/draft/validate", headers=H).json()
        print(f"  验证: valid={v['valid']}")

        print(f"\n  🚀 启动...")
        t0 = time.time()
        rr = client.post(f"/api/v1/applications/{app_id}/runs", headers=H, json={
            "inputs": {}, "use_draft": True, "workspace_path": ".",
        })
        if rr.status_code != 202:
            print(f"  ❌ {rr.text[:300]}"); return 1
        run_id = rr.json()["run_id"]

        seen = 0
        for i in range(300):
            time.sleep(1)
            ev = client.get(f"/v1/streams/{run_id}?after={seen}", headers=H).json()
            for e in ev:
                seen += 1
                t = e.get("type",""); d = e.get("data",{})
                if "text.delta" in t and seen % 50 == 0:
                    sys.stdout.write("."); sys.stdout.flush()
                elif "node.completed" in t and d.get("type") == "llm":
                    print(f"\n  🤖 LLM生成完成")
                elif "node.completed" in t:
                    print(f"  ✅ [{d.get('type','')}] {d.get('title','')}")
                elif "node.failed" in t:
                    print(f"  ❌ [{d.get('type','')}]: {str(d.get('error',''))[:200]}")
                elif "workflow.completed" in t:
                    print(f"  🎉 完成!")
                elif "workflow.failed" in t:
                    print(f"  ❌ 失败: {str(d.get('error',''))[:300]}")

            rec = client.get(f"/api/v1/runs/{run_id}", headers=H).json()
            if rec["status"] in ("succeeded","failed"): break

        elapsed = time.time() - t0
        rec = client.get(f"/api/v1/runs/{run_id}", headers=H).json()
        print(f"\n  耗时: {elapsed:.0f}s | 状态: {rec['status']}")

        if rec["status"] != "succeeded":
            print(f"  错误: {rec.get('error','')[:500]}"); return 1

        outputs = rec.get("outputs",{})
        report = outputs.get("report","") or outputs.get("raw","")
        rlen = len(report)
        cn = sum(1 for c in report if '一' <= c <= '鿿')

        print(f"\n{'═'*60}")
        print(f"  结果分析")
        print(f"{'═'*60}")
        print(f"  总字符数: {rlen}")
        print(f"  中文字数: {cn}")
        print(f"  英文/数字: {rlen - cn}")
        print(f"  达标(≥10000): {'✅ YES!' if rlen >= 10000 else f'❌ 差{10000-rlen}字'}")

        # Show beginning, middle, end
        print(f"\n  ── 开头 (前400字) ──")
        print(report[:400])
        mid = rlen // 2
        print(f"\n  ── 中段 (~{mid}字) ──")
        print(report[max(0,mid-250):mid+250])
        print(f"\n  ── 结尾 (后400字) ──")
        print(report[-400:])

        # Save
        path = Path("/home/jiangzhijun/Lilies/idol_report.md")
        path.write_text(report, encoding="utf-8")
        print(f"\n  📄 保存: {path} ({path.stat().st_size} bytes)")

    try: tmp.cleanup()
    except: pass
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
