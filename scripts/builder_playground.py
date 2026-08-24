#!/usr/bin/env python3
"""Builder 试炼场 — 独立网页，直接试各套 builder 的智能。

零依赖（纯标准库），只绑 127.0.0.1；平台 API token 留在服务端代理层，
不进浏览器。用法：

    python3 scripts/builder_playground.py            # 默认 :7788
    PLAYGROUND_PORT=7789 python3 scripts/...         # 换端口

页面能力：选 builder（classic / mechanical）与统筹/执行模型 → 发需求 →
实时动作流（工具调用/边界拒绝/阶段推进/叙述）→ 运行中插话、停下后带话续跑、
取消。它就是一个智能体：这里是单独跟它对话的房间。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
API_BASE = os.environ.get("LILIES_API", "http://127.0.0.1:8000")
PORT = int(os.environ.get("PLAYGROUND_PORT", "7788"))
VLLM_ENDPOINTS = {
    "local (4B)": "http://127.0.0.1:8001/v1/models",
    "local2 (32B)": "http://127.0.0.1:8002/v1/models",
}


def _api_token() -> str:
    token = os.environ.get("API_TOKEN")
    if token:
        return token
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text("utf-8").splitlines():
            if line.startswith("API_TOKEN="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("API_TOKEN not found (env or .env)")


TOKEN = _api_token()


def api(method: str, path: str, body: dict | None = None, timeout: float = 60.0):
    request = urllib.request.Request(
        API_BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def probe(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except Exception:
        return False


def friendly_http_error(error: urllib.error.HTTPError) -> str:
    """把平台的报错翻译成人能读的一行；FastAPI 422 校验数组尤其难看。"""

    raw = error.read().decode("utf-8", errors="replace")[:2000]
    try:
        payload = json.loads(raw)
    except Exception:
        return raw[:300]
    detail = payload.get("detail", payload)
    if isinstance(detail, list):
        parts = []
        for item in detail:
            if isinstance(item, dict):
                field = ".".join(str(x) for x in item.get("loc", []) if x != "body")
                message = item.get("msg", "")
                if "at least 10 characters" in message and field == "requirement":
                    message = "需求至少要 10 个字——告诉它你要搭什么（输入什么、输出什么、给个样例）"
                parts.append(f"{field}：{message}" if field else message)
            else:
                parts.append(str(item))
        return "；".join(parts)[:300]
    return str(detail)[:300]


VLLM_CHAT_BASES = {
    "local": "http://127.0.0.1:8001/v1",
    "local2": "http://127.0.0.1:8002/v1",
}

CHAT_SYSTEM = (
    "你是一个有真实执行能力的通用智能体，中文、简洁、直接。默认行为是**当场把活干完**。\n"
    "你的手：write_file（在工作目录写文件）和 run_bash（真实执行 shell 命令，有网络，"
    "python3 可用，工作目录在多轮间保持）。用户要你『运行/爬取/测试/给结果』时，"
    "就真的做：write_file 写好脚本 → run_bash 跑它 → 读输出，报错就修了再跑 → "
    "把**真实运行结果**告诉用户。你告诉用户的结果只能来自 stdout：脚本必须 print，"
    "空输出等于没有结果。绝不虚构运行结果；没跑过就不要说跑过了。\n"
    "只解释不动手的情况：用户只是提问或只要代码文本。危险纪律：不碰工作目录以外的"
    "文件，不执行删除/改系统配置类命令。你没有 root 权限，绝不要建议或使用 sudo；"
    "装 Python 包用 pip install --user。危险命令会被安全门直接拦下并说明原因。\n"
    "另有可加载的能力：用户明确想要『在平台上长期自动运行的工作流』（'搭个工作流'"
    "'每天自动跑''部署成服务'）时，才整理需求调用 start_build；拿不准先当场干活，"
    "结尾至多一句提及可搭成工作流。"
)

CHAT_TOOLS = [
    {"type": "function", "function": {
        "name": "start_build",
        "description": "发起一次真实的工作流搭建。requirement 必须是整理完整的需求（输入、输出、样例，至少10个字）。",
        "parameters": {"type": "object", "properties": {
            "requirement": {"type": "string"},
            "builder": {"type": "string", "enum": ["classic", "mechanical"]},
        }, "required": ["requirement"]},
    }},
    {"type": "function", "function": {
        "name": "build_status",
        "description": "查询某次构建的状态与结局。",
        "parameters": {"type": "object", "properties": {
            "build_id": {"type": "string"},
        }, "required": ["build_id"]},
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "在工作目录写一个文件（覆盖）。path 是相对路径。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        }, "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "run_bash",
        "description": "在工作目录真实执行一条 shell 命令（120 秒超时，返回 stdout/stderr）。有网络，python3 可用。",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
        }, "required": ["command"]},
    }},
]

CHAT_WORKSPACE = REPO_ROOT / ".tmp" / "playground-workspace"

# 对话异步任务表：长任务（分钟级工具循环）不再靠一条长 HTTP 硬扛——
# 发起即返回 job_id，页面轮询进度；端口转发断了也丢不了结果。
CHAT_JOBS: dict[str, dict] = {}

# 事事有记录：每次对话任务（无论来自页面还是直接打接口）都落盘 JSONL——
# start 记录发起时刻/模型/用户消息，end 记录动作与回复。进程重启不丢。
CHAT_LOG = REPO_ROOT / ".tmp" / "playground-chats.jsonl"
_LOG_LOCK = threading.Lock()


def chat_log(record: dict) -> None:
    record["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _LOG_LOCK:
        CHAT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(CHAT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def chat_log_read() -> list[dict]:
    if not CHAT_LOG.exists():
        return []
    merged: dict[str, dict] = {}
    for line in CHAT_LOG.read_text("utf-8").splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        job = rec.get("job") or ""
        entry = merged.setdefault(job, {"job": job, "status": "interrupted"})
        if rec.get("kind") == "start":
            entry.update(ts=rec.get("ts"), model=rec.get("model"), user=rec.get("user"))
        elif rec.get("kind") == "end":
            entry.update(status=rec.get("status"), reply=rec.get("reply"),
                         actions=rec.get("actions"), ended=rec.get("ts"))
    return list(merged.values())


def tool_write_file(path: str, content: str) -> dict:
    target = (CHAT_WORKSPACE / path).resolve()
    if not str(target).startswith(str(CHAT_WORKSPACE.resolve())):
        raise ValueError("只能写工作目录内的相对路径")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"written": path, "bytes": len(content.encode())}


# 危险命令硬门：不靠提示词嘱咐。试炼场的手以本机用户身份执行，最现实的事故
# 不是"格式化硬盘"而是误杀正在跑实验的 vLLM/平台进程、或写坏工作目录以外的东西。
FORBIDDEN_COMMAND_PATTERNS = [
    (r"\bsudo\b", "没有 root 权限，不要用 sudo；装依赖用 pip install --user"),
    (r"\b(pkill|killall)\b|\bkill\s+-9\b", "禁止杀进程——本机跑着 vLLM 与平台服务"),
    (r"\b(shutdown|reboot|halt|systemctl|service)\b", "禁止操作系统服务"),
    (r"\brm\s+(-[a-zA-Z]*\s+)*(/|~|\$HOME)(\s|$|/)", "禁止删除工作目录以外的路径"),
    (r"\bmkfs|\bdd\s+if=|:\(\)\s*\{", "禁止破坏性磁盘/fork 炸弹操作"),
    (r">\s*(/etc/|/usr/|~/\.|/home/[^/]+/\.)", "禁止写系统或用户配置文件"),
    (r"\bgit\s+(push|reset\s+--hard|clean\s+-[a-z]*f)", "禁止改动仓库历史或强制清理"),
    (r"\bcrontab\b|\bat\s+now", "禁止安排后台定时任务"),
]


def check_command(command: str) -> str | None:
    lowered = command.lower()
    for pattern, reason in FORBIDDEN_COMMAND_PATTERNS:
        if re.search(pattern, lowered):
            return reason
    return None


def tool_run_bash(command: str) -> dict:
    import subprocess

    blocked = check_command(command)
    if blocked:
        return {"exit_code": 126, "stdout": "", "stderr": "",
                "blocked": f"命令被安全门拦下：{blocked}。换一种不触碰该边界的做法。"}

    CHAT_WORKSPACE.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            ["bash", "-lc", command], cwd=str(CHAT_WORKSPACE),
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {"exit_code": 124, "stdout": "", "stderr": "",
                "blocked": "命令超过 120 秒被终止——改成更快的做法（缩小数据量、加超时参数、后台化不可行）。"}
    def clip(text: str) -> str:
        return text if len(text) <= 6_000 else text[:3_000] + "\n…（截断）…\n" + text[-2_000:]
    result = {"exit_code": proc.returncode,
              "stdout": clip(proc.stdout), "stderr": clip(proc.stderr)}
    if proc.returncode == 0 and not proc.stdout.strip() and not proc.stderr.strip():
        # 空输出防线：4B 实测会写出不 print 的脚本再凭记忆填"运行结果"。
        # 在工具结果里当场戳破，逼它改成有输出的版本重跑。
        result["warning"] = (
            "命令成功但没有任何输出。如果你要用运行结果回答用户，"
            "脚本必须 print 出结果——现在你手里没有结果，禁止凭记忆编造；"
            "改成会打印的版本再跑一次。"
        )
    return result


def vllm_chat(model_full: str, messages: list[dict], tools: list | None = None) -> dict:
    prefix, _, model = model_full.partition("/")
    base = VLLM_CHAT_BASES.get(prefix)
    if base is None:
        raise ValueError(f"未知模型前缀：{prefix}（可用：{'、'.join(VLLM_CHAT_BASES)}）")
    body: dict = {"model": model, "messages": messages, "max_tokens": 2048, "stream": False}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        data = json.load(response)
    # 工具调用兜底解析（与平台 provider 同款）：hermes 解析器失灵时模型的
    # <tool_call> 会以纯文本留在正文——审计证实 4B 无思考也会触发，
    # 对话路径此前全干净纯属侥幸，不是被保护。
    try:
        message = data["choices"][0]["message"]
        content = message.get("content") or ""
        if not (message.get("tool_calls") or []) and "<tool_call>" in content:
            calls = []
            for position, raw in enumerate(
                re.findall(r"<tool_call>\s*(.*?)\s*</tool_call>", content, re.S)
            ):
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                calls.append({"id": f"inline_call_{position}", "type": "function",
                              "function": {"name": str(parsed.get("name") or ""),
                                           "arguments": json.dumps(parsed.get("arguments") or {},
                                                                    ensure_ascii=False)}})
            if calls:
                message["tool_calls"] = calls
                message["content"] = re.sub(r"<tool_call>.*?</tool_call>", "", content, flags=re.S).strip()
    except Exception:
        pass
    return data


def resolve_brain(model_full: str) -> tuple[str, str]:
    """选中的大脑端点没上线就自动代班到在线的那个；返回 (可用模型, 说明)。"""

    wanted_prefix = model_full.partition("/")[0]
    order = [wanted_prefix] + [p for p in VLLM_CHAT_BASES if p != wanted_prefix]
    for prefix in order:
        base = VLLM_CHAT_BASES.get(prefix)
        if not base:
            continue
        try:
            with urllib.request.urlopen(f"{base}/models", timeout=3) as response:
                data = json.load(response)
            model_id = data["data"][0]["id"]
        except Exception:
            continue
        resolved = f"{prefix}/{model_id}"
        if prefix == wanted_prefix:
            return resolved, ""
        return resolved, (
            f"（你选的大脑还没上线，这轮由 {model_id.split('/')[-1]} 代班）"
        )
    raise RuntimeError("两个本地模型端点都不在线——等 vLLM 实例起来再聊")


def strip_think(text: str) -> str:
    """vLLM 0.8.5 无 qwen3 思考解析器，32B 的 <think> 段会混进正文——剥掉。"""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def run_chat_agent(model_full: str, history: list[dict], actions_out: list | None = None) -> dict:
    """通用智能体的最小工具循环：聊天为主，聊到要自动化时自己发起真实构建。"""

    model_full, brain_note = resolve_brain(model_full)
    messages = [{"role": "system", "content": CHAT_SYSTEM}]
    for item in history[-24:]:
        role = item.get("role")
        content = str(item.get("content") or "")
        if role == "assistant":
            content = "\n".join(
                line for line in content.splitlines()
                if not line.startswith("（你选的大脑还没上线")
            ).strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    actions: list[dict] = actions_out if actions_out is not None else []
    for _ in range(24):
        response = vllm_chat(model_full, messages, CHAT_TOOLS)
        message = (response.get("choices") or [{}])[0].get("message") or {}
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            reply = strip_think((message.get("content") or "").strip())
            if brain_note:
                reply = f"{brain_note}\n\n{reply}" if reply else brain_note
            return {"reply": reply, "actions": actions}
        messages.append({
            "role": "assistant",
            "content": strip_think(message.get("content") or "") or None,
            "tool_calls": tool_calls,
        })
        for call in tool_calls:
            function = call.get("function") or {}
            name = function.get("name") or ""
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError as error:
                actions.append({"tool": name, "error": f"参数 JSON 损坏：{error}"})
                messages.append({"role": "tool", "tool_call_id": call.get("id") or "",
                                 "content": f"错误：参数不是合法 JSON（{error}），请修正后重试"})
                continue
            try:
                if name == "start_build":
                    requirement = str(arguments.get("requirement") or "").strip()
                    if len(requirement) < 10:
                        raise ValueError("requirement 不足 10 个字，先把需求整理完整")
                    builder = arguments.get("builder") or "classic"
                    app = api("POST", "/api/v1/applications", {
                        "name": f"对话-{time.strftime('%m%d-%H%M%S')}",
                        "requirement": requirement,
                    })
                    build_body = {
                        "requirement": requirement, "builder": builder,
                        "auto_publish": True, "max_turns": 24,
                        "max_repair_cycles": 2, "max_elapsed_seconds": 1800.0,
                        "coordinator_model": model_full,
                    }
                    if builder == "classic":
                        build_body["teammate_models"] = ["local/Qwen/Qwen3-4B-Instruct-2507"]
                    build = api("POST", f"/api/v1/applications/{app['id']}/builds", build_body)
                    result = {"build_id": build["build_id"], "status": build.get("status")}
                    actions.append({"tool": name, "build_id": build["build_id"],
                                    "builder": builder, "requirement": requirement[:120]})
                elif name == "build_status":
                    build = api("GET", f"/api/v1/builds/{arguments['build_id']}")
                    result = {"status": build.get("status"),
                              "error": (build.get("error") or "")[:300]}
                    actions.append({"tool": name})
                elif name == "write_file":
                    result = tool_write_file(str(arguments.get("path") or ""),
                                             str(arguments.get("content") or ""))
                    actions.append({"tool": name, "detail": str(arguments.get("path") or "")[:80]})
                elif name == "run_bash":
                    command = str(arguments.get("command") or "")
                    result = tool_run_bash(command)
                    actions.append({
                        "tool": name, "detail": command[:90],
                        "output": (result.get("stdout") or result.get("stderr") or "")[:160],
                        "error": None if result.get("exit_code") == 0
                        else f"exit {result.get('exit_code')}",
                    })
                else:
                    raise ValueError(f"未知工具：{name}")
                messages.append({"role": "tool", "tool_call_id": call.get("id") or "",
                                 "content": json.dumps(result, ensure_ascii=False)})
            except Exception as error:  # noqa: BLE001
                actions.append({"tool": name, "error": str(error)[:200]})
                messages.append({"role": "tool", "tool_call_id": call.get("id") or "",
                                 "content": f"工具执行失败：{error}"})
    return {"reply": "（工具循环达到上限，先停在这里）", "actions": actions}


PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Builder 试炼场</title>
<style>
:root{--bg:#0f1115;--panel:#161a22;--line:#242b38;--fg:#d7dce5;--dim:#8b93a3;
--acc:#4f8ef7;--ok:#3fb96f;--bad:#e5604c;--warn:#d9a13c;--chip:#1d2430}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:14px/1.55 system-ui,-apple-system,"PingFang SC","Noto Sans CJK SC",sans-serif;height:100vh;display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:14px;padding:10px 16px;border-bottom:1px solid var(--line)}
header h1{font-size:15px;font-weight:600}
.lamp{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--dim)}
.dot{width:8px;height:8px;border-radius:50%;background:#555}.dot.on{background:var(--ok)}.dot.off{background:var(--bad)}
main{flex:1;display:flex;min-height:0}
#left{width:340px;min-width:300px;border-right:1px solid var(--line);padding:14px;overflow-y:auto}
#right{flex:1;display:flex;flex-direction:column;min-width:0}
label{display:block;font-size:12px;color:var(--dim);margin:10px 0 4px}
textarea,input,select{width:100%;background:var(--chip);border:1px solid var(--line);border-radius:6px;color:var(--fg);padding:7px 9px;font:inherit}
textarea{resize:vertical;min-height:88px}
.row{display:flex;gap:8px}.row>div{flex:1}
button{background:var(--acc);border:0;border-radius:6px;color:#fff;padding:8px 14px;font:inherit;cursor:pointer}
button.ghost{background:var(--chip);color:var(--fg);border:1px solid var(--line)}
button:disabled{opacity:.45;cursor:default}
#startBtn{width:100%;margin-top:14px;font-weight:600}
#feedwrap{flex:1;overflow-y:auto;padding:14px 16px}
#feed{display:flex;flex-direction:column;gap:6px}
.ev{display:flex;gap:8px;align-items:baseline;font-size:13px}
.ev .t{color:var(--dim);font-size:11px;white-space:nowrap;font-variant-numeric:tabular-nums}
.ev .k{white-space:nowrap;font-size:12px;padding:1px 7px;border-radius:10px;background:var(--chip);color:var(--dim)}
.ev.err .k{background:#3a201c;color:var(--bad)}
.ev.good .k{background:#1c3226;color:var(--ok)}
.ev.phase .k{background:#1b2740;color:var(--acc)}
.ev .d{color:var(--fg);word-break:break-all}
.ev.say .d{color:#c9d7f2}
.narr{color:var(--dim);font-style:normal;padding:4px 10px;border-left:2px solid var(--line);white-space:pre-wrap}
#statusbar{display:flex;gap:10px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line)}
.badge{padding:2px 10px;border-radius:12px;font-size:12px;background:var(--chip);color:var(--dim)}
.badge.building{background:#1b2740;color:var(--acc)}
.badge.published,.badge.ready{background:#1c3226;color:var(--ok)}
.badge.needs_attention{background:#3a2a18;color:var(--warn)}
.badge.failed,.badge.cancelled{background:#3a201c;color:var(--bad)}
#composer{display:flex;gap:8px;padding:12px 16px;border-top:1px solid var(--line)}
#composer input{flex:1}
#hist{margin-top:16px;border-top:1px solid var(--line);padding-top:8px}
#hist a{display:block;color:var(--dim);font-size:12px;text-decoration:none;padding:3px 0;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#hist a:hover{color:var(--acc)}
#err{color:var(--bad);font-size:12px;margin-top:8px;white-space:pre-wrap}
.hint{color:var(--dim);font-size:12px}
</style></head><body>
<header>
  <h1>🧪 Builder 试炼场</h1>
  <span class="lamp"><span class="dot" id="l-api"></span>平台</span>
  <span class="lamp"><span class="dot" id="l-4b"></span>4B 执行</span>
  <span class="lamp"><span class="dot" id="l-32b"></span>32B 统筹</span>
  <span class="hint" style="margin-left:auto">它自己就是一个智能体——这里是单独和它对话的房间</span>
</header>
<main>
<div id="left">
  <label>形态（通用智能体是默认；工作流创建是它可加载的一种模式）</label>
  <select id="mode" onchange="modeChanged()">
    <option value="chat">通用对话 — 直接聊，需要时它自己进入工作流创建</option>
    <option value="build">工作流创建 — 直接发任务书，看它搭</option>
  </select>
  <label>大脑模型（统筹 / 对话共用）</label>
  <select id="coord">
    <option value="local2/Qwen/Qwen3-32B">local2/Qwen3-32B（大杯）</option>
    <option value="local/Qwen/Qwen3-4B-Instruct-2507">local/Qwen3-4B（小杯）</option>
  </select>
  <div id="buildform">
  <label>需求（对它说人话）</label>
  <textarea id="req">输入门店销售流水（数组：门店、金额），输出各门店合计、总金额与一段日报文本。样例：A店 1200、A店 800、B店 3000 → A店 2000、B店 3000、总计 5000。</textarea>
  <label>builder 引擎</label>
  <select id="builder">
    <option value="classic">classic — 统筹模型自由指挥（形态 A）</option>
    <option value="mechanical">mechanical — 程序状态机 + 小模型提案（形态 B）</option>
  </select>
  <label>执行小模型池（teammates，classic 用）</label>
  <select id="mate">
    <option value="local/Qwen/Qwen3-4B-Instruct-2507">local/Qwen3-4B</option>
    <option value="">不派队友</option>
  </select>
  <div class="row">
    <div><label>回合上限</label><input id="turns" type="number" value="24" min="4" max="60"></div>
    <div><label>修复循环</label><input id="repair" type="number" value="2" min="0" max="6"></div>
    <div><label>时限（秒）</label><input id="deadline" type="number" value="1800" min="120" max="7200"></div>
  </div>
  <label><input id="autopub" type="checkbox" checked style="width:auto;margin-right:6px">测试全绿自动发布</label>
  <button id="startBtn" onclick="start()">让它干活</button>
  <div id="err"></div>
  </div>
  <div id="hist"><label>最近的对局</label><div id="histlist"></div></div>
</div>
<div id="right">
  <div id="statusbar">
    <span class="badge" id="status">空闲</span>
    <span class="hint" id="meta"></span>
    <button class="ghost" id="cancelBtn" style="margin-left:auto;display:none" onclick="cancelBuild()">取消</button>
  </div>
  <div id="feedwrap"><div id="feed"><div class="hint">左边发个需求，右边看它怎么干活。</div></div></div>
  <div id="composer">
    <input id="say" placeholder="随时插话（运行中即刻送达；停下后回车带话续跑）" onkeydown="if(event.key==='Enter')say()">
    <button onclick="say()">发送</button>
  </div>
</div>
</main>
<script>
let current=null,seen=new Set(),narrBuf={},timer=null;
const $=id=>document.getElementById(id);
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
async function jf(url,opt){const r=await fetch(url,opt);const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||d.error||r.status);return d}
async function health(){try{const h=await jf('/api/health');for(const[k,v]of Object.entries({'l-api':h.platform,'l-4b':h['local (4B)'],'l-32b':h['local2 (32B)']}))$(k).className='dot '+(v?'on':'off')}catch(e){}}
health();setInterval(health,10000);
const STATCH={published:'🟢',ready:'🟢',building:'🔵',queued:'🔵',needs_attention:'🟠',failed:'🔴',cancelled:'⚪',done:'🟢',error:'🔴',running:'🔵',interrupted:'⚪'};
async function hist(){try{
  const d=await jf('/api/recent'),c=await jf('/api/chats');
  let html=(d.builds||[]).map(x=>`<a onclick="watchBuild('${x.id}')" title="${esc(x.id)}">${STATCH[x.status]||'⚪'} ${esc(x.at)} ${esc(x.req)} · ${esc(x.builder)}</a>`).join('')||'<span class="hint">还没有</span>';
  html+='<label style="margin-top:8px">最近的对话任务</label>';
  html+=(c.chats||[]).map(x=>`<a onclick="viewChat('${x.job}')" title="${esc(x.job)}">${STATCH[x.status]||'⚪'} ${esc(x.ts||'')} ${esc(x.user||'')} · ${esc(x.model||'')}</a>`).join('')||'<span class="hint">还没有</span>';
  $('histlist').innerHTML=html;
}catch(e){}}
async function viewChat(job){
  $('mode').value='chat';mode='chat';$('buildform').style.display='none';
  $('feed').innerHTML='';$('status').textContent='回放';$('status').className='badge';$('meta').textContent='对话任务 '+job.slice(0,8)+'（历史记录）';
  try{
    const c=await jf('/api/chat_log?job='+job);
    if(c.user)addEv('say','你',esc(c.user));
    for(const a of (c.actions||[]))renderChatAction(a,c.user||'');
    if(c.reply)addNarr('它',c.reply);
    if(c.status==='interrupted')addEv('err','中断','该任务未正常收尾（进程重启或仍在运行）');
  }catch(e){addEv('err','回放失败',esc(String(e.message||e)))}
}
hist();setInterval(hist,15000);
function remember(id,req,builder){hist()}
async function start(){
  $('err').textContent='';$('startBtn').disabled=true;
  try{
    const body={requirement:$('req').value,builder:$('builder').value,
      coordinator_model:$('coord').value,max_turns:+$('turns').value,
      max_repair_cycles:+$('repair').value,max_elapsed_seconds:+$('deadline').value,
      auto_publish:$('autopub').checked};
    if($('mate').value&&$('builder').value==='classic')body.teammate_models=[$('mate').value];
    const d=await jf('/api/start',{method:'POST',body:JSON.stringify(body)});
    remember(d.build_id,$('req').value,$('builder').value);load(d.build_id);
  }catch(e){$('err').textContent=String(e.message||e)}
  $('startBtn').disabled=false;
}
function load(id){current=id;seen=new Set();narrBuf={};$('feed').innerHTML='';$('meta').textContent=id.slice(0,8);poll();if(timer)clearInterval(timer);timer=setInterval(poll,2500)}
function addEv(cls,k,d,t){const div=document.createElement('div');div.className='ev '+cls;
  div.innerHTML=`<span class="t">${t||''}</span><span class="k">${esc(k)}</span><span class="d">${d}</span>`;
  $('feed').appendChild(div)}
function addNarr(actor,text){const div=document.createElement('div');div.className='narr';div.textContent=`💬 ${actor}：${text}`;$('feed').appendChild(div)}
function render(ev){
  const t=(ev.created_at||'').slice(11,19),d=ev.data||{},ty=ev.type;
  const m=ty.match(/^build\\.(.+)\\.model\\.text\\.delta$/);
  if(m){const a=m[1];narrBuf[a]=(narrBuf[a]||'')+(d.text||'');return}
  flushNarr();
  if(ty==='build.started')addEv('good','开始',esc((d.requirement||'').slice(0,80)),t);
  else if(ty==='tool.requested')addEv('','调用',esc(d.tool||''),t);
  else if(ty==='build.operation'){const bad=!!d.error;addEv(bad?'err':'','动作',esc(d.tool||'')+(bad?' ⛔ '+esc(String(d.error).slice(0,180)):' ✓'),t)}
  else if(ty==='build.turn.completed')addEv('','轮',`#${d.turn??''} ${esc(d.actor||'统筹')}`,t);
  else if(ty==='build.mechanical.phase')addEv('phase','阶段',esc(d.phase||'')+(d.cycle?` (第${d.cycle}轮)`:''),t);
  else if(ty==='build.mechanical.step')addEv(d.error?'err':'','提案',esc(d.actor||'')+' '+(d.done?'phase_done':esc(d.executed||''))+(d.error?' ⛔ '+esc(String(d.error).slice(0,160)):''),t);
  else if(ty==='build.published')addEv('good','发布','v'+esc(String(d.version??'')),t);
  else if(ty==='build.needs_attention'||ty==='build.deadline.exceeded')addEv('err',ty.replace('build.',''),esc(JSON.stringify(d).slice(0,160)),t);
  else if(ty==='build.teammate.spawned')addEv('phase','派工',esc(d.name||'')+' → '+esc(d.model||''),t);
  else addEv('',ty.replace('build.',''),esc(JSON.stringify(d).slice(0,140)),t);
}
function flushNarr(){for(const[a,txt]of Object.entries(narrBuf)){if(txt.trim())addNarr(a,txt.trim())}narrBuf={}}
async function poll(){
  if(!current)return;
  try{
    const s=await jf('/api/state?id='+current);
    const b=s.build||{};$('status').textContent=b.status||'?';$('status').className='badge '+(b.status||'');
    $('cancelBtn').style.display=(b.status==='building'||b.status==='queued')?'':'none';
    const ts=b.team_state||{};
    $('meta').textContent=`${current.slice(0,8)} · ${b.builder||''} · 统筹 ${ (ts.coordinator_model||'默认').split('/').pop() }`;
    for(const ev of (s.events||[])){if(seen.has(ev.id))continue;seen.add(ev.id);render(ev)}
    flushNarr();
    if(b.error&&!seen.has('final-err')){seen.add('final-err');addEv('err','结局',esc(String(b.error).slice(0,300)))}
    if(['published','ready','needs_attention','failed','cancelled'].includes(b.status)&&timer&&!b._done){clearInterval(timer);timer=null}
    $('feedwrap').scrollTop=$('feedwrap').scrollHeight;
  }catch(e){}
}
let mode='build',chatMsgs=[];
function modeChanged(){
  mode=$('mode').value;
  $('buildform').style.display=(mode==='build')?'':'none';
  if(mode==='chat'){
    if(timer){clearInterval(timer);timer=null}
    current=null;$('feed').innerHTML='';$('cancelBtn').style.display='none';
    $('status').textContent='对话';$('status').className='badge';$('meta').textContent='通用智能体形态';
    $('say').placeholder='跟它说话——需要搭工作流时它会自己动手';
    for(const m of chatMsgs){if(m.role==='user')addEv('say','你',esc(m.content));else if(m.role==='assistant'&&m.content)addNarr('它',m.content)}
    if(!chatMsgs.length)addEv('phase','提示','通用对话形态：随便聊。它手里有 start_build 工具，聊到要自动化的事它会自己发起搭建。');
  }else{
    $('feed').innerHTML='<div class="hint">左边发个需求，右边看它怎么干活。</div>';
    $('status').textContent='空闲';$('status').className='badge';$('meta').textContent='';
    $('say').placeholder='随时插话（运行中即刻送达；停下后回车带话续跑）';
  }
}
function renderChatAction(a,msg){
  if(a.tool==='start_build'&&a.build_id){
    addEv('good','动手了',`发起构建 ${esc(a.build_id.slice(0,8))}（${esc(a.builder||'classic')}）— <a style="color:var(--acc);cursor:pointer" onclick="watchBuild('${a.build_id}')">切到任务视图跟进</a>`);
    remember(a.build_id,a.requirement||msg,a.builder||'classic');
  }else{
    let line=esc(a.tool||'')+(a.detail?' · '+esc(a.detail):'')+(a.error?' ⛔ '+esc(String(a.error).slice(0,140)):' ✓');
    if(a.output)line+=' <span class="hint">'+esc(String(a.output).slice(0,140))+'</span>';
    addEv(a.error?'err':'','工具',line);
  }
}
async function sendChat(msg){
  chatMsgs.push({role:'user',content:msg});addEv('say','你',esc(msg));
  const wait=document.createElement('div');wait.className='hint';wait.textContent='…思考中';$('feed').appendChild(wait);
  $('feedwrap').scrollTop=$('feedwrap').scrollHeight;
  let job=null;
  try{job=(await jf('/api/chat',{method:'POST',body:JSON.stringify({model:$('coord').value,messages:chatMsgs})})).job_id}
  catch(e){wait.remove();addEv('err','对话失败',esc(String(e.message||e)));return}
  let rendered=0,quiet=0;
  const it=setInterval(async()=>{
    let s;try{s=await jf('/api/chat_result?job='+job);quiet=0}catch(e){if(++quiet>10){clearInterval(it);wait.remove();addEv('err','对话失败','进度查询连续失败')}return}
    const acts=s.actions||[];
    for(;rendered<acts.length;rendered++){$('feed').insertBefore($('feed').lastChild,null);renderChatAction(acts[rendered],msg);$('feed').appendChild(wait)}
    wait.textContent=acts.length?`…干活中（已 ${acts.length} 步）`:'…思考中';
    if(s.status!=='running'){
      clearInterval(it);wait.remove();
      if(s.status==='error')addEv('err','对话失败',esc(String(s.error)));
      else if(s.reply){chatMsgs.push({role:'assistant',content:s.reply});addNarr('它',s.reply)}
    }
    $('feedwrap').scrollTop=$('feedwrap').scrollHeight;
  },2000);
}
function watchBuild(id){$('mode').value='build';modeChanged();load(id)}
async function say(){
  const msg=$('say').value.trim();if(!msg)return;$('say').value='';
  if(mode==='chat'){sendChat(msg);return}
  if(!current)return;
  addEv('say','你',esc(msg));
  try{const d=await jf('/api/say',{method:'POST',body:JSON.stringify({id:current,message:msg})});
    if(d.resumed){addEv('phase','续跑','带着你的话重新开工');if(!timer)timer=setInterval(poll,2500)}
  }catch(e){addEv('err','插话失败',esc(String(e.message||e)))}
}
async function cancelBuild(){if(!current)return;try{await jf('/api/cancel',{method:'POST',body:JSON.stringify({id:current})})}catch(e){}}
modeChanged();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 安静
        pass

    def _send(self, code: int, payload, content_type="application/json"):
        body = payload.encode() if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        try:
            if self.path == "/" or self.path.startswith("/index"):
                self._send(200, PAGE, "text/html")
            elif self.path == "/api/chats":
                chats = sorted(chat_log_read(), key=lambda x: x.get("ts") or "", reverse=True)
                self._send(200, {"chats": [
                    {"job": c["job"], "ts": (c.get("ts") or "")[11:16],
                     "status": c.get("status"), "model": (c.get("model") or "").split("/")[-1],
                     "user": (c.get("user") or "")[:40]} for c in chats[:15]]})
            elif self.path.startswith("/api/chat_log"):
                job_id = self.path.split("job=", 1)[1].split("&")[0]
                found = [c for c in chat_log_read() if c["job"] == job_id]
                self._send(200 if found else 404,
                           found[0] if found else {"error": "not found"})
            elif self.path == "/api/recent":
                db = REPO_ROOT / "data" / "agent_platform.db"
                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "select id, status, builder, requirement, created_at "
                    "from builds order by created_at desc limit 15").fetchall()
                conn.close()
                self._send(200, {"builds": [
                    {"id": r["id"], "status": r["status"], "builder": r["builder"],
                     "req": (r["requirement"] or "")[:40],
                     "at": (r["created_at"] or "")[11:16]} for r in rows]})
            elif self.path == "/api/health":
                result = {"platform": probe(f"{API_BASE}/health")}
                for name, url in VLLM_ENDPOINTS.items():
                    result[name] = probe(url)
                self._send(200, result)
            elif self.path.startswith("/api/chat_result"):
                job_id = self.path.split("job=", 1)[1].split("&")[0]
                job = CHAT_JOBS.get(job_id)
                if job is None:
                    self._send(404, {"error": "job not found"})
                else:
                    self._send(200, {"status": job["status"], "reply": job["reply"],
                                     "error": job["error"], "actions": list(job["actions"])})
            elif self.path.startswith("/api/state"):
                build_id = self.path.split("id=", 1)[1].split("&")[0]
                build = api("GET", f"/api/v1/builds/{build_id}")
                events = api("GET", f"/v1/streams/{build_id}")
                self._send(200, {"build": build, "events": events[-400:]})
            else:
                self._send(404, {"error": "not found"})
        except urllib.error.HTTPError as error:
            self._send(error.code, {"error": friendly_http_error(error)})
        except Exception as error:  # noqa: BLE001
            self._send(500, {"error": str(error)[:400]})

    def do_POST(self):
        try:
            data = self._body()
            if self.path == "/api/start":
                requirement = str(data.get("requirement") or "").strip()
                if len(requirement) < 10:
                    self._send(400, {"error": (
                        "需求至少要 10 个字——这一栏不是聊天框，是给它的任务书："
                        "要输入什么、输出什么，最好带个样例。想跟它聊天用下面的插话栏。"
                    )})
                    return
                app = api("POST", "/api/v1/applications", {
                    "name": f"试炼-{time.strftime('%m%d-%H%M%S')}",
                    "requirement": requirement,
                })
                build_body = {
                    "requirement": requirement,
                    "builder": data.get("builder") or "classic",
                    "auto_publish": bool(data.get("auto_publish", True)),
                    "max_turns": int(data.get("max_turns") or 24),
                    "max_repair_cycles": int(data.get("max_repair_cycles") or 2),
                    "max_elapsed_seconds": float(data.get("max_elapsed_seconds") or 1800),
                }
                if data.get("coordinator_model"):
                    build_body["coordinator_model"] = data["coordinator_model"]
                if data.get("teammate_models"):
                    build_body["teammate_models"] = data["teammate_models"]
                build = api("POST", f"/api/v1/applications/{app['id']}/builds", build_body)
                self._send(200, {"build_id": build["build_id"], "application_id": app["id"]})
            elif self.path == "/api/say":
                build_id, message = data["id"], data["message"]
                try:
                    api("POST", f"/api/v1/builds/{build_id}/messages", {"message": message})
                    self._send(200, {"delivered": True})
                except urllib.error.HTTPError as error:
                    if error.code == 409:  # 不在运行 → 带话续跑
                        api("POST", f"/api/v1/builds/{build_id}/resume", {"message": message})
                        self._send(200, {"resumed": True})
                    else:
                        raise
            elif self.path == "/api/chat":
                job_id = uuid.uuid4().hex[:12]
                job = {"status": "running", "reply": "", "error": "", "actions": []}
                CHAT_JOBS[job_id] = job
                model = str(data.get("model") or "local/Qwen/Qwen3-4B-Instruct-2507")
                history = list(data.get("messages") or [])
                last_user = next((m.get("content", "") for m in reversed(history)
                                  if m.get("role") == "user"), "")
                chat_log({"kind": "start", "job": job_id, "model": model,
                          "user": str(last_user)[:500]})

                def work():
                    try:
                        result = run_chat_agent(model, history, actions_out=job["actions"])
                        job["reply"] = result.get("reply") or ""
                        job["status"] = "done"
                    except Exception as error:  # noqa: BLE001
                        job["error"] = str(error)[:300]
                        job["status"] = "error"
                    chat_log({"kind": "end", "job": job_id, "status": job["status"],
                              "reply": job["reply"][:4000], "error": job["error"],
                              "actions": job["actions"][-40:]})

                threading.Thread(target=work, daemon=True).start()
                self._send(200, {"job_id": job_id})
            elif self.path == "/api/cancel":
                api("POST", f"/api/v1/builds/{data['id']}/cancel", {})
                self._send(200, {"cancelled": True})
            else:
                self._send(404, {"error": "not found"})
        except urllib.error.HTTPError as error:
            self._send(error.code, {"error": friendly_http_error(error)})
        except Exception as error:  # noqa: BLE001
            self._send(500, {"error": str(error)[:400]})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Builder 试炼场: http://127.0.0.1:{PORT}  (API: {API_BASE})")
    server.serve_forever()
