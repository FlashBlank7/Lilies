"""模板块：取不到值、写了不支持的语法——话得说得能照着改。

真机量出来的两桩（2026-08-30 查 227 次失败时撞上的）：

1. **6 次失败，错误正文是一个字符：`'g'`。**
   模板写着 `{% for g in gpus %}…{{ g.index }}`，而这个渲染器
   只做 `{{ 变量 }}` 替换，`{% %}` 原样留着、`g` 从来没被定义，
   于是 `variables["g"]` 抛 KeyError——KeyError 的 str() 就是键名。
   业主看到「node render_report failed: 'g'」无从下手；
   更要紧的是**修工作流的是模型**，它读到 'g' 一样无从下手。

2. **1 次「成功」的运行，交付正文里印着 `{% for gpu in gpus %}`。**
   变量恰好都取得到时，控制标签就被原样留在报告里——
   平台照样打 ✓。业主拿到的是一份印着模板源码的 GPU 日报。
   **这比报错更糟：报错至少有人知道。**

已发布的活工作流里带 `{%` 的有 0 个（查过全库），所以改成拒绝
不会弄坏任何在跑的东西。照本文件 4941 行定下的调子：拒绝即教学。

渲染器此前零测试——`grep -l _render tests/` 一个文件都没有。
"""

from __future__ import annotations

import pytest

from agent_platform.workflow_runtime import WorkflowRuntime


def _render(template: str, variables: dict) -> str:
    return WorkflowRuntime._render(template, variables)


class TestOrdinaryRenderingStillWorks:
    """反向那一批放最前面：下面全是"要拒绝"，少了这些"一律拒"也能全绿。"""

    def test_a_plain_variable_is_substituted(self):
        assert _render("你好 {{ name }}", {"name": "莉莉丝"}) == "你好 莉莉丝"

    def test_a_dotted_path_reaches_into_a_dict(self):
        assert _render("{{ gpu.name }}", {"gpu": {"name": "RTX6000Ada"}}) == "RTX6000Ada"

    def test_spaces_inside_the_braces_are_tolerated(self):
        assert _render("{{name}} {{  name  }}", {"name": "x"}) == "x x"

    def test_a_number_becomes_text(self):
        assert _render("{{ n }}", {"n": 42}) == "42"

    def test_text_without_any_variable_passes_through(self):
        assert _render("就是一句话", {}) == "就是一句话"


class TestAMissingVariableSaysWhichOne:
    def test_it_no_longer_fails_with_just_the_key_name(self):
        """真机上这里抛的是 KeyError('g')，报出去就是一个字 'g'。"""
        with pytest.raises(Exception) as caught:
            _render("{{ g.index }}", {"gpus": []})
        assert str(caught.value).strip() not in ("g", "'g'"), "又退回成光秃秃的键名了"

    def test_the_missing_name_is_named(self):
        with pytest.raises(Exception, match="g"):
            _render("{{ g.index }}", {"gpus": []})

    def test_it_lists_what_you_could_have_used(self):
        """只说"取不到"没法改——得告诉人手里有什么。"""
        with pytest.raises(Exception, match="gpus"):
            _render("{{ g.index }}", {"gpus": [], "generated_at": "x"})

    def test_a_missing_leaf_under_a_real_object_is_also_named(self):
        with pytest.raises(Exception, match="temperature"):
            _render("{{ gpu.temperature }}", {"gpu": {"name": "A"}})

    def test_walking_into_a_string_is_a_clear_error_not_a_typeerror(self):
        """`{{ a.b }}` 而 a 是字符串——原来是 TypeError，同样没法读。"""
        with pytest.raises(Exception) as caught:
            _render("{{ a.b }}", {"a": "文本"})
        assert "a.b" in str(caught.value) or "b" in str(caught.value)


class TestControlSyntaxIsRefusedInsteadOfPrinted:
    @pytest.mark.parametrize("template", [
        "{% for g in gpus %}{{ g.id }}{% endfor %}",
        "{% if x %}有{% endif %}",
        "{% set high = [] %}",
    ])
    def test_it_is_refused(self, template):
        with pytest.raises(Exception, match=r"\{%|控制"):
            _render(template, {"gpus": [], "x": 1, "high": []})

    def test_the_message_says_what_to_do_instead(self):
        """拒绝即教学：模型要照着这句话把工作流改对。"""
        with pytest.raises(Exception) as caught:
            _render("{% for g in gpus %}{{ g.id }}{% endfor %}", {"gpus": []})
        assert "数据处理" in str(caught.value) or "上一步" in str(caught.value)

    def test_the_silent_garbage_path_is_gone(self):
        """变量都取得到时，原来会把 {% %} 原样印进交付正文并判成功。

        真机上就发生过一次：业主拿到一份印着 `{% for gpu in gpus %}`
        的 GPU 日报，平台打的是 ✓。
        """
        with pytest.raises(Exception):
            _render("头{% for gpu in gpus %}{{ name }}{% endfor %}尾",
                    {"gpus": [], "name": "x"})

    def test_a_lone_percent_sign_is_not_control_syntax(self):
        """别宽到"带 % 就拒"——利用率 100% 是正常正文。"""
        assert _render("利用率 {{ pct }}%", {"pct": 100}) == "利用率 100%"
