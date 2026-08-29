"""客户看到的报错要是人话，不是内部报文。

真机实测（2026-08-29）：使用者通道回的是

    {"status": "failed",
     "error": "node start failed: missing required input: text"}

而前端 use/[id]/page.tsx 就是 `没有跑成：{run.error}` 直出。
使用者屏幕上于是是一句带节点 id 的英文——对他没有任何意义，
他也无从知道自己少填了什么。

平台早有 _human_error 把它翻成「缺少必填输入「text」」，
管家答问、面板、体检都在用，唯独**客户这条路**没用。
又是同一个闸没装满出口，而这次漏的是最外面那个。

脱敏那一层一直都在，顺序也不能反：先脱敏、再翻译，
否则模板可能把一段该藏的原文重新拼进人话里。
"""
from __future__ import annotations

from agent_platform.customer_runtime_projection import (_HIDDEN_RUNTIME_ERROR,
                                                        project_runtime_run)


def _run(error: str) -> dict:
    return project_runtime_run({"id": "r1", "status": "failed",
                                "error": error, "outputs": {}, "state": {}})


def test_a_missing_input_is_explained_in_chinese():
    got = _run("node start failed: missing required input: text")["error"]
    assert "缺少必填输入" in got, got
    assert "text" in got, "得说清是**哪个**输入，不然他不知道补什么"


def test_the_internal_node_id_does_not_reach_the_customer():
    got = _run("node start failed: missing required input: text")["error"]
    assert "node start" not in got, got
    assert "failed" not in got, got


def test_an_unrecognised_error_is_passed_through_not_mangled():
    """认不出来就原样给——硬套模板会把节点自己抛的中文说明盖掉。"""
    got = _run("库存不足，无法出库")["error"]
    assert got == "库存不足，无法出库"


def test_redaction_still_wins_over_translation():
    """该藏的还得藏。翻译排在脱敏之后，不能把它绕开。"""
    got = _run("traceback: File \"x.py\", line 3")["error"]
    assert got == _HIDDEN_RUNTIME_ERROR


def test_a_credential_in_the_error_never_reaches_the_customer():
    """原来的黑名单防的全是"内部思考漏出去"，**一条凭据类的都没有**。

    实测 "DEEPSEEK_API_KEY invalid" 原样送到了客户眼前。
    工作流节点会去调外部接口，而那些接口的报错里最常见的
    就是带凭据的 URL 和「api key 无效」这类原文。
    """
    for leaky in ("DEEPSEEK_API_KEY invalid",
                  "401 from https://api.example.com/x?token=abc123",
                  "Authorization: Bearer sk-live-9f3a",
                  "invalid api key: sk-abcdef",
                  "password authentication failed for user lilies",
                  "AWS_SECRET_ACCESS_KEY is not set"):
        got = _run(leaky)["error"]
        assert got == _HIDDEN_RUNTIME_ERROR, f"{leaky} → {got}"


def test_an_ordinary_token_limit_error_is_not_hidden():
    """「token 超限」对客户是有用的话，别跟凭据一起藏掉——
    黑名单只列复合词，不列光秃秃的 token。
    """
    got = _run("输出超长：token 超过上限")["error"]
    assert got != _HIDDEN_RUNTIME_ERROR
    assert "token" in got


def test_the_hidden_message_is_chinese_and_says_what_to_do():
    """这句是给**客户**看的。原文是
    "Runtime failed; private diagnostic details were hidden."——
    他不懂英文，也不知道下一步该干什么。
    """
    import re

    assert re.search(r"[一-鿿]", _HIDDEN_RUNTIME_ERROR)
    assert "联系" in _HIDDEN_RUNTIME_ERROR


def test_no_error_means_no_field():
    projected = project_runtime_run({"id": "r1", "status": "succeeded",
                                     "error": "", "outputs": {}, "state": {}})
    assert "error" not in projected


def test_a_very_long_error_is_still_capped():
    got = _run("x" * 9000)["error"]
    assert len(got) <= 2_000


# ── 客户能看到的一切都要过 project_public_value 这道筛 ──
#
# 变异验证发现的两个空档（2026-08-29）：
#   · 把「私有键过滤」整个删掉 → 25 条用例全绿
#   · 把递归改成只处理顶层     → 25 条用例全绿
# 也就是说这道筛只有"字符串里的凭据打码"那一半有人盯着，
# 另外两半坏了没人知道。而它是客户面所有数据的最后一关。

from agent_platform.customer_runtime_projection import (  # noqa: E402
    _PRIVATE_KEYS, project_public_value)


def test_private_keys_are_dropped():
    """清单里的键一个都不能留在客户那份数据里。"""
    payload = {name: "机密" for name in sorted(_PRIVATE_KEYS)}
    payload["line_count"] = 3          # 正常业务字段作对照
    projected = project_public_value(payload)
    leaked = sorted(set(projected) & _PRIVATE_KEYS)
    assert leaked == [], f"这些私有键漏给客户了：{leaked}"
    assert projected["line_count"] == 3, "把正常字段也筛掉了就矫枉过正了"


def test_the_marker_family_is_dropped_even_with_a_prefix():
    """`_is_private_key` 除了清单还认几个词根——带前缀的变体也要挡住。"""
    payload = {"builder_thinking": "x", "node_chain_of_thought": "y",
               "raw_blocks_v2": "z", "private_reason_note": "w"}
    assert project_public_value(payload) == {}


def test_it_filters_all_the_way_down():
    """嵌套里的私有键也要筛掉——只处理顶层等于没筛。

    真实载荷本来就是嵌套的（state.snapshot、outputs 里套 dict 套 list），
    秘密恰恰最容易藏在第二层。
    """
    payload = {"结果": {"明细": [{"line_count": 3, "api_key": "sk-xxxx"}]}}
    projected = project_public_value(payload)
    inner = projected["结果"]["明细"][0]
    assert inner == {"line_count": 3}, inner


def test_a_credential_deep_in_a_string_is_masked_too():
    """字符串打码那一半也要能穿透嵌套。"""
    payload = {"日志": {"行": ["Authorization: Bearer sk-live-9f3a"]}}
    line = project_public_value(payload)["日志"]["行"][0]
    assert "sk-live-9f3a" not in line


def test_a_very_deep_structure_does_not_hang_or_crash():
    """深度有上限（20 层）——递归保护别在补测试时被顺手拆掉。"""
    deep = current = {}
    for _ in range(40):
        current["下一层"] = {}
        current = current["下一层"]
    current["api_key"] = "sk-xxxx"
    assert project_public_value(deep) is not None
