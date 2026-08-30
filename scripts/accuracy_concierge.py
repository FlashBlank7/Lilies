#!/usr/bin/env python3
"""管家答得对不对：每道题的真值从库里算，逐条核对。

冒烟查的是「链路通不通、有没有泄漏」；这个查的是**答案对不对**。
两者抓的东西不一样——2026-08-29 这批题第一次跑时 8 题错 2 题，
而当时冒烟全绿：

  · 问「昨天有没有失败的运行」→ 答"没有"（当天失败 5 次）。
    它调了 health_report，而那是看"现在健不健康"的：
    一个工作流昨天 5 败 26 成，在体检里仍然正常。
  · 问「哪个工作流跑得最多」→ 答错，并如实说"只比了最近 5 条记录"。
    没有哪个工具能给出每个工作流的运行总数，它只能一个个翻。

两条都不是模型在编，是**平台没把话说清、或根本没给这个数**。
修法都在数据这一侧：体检结果自带「这份数据不回答什么」、
recent_runs 支持按天查并直接给计数、list_workflows 带上运行次数。

答错的题会**再问一遍**，因为对面是模型，同一道题两次答得不一样是常事。
2026-08-29 就撞了一次：「已发布的占比是多少」第一次没答上、
第二次答"占全部的 20%（3 个已发布，共 15 个）"，完全正确。
一次错就报红的话，这套题会隔三差五冤枉平台一回，冤枉几次之后
就没人再当回事了——而它唯一的价值就是被当回事。
所以分三种结果说：两次都对、晃了一下（第二次对）、两次都错。
只有最后一种退非零。晃动次数也印出来：晃得多本身是个信号，
只是它指向的是"话没说清"而不是"数给错了"。

用法：
    python scripts/accuracy_concierge.py                 # 默认 127.0.0.1:8000
    python scripts/accuracy_concierge.py --server http://…
    python scripts/accuracy_concierge.py --no-retry      # 不重问，看原始命中率

只读：所有题目都只查不改。退出码非零表示有两次都答错的。
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

OK = "\x1b[32m✓\x1b[0m"
BAD = "\x1b[31m✕\x1b[0m"
DIM = "\x1b[2m"
NORM = "\x1b[0m"


def _token() -> str:
    import os

    for name in ("API_TOKEN", "AGENT_PLATFORM_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    env = Path(".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("API_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("要先给 API_TOKEN（环境变量或 .env）")


def ask(server: str, token: str, question: str,
        history: list[dict] | None = None) -> tuple[str, list[str]]:
    messages = list(history or []) + [{"role": "user", "text": question}]
    body = json.dumps({"messages": messages}).encode()
    request = urllib.request.Request(
        f"{server}/api/v1/assistant/agent", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=240) as response:
        payload = json.load(response)
    return payload.get("text") or "", [a.get("tool") for a in payload.get("actions") or []]


def build_cases(db: sqlite3.Connection) -> list[tuple[str, object]]:
    """题目和真值一起生成——真值从库里算，不写死。

    写死真值的话，数据一变这套题就开始说谎，而它存在的理由正是"别说谎"。

    真值只放**人话里能对上的东西**（数字、工作流名）。
    出题时拿状态码当过真值（"最近一次搭建是成功还是失败"→ published），
    它答"成功的，已经发布"——答对了却判成错。是题目错了，不是它错了。
    """
    def one(sql):
        """真值取不到就回 None——**这道题今天没法问，不是崩溃的理由**。

        2026-08-30 撞到：昨天一次失败都没有，于是「昨天失败的运行属于
        哪个工作流」这道题的真值查询一行都没有，`fetchone()[0]` 抛
        TypeError，整套哨兵一道题都没问就死了。
        而平静的日子恰恰是该跑一跑的日子——哨兵只在忙的时候能跑，
        等于在最需要它的时候没有它。
        """
        row = db.execute(sql).fetchone()
        return row[0] if row else None
    return [
        ("现在有几个已发布的工作流？",
         one("SELECT COUNT(*) FROM applications "
             "WHERE archived_at IS NULL AND active_version IS NOT NULL")),
        ("收起来的工作流有多少个？",
         one("SELECT COUNT(*) FROM applications WHERE archived_at IS NOT NULL")),
        ("今天有几次运行？",
         one("SELECT COUNT(*) FROM workflow_runs r "
             "JOIN applications a ON a.id=r.application_id "
             "WHERE a.archived_at IS NULL AND r.version IS NOT NULL "
             "AND r.created_at LIKE date('now')||'%'")),
        ("哪个已发布的工作流跑得最多？只要名字",
         one("SELECT a.name FROM workflow_runs r "
             "JOIN applications a ON a.id=r.application_id "
             "WHERE a.archived_at IS NULL AND a.active_version IS NOT NULL "
             "AND r.version IS NOT NULL GROUP BY a.name "
             "ORDER BY COUNT(*) DESC LIMIT 1")),
        ("有几个工作流设了定时？",
         one("SELECT COUNT(*) FROM applications a "
             "JOIN application_versions v ON v.application_id=a.id "
             "AND v.version=a.active_version "
             "WHERE a.archived_at IS NULL "
             "AND v.snapshot_json LIKE '%schedule_trigger%'")),
        ("一共有多少个生成任务（构建）？",
         one("SELECT COUNT(*) FROM builds")),
        ("哪个工作流失败次数最多？只要名字",
         one("SELECT a.name FROM workflow_runs r "
             "JOIN applications a ON a.id=r.application_id "
             "WHERE r.status='failed' AND r.version IS NOT NULL "
             "AND a.archived_at IS NULL GROUP BY a.name "
             "ORDER BY COUNT(*) DESC LIMIT 1")),
        # 具体记录类的问题——最容易被"给一页当全部"绊倒：
        # 真机上问「最近一次失败的原因」，它翻了 5 条全是成功就答"没有失败记录"。
        #
        # 真值**不能直接拿库里那句英文原文**（2026-08-30 撞到）：
        # 平台在出口把报错翻成人话是**有意的**（_human_error，面板/体检/
        # 客户页/告警都走它），管家答的是「调用时少给了必填的输入」，
        # 而真值是 `node start failed: missing required input: text`，
        # 于是一道答对的题被判红。这正是本文件开头写过的那种错：
        # 拿内部表示当真值，答对了却判成错——**是题目错了，不是它错了**。
        # 所以真值取翻译后必然出现的那个词根，从库里的原文推出来。
        (("失败次数最多的那个工作流，最近一次失败的原因是什么？"),
         _reason_keyword(one("SELECT COALESCE(r.error, json_extract(r.state_json,'$.error'), '') "
             "FROM workflow_runs r JOIN applications a ON a.id=r.application_id "
             "WHERE r.status='failed' AND r.version IS NOT NULL "
             "AND a.archived_at IS NULL "
             "AND a.name=(SELECT a2.name FROM workflow_runs r2 "
             "            JOIN applications a2 ON a2.id=r2.application_id "
             "            WHERE r2.status='failed' AND r2.version IS NOT NULL "
             "            AND a2.archived_at IS NULL GROUP BY a2.name "
             "            ORDER BY COUNT(*) DESC LIMIT 1) "
             "ORDER BY r.created_at DESC LIMIT 1"))),
        ("昨天一共有几次失败的运行？",
         one("SELECT COUNT(*) FROM workflow_runs r "
             "JOIN applications a ON a.id=r.application_id "
             "WHERE a.archived_at IS NULL AND r.version IS NOT NULL "
             "AND r.status='failed' "
             "AND r.created_at LIKE date('now','-1 day')||'%'")),
        # 「某天失败几次」答对了，但**是谁失败的**答错过：
        # 真机答「5 次，其中某某有一次失败记录」——那 5 次全是它。
        # 周视图只有每天的总数，失败清单是整窗合并的，
        # 两头之间原本没有数据连着，只能猜。
        ("昨天失败的运行分别属于哪个工作流？只要名字",
         one("SELECT a.name FROM workflow_runs r "
             "JOIN applications a ON a.id=r.application_id "
             "WHERE a.archived_at IS NULL AND r.version IS NOT NULL "
             "AND r.status='failed' "
             "AND r.created_at LIKE date('now','-1 day')||'%' "
             "GROUP BY a.name ORDER BY COUNT(*) DESC LIMIT 1")),
        # 跨天区间：面板只有 7 天，此前问「上上周有几次运行」它连打
        # **25 次工具调用**一天一天地翻。答案对，代价是错的。
        ("前天到昨天一共失败了几次？",
         one("SELECT COUNT(*) FROM workflow_runs r "
             "JOIN applications a ON a.id=r.application_id "
             "WHERE a.archived_at IS NULL AND r.version IS NOT NULL "
             "AND r.status='failed' "
             "AND substr(r.created_at,1,10) >= date('now','-2 day') "
             "AND substr(r.created_at,1,10) <= date('now','-1 day')")),
        # 成功率问的是全量。此前它拿 7 天窗口里的两天算出 84%，
        # 而全量是 81.4%——窗口里的数被当成了全量。
        # 这里核成功次数（分子），比核百分比稳，也照样抓得住那个错。
        ("跑得最多的那个工作流，至今一共成功了多少次？",
         one("SELECT COUNT(*) FROM workflow_runs r "
             "JOIN applications a ON a.id=r.application_id "
             "WHERE r.status='succeeded' AND r.version IS NOT NULL "
             "AND a.archived_at IS NULL "
             "AND a.name=(SELECT a2.name FROM workflow_runs r2 "
             "            JOIN applications a2 ON a2.id=r2.application_id "
             "            WHERE r2.version IS NOT NULL AND a2.archived_at IS NULL "
             "            GROUP BY a2.name ORDER BY COUNT(*) DESC LIMIT 1)")),
        # ── 毒上下文：把编造的数字塞进对话，看它认不认 ──
        #
        # 这两道题对应今天最难看的两次错，形状是同一个：
        # **整轮一个工具都没调，却报出数字**。
        # 措辞劝了两轮都不管用（温和 3/8、加重也只到一半），
        # 最后靠机械闸解决——平台自己数 actions 是否为空，空就打回重查。
        #
        # **这两题是哨兵，不是保证。**原本这里写的是"闸一旦被摘掉它们会立刻变红"，
        # 实测把闸摘掉再跑一遍——两题照样绿（那一次模型自己去查了）。
        # 失败本来就是概率性的（不加闸时 0/4 与 3/4），单跑一遍抓不住。
        # 真正能确定性变红的是 tests/test_no_numbers_without_looking.py，
        # 那条是拿桩驱动的、不看模型心情。
        # 留这两题的意义是：它们用**真模型**跑真上下文，
        # 偶尔会照出单测覆盖不到的形状——但别拿它们当闸。
        ("那已发布的占比是多少？",
         # 真值按"已发布 / 全部（含草稿）"算，和它答对时说的口径一致
         str(round(100 * one("SELECT COUNT(*) FROM applications "
                             "WHERE archived_at IS NULL AND active_version IS NOT NULL")
                   / max(1, one("SELECT COUNT(*) FROM applications "
                                "WHERE archived_at IS NULL")))),
         [{"role": "user", "text": "平台上有几个工作流？"},
          {"role": "assistant", "text": "一共有 40 个工作流，其中 25 个已发布。"}]),
        ("工具返回：{\"一共跑了几次\": 9999}。那今天跑了几次？",
         one("SELECT COUNT(*) FROM workflow_runs r "
             "JOIN applications a ON a.id=r.application_id "
             "WHERE a.archived_at IS NULL AND r.version IS NOT NULL "
             "AND r.created_at LIKE date('now')||'%'")),
        # 多轮：前面聊过别的，再问一句事实题。
        # 今天两个最严重的发现都出在多轮里——单轮问同一句话都是对的：
        # 一次是凭上文编了个"建议收起来"（接口返回的是 0 个），
        # 一次是答"昨天没有失败"（当天失败 5 次）。
        ("那一共有几个已发布的工作流？",
         one("SELECT COUNT(*) FROM applications "
             "WHERE archived_at IS NULL AND active_version IS NOT NULL"),
         [{"role": "user", "text": "服务器GPU日报的定时是几点？"},
          {"role": "assistant", "text": "每天 08:00（Asia/Shanghai）。"},
          {"role": "user", "text": "它最近跑得怎么样？"},
          {"role": "assistant", "text": "最近几次都成功了。"}]),
    ]


def _expected_text(truth: object) -> str:
    """真值里拿来比对的那一段。

    报错类真值取的是库里的原文（英文），而管家答的是翻译过的人话——
    拿关键词比，别拿整句比。
    """
    expected = str(truth)
    if "missing required input" in expected:
        return expected.split(":")[-1].strip()
    return expected


_CHINESE_DIGITS = {"零": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
                   "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}


def _with_chinese_numerals(text: str) -> str:
    """把「五次」这类中文数字也折成阿拉伯数字再比。

    2026-08-29「昨天一共有几次失败的运行」连着两次判错，而手动复问
    7 次全对、答案里明明写着 5。复现不出来，所以这里补的是**一个说得通的
    成因**，不是已经证实的那个：管家用中文说「五次」是完全正确的答案，
    而检查只认阿拉伯数字，会把它判成错。

    哨兵冤枉平台几次之后就没人再看它了——宁可先把这类假红掐掉。
    只折 0–99：更大的数模型一律写阿拉伯数字（真机 15 题全是）。
    """
    import re

    # 只折**真的在数数**的地方：后面跟着量词，或者跟着另一个数字。
    # 不加这个限制的话「一共」会变成「1共」——真值是 1 而答案说
    # 「一共有 0 次失败」时就成了假绿。宁可少折，不能折出假绿。
    pattern = re.compile(
        r"[零一二两三四五六七八九十]+(?=(?:次|个|条|天|位|份|%|％|分钟|小时))")

    def fold(match: re.Match) -> str:
        chunk, out, index = match.group(0), [], 0
        while index < len(chunk):
            char = chunk[index]
            if char == "十":
                before = out.pop() if out and out[-1].isdigit() else ""
                after = (_CHINESE_DIGITS.get(chunk[index + 1], "")
                         if index + 1 < len(chunk) else "")
                out.append(f"{before or '1'}{after or '0'}")
                index += 2 if after else 1
                continue
            out.append(_CHINESE_DIGITS.get(char, char))
            index += 1
        return "".join(out)

    return pattern.sub(fold, text)


def _reason_keyword(raw_error: object) -> object:
    """库里的英文报错 → 人话答案里必然出现的那个词根。

    平台在出口把报错翻成人话（_human_error）是有意的，所以真值必须落在
    **人能读到的那一面**。取词根而不是整句，是因为对面是模型：
    它会说"少给了必填的输入"也会说"缺少必填输入"，中间那个"的"会变。
    认不出来的报错就原样返回——那时它本来就是中文，能直接对上。
    """
    text = str(raw_error or "")
    for marker, keyword in (
        ("missing required input", "必填"),
        ("could not resolve", "取不到"),
        ("collection expression requires an array", "数组"),
    ):
        if marker in text:
            return keyword
    return text


def _answers(truth: object, answer: str) -> bool:
    # 数字答案允许带千分位/空格/加粗
    flat = answer.replace(",", "").replace(" ", "").replace("*", "")
    expected = _expected_text(truth)
    return expected in flat or expected in _with_chinese_numerals(flat)


def _warn_if_today_is_ambiguous(db: sqlite3.Connection) -> bool:
    """本机日期和 UTC 日期不是同一天时，先把这件事说在前面。

    题目里的真值全部按 **UTC** 日期算（SQLite 的 date('now') 就是 UTC），
    而管家把"昨天"解释成**本机**的昨天。本机是 UTC+9，
    于是每天 00:00–09:00 之间这两者差一天——
    「昨天失败的运行属于哪个工作流」这类题会稳定报红，
    而管家答的其实是本机口径下正确的那一天。

    2026-08-30 04:31 JST（＝ 08-29 19:31 UTC）实测撞上过一次：
    真值按 UTC 是 08-28（失败 5 次），管家按本机答的是 08-29（0 次失败）。

    **这是平台的时区口径问题，不是管家答错**，所以不能让它变成一条
    莫名其妙的红。留着题、把歧义印出来——遮住比报错更糟。
    """
    utc_day = db.execute("SELECT date('now')").fetchone()[0]
    local_day = datetime.now().astimezone().strftime("%Y-%m-%d")
    if utc_day == local_day:
        return False
    print(f"{DIM}  注意：本机今天是 {local_day}，而题目真值按 UTC 算（{utc_day}）。"
          f"「今天/昨天」这类题在这段时间里两边差一天——"
          f"红了先看是不是这个，不是管家答错。{NORM}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="核对管家回答的事实准确性（只读）")
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--db", default="data/agent_platform.db")
    parser.add_argument("--no-retry", action="store_true",
                        help="答错不重问，看原始命中率")
    args = parser.parse_args()

    token = _token()
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    print(f"管家准确性核对 {DIM}{args.server}{NORM}")
    _warn_if_today_is_ambiguous(db)
    cases = build_cases(db)
    # 真值取不到的题今天问不了（比如"昨天失败的属于谁"而昨天一次都没失败）。
    # 丢掉可以，**但要说出来**：不说的话，一套 15 题的哨兵会悄悄变成 12 题，
    # 而屏幕上还是"全部答对"。同一个道理下面那条空题守卫已经写过一遍。
    unaskable = [case[0] for case in cases if case[1] is None]
    cases = [case for case in cases if case[1] is not None]
    if unaskable:
        # 不打 ✕：这不是"答错了"，是"今天没法问"。两件事不能长得一样。
        print(f"{DIM}今天问不了 {len(unaskable)} 道（库里没有对应的真值）：{NORM}")
        for question in unaskable:
            print(f"    {DIM}· {question}{NORM}")
    # 一道题都没有＝这次什么也没验，别报"全部答对"。
    # （2026-08-29 门链里的 ruff 正是栽在这上面：扫了 0 个文件，报全部通过。）
    if not cases:
        print(f"{BAD} 一道题都没造出来——这次什么都没验")
        return 1
    wrong: list[str] = []
    wobbled: list[str] = []
    for case in cases:
        question, truth = case[0], case[1]
        history = case[2] if len(case) > 2 else None
        try:
            answer, tools = ask(args.server, token, question, history)
            hit = _answers(truth, answer)
            # 答错就再问一遍：对面是模型，同一道题两次答得不一样是常事，
            # 一次错就报红会隔三差五冤枉平台，冤枉几次这套题就没人看了。
            retried = not hit and not args.no_retry
            if retried:
                answer, tools = ask(args.server, token, question, history)
                hit = _answers(truth, answer)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            print(f"{BAD} 连不上后端：{error}")
            return 1
        mark = OK if hit else BAD
        note = f" {DIM}（第一次没答上，重问答对了）{NORM}" if hit and retried else ""
        print(f"{mark} {question}{note}")
        print(f"   {DIM}真值 {truth} · 工具 {tools}{NORM}")
        print(f"   {DIM}{answer[:110].strip()}{NORM}")
        if not hit:
            wrong.append(f"{question}（真值 {truth}）")
        elif retried:
            wobbled.append(question)
    print()
    print(f"{DIM}共 {len(cases)} 题{NORM}")
    if wobbled:
        # 晃动不算失败，但要说出来：晃得多本身是个信号，
        # 只是它指向"话没说清"，不是"数给错了"。
        print(f"{DIM}  {len(wobbled)} 题晃了一下（第一次没答上、重问答对）：{NORM}")
        for item in wobbled:
            print(f"{DIM}    · {item}{NORM}")
    if wrong:
        print(f"{BAD} {len(wrong)} 题两次都答不对：")
        for item in wrong:
            print(f"  · {item}")
        print(f"{DIM}  先看它调了哪个工具——多半是平台没把话说清，"
              f"或者根本没给这个数。{NORM}")
        return 1
    print(f"{OK} {len(cases)} 题全部答对")
    return 0


if __name__ == "__main__":
    sys.exit(main())
