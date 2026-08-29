"""模板匹配得看得懂中文需求——这个仓的需求全是中文。

template_strategy 173 行，被 builder（搭建时选模板）和 api 用着，此前零测试。

原来的分词是 `re.split(r"[^0-9A-Za-z_]+", …)`：**中文字符全被当成分隔符**。
一句纯中文切完什么都不剩，掉进"按空格切"那条兜底，
中文没有空格，于是**整句变成一个词**。量出来的后果：

  · 无 tags 的中文模板，只有需求**一字不差等于模板名**时才配得上。
    名字叫「词频统计」、描述写着"统计文本里每个词出现的次数"的模板，
    遇到需求"统计文本中每个词出现次数"——一个都配不上。
  · 有 tags 的能配上，但只有 tags 这一条路在出力，name / description
    两条完全不起作用，分数系统性偏低（同等贴切：中文 0.63、英文 1.557）。

改成中文走二字滑窗。中文里有信息量的词绝大多数是两个字，
滑窗必然覆盖到；噪声词配不上模板文本、不加分。没有引入新依赖。

**代价说清楚**：中英混排的需求里词数变多，而 text_matches 那一项是
除以词数的，所以绝对分会略降（实测 1.225 → 1.15）。
同一次查询里所有模板的除数相同，**排序不受影响**；
受影响的只有 0.1 那条入选线上下的边缘模板。
"""

from __future__ import annotations

import pytest

from agent_platform.template_models import TemplateMeta
from agent_platform.template_strategy import _query_terms, score_template_matches


def _meta(**extra) -> TemplateMeta:
    fields = {"name": "词频统计", "title": "词频统计模板",
              "description": "统计文本里每个词出现的次数并过滤低频词",
              "category": "data_analysis", "tags": []}
    fields.update(extra)
    return TemplateMeta(**fields)


class TestChineseIsSplitIntoWords:
    def test_a_chinese_sentence_is_not_one_giant_term(self):
        """这是整件事的根：整句当一个词，就等于没分词。"""
        terms = _query_terms("统计文本中每个词出现次数")
        assert len(terms) > 1, terms
        assert all(len(t) <= 2 for t in terms), terms
        assert "统计" in terms and "文本" in terms

    def test_latin_words_still_come_out_whole(self):
        """英文那条路不能被带坏。"""
        assert _query_terms("analyze csv data") == ["analyze", "csv", "data"]

    def test_a_mixed_requirement_keeps_both(self):
        terms = _query_terms("把 csv 数据分析成 report")
        assert "csv" in terms and "report" in terms
        assert "数据" in terms and "分析" in terms

    def test_single_characters_are_dropped(self):
        """「把」「的」这种夹在中间的单字命中一切，只贡献噪声。

        实现上不是靠一句 `if len(run) < 2` 挡的——滑窗本身就出不了单字。
        （原来写了那一句，变异验证显示它是等价变异，删掉了。）
        这条断言钉的是行为，不是那一句。
        """
        assert "把" not in _query_terms("把 csv 数据分析成 report")
        assert "的" not in _query_terms("统计 的 次数")

    def test_terms_are_deduplicated(self):
        """滑窗会重复出词；重复只会把 text_matches 的分母抬高。"""
        terms = _query_terms("统计统计统计")
        assert len(terms) == len(set(terms)), terms


class TestAChineseTemplateCanBeFoundByItsNameAndDescription:
    def test_a_tagless_chinese_template_matches_a_real_requirement(self):
        """改动前这一条配不上——名字和描述都高度相关也没用。"""
        scored = score_template_matches("统计文本中每个词出现次数，只要两次以上的",
                                        [_meta()])
        assert scored, "无 tags 的中文模板一个都没配上"
        assert scored[0][1].name == "词频统计"

    def test_an_unrelated_requirement_still_matches_nothing(self):
        """反向那一条。少了它，"什么都配上"能让上面全绿。"""
        assert score_template_matches("给客服工单分类并路由到不同小组",
                                      [_meta()]) == []

    def test_the_more_relevant_chinese_template_ranks_first(self):
        """排序才是这个函数真正要交付的东西。"""
        word_freq = _meta()
        daily = _meta(name="每日报表", title="每日汇总报表",
                      description="按天汇总数据并生成日报")
        scored = score_template_matches("统计文本里每个词出现的次数",
                                        [daily, word_freq])
        assert scored[0][1].name == "词频统计", scored


class TestEnglishBehaviourIsUnchanged:
    """这次改动只在有中文时才多出词——英文那边的分数要一模一样。"""

    @pytest.mark.parametrize("requirement, expected", [
        ("count how often each word appears", 0.367),
        ("analyze csv data and produce a report", 1.557),
    ])
    def test_the_score_is_exactly_what_it_was(self, requirement, expected):
        metas = [
            TemplateMeta(name="word_frequency", title="Word Frequency",
                         description="count how often each word appears in text",
                         category="data_analysis", tags=[]),
            TemplateMeta(name="csv_report", title="CSV Report",
                         description="analyze csv data and produce a report",
                         category="data_analysis", tags=["csv", "report", "data"]),
        ]
        scored = score_template_matches(requirement, metas)
        assert scored, requirement
        assert scored[0][0] == pytest.approx(expected, abs=0.002), scored

    def test_an_empty_requirement_matches_nothing(self):
        assert score_template_matches("", [_meta()]) == []
        assert score_template_matches("   ", [_meta()]) == []
