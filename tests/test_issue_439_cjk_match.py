"""issue #439: CJK 分词 — 中文需求 vs 中文能力词打分不再恒 0。

CJK 连续串按字符 bigram 切分 (单字保留), 拉丁词不变; 打分契约仍是确定性零 LLM。
"""

from __future__ import annotations

from fusion_cowork.nodes import import_all_nodes
from fusion_cowork.nodes.ops.match_node import HistoricalMatchNode, _tokenize, score_overlap

import_all_nodes()


class TestTokenize:
    def test_cjk_run_becomes_bigrams(self):
        tokens = _tokenize("库存管理")
        # 管理 是停用词被过滤 — 两侧同规则, 打分对称
        assert {"库存", "存管"} <= tokens

    def test_single_cjk_char_kept(self):
        assert _tokenize("账") == {"账"}

    def test_latin_unchanged(self):
        assert _tokenize("Pet Hotel Booking") == {"pet", "hotel", "booking"}

    def test_mixed_en_cn(self):
        tokens = _tokenize("需要库存管理 and payments")
        assert "库存" in tokens
        assert "payments" in tokens

    def test_stopword_bigram_filtered_both_sides(self):
        # 管理 是停用词 — 两侧同规则过滤, 打分对称
        assert "管理" not in _tokenize("库存管理")


class TestScoring:
    def test_zh_requirement_vs_zh_caps_full(self):
        score = score_overlap("需要库存管理和订单追踪", ["库存管理", "订单追踪"])
        assert score >= 0.6

    def test_zh_exact_subset_reaches_one(self):
        score = score_overlap("库存管理和订单追踪", ["库存管理", "订单追踪"])
        assert score == 1.0

    def test_zh_partial_overlap(self):
        full = score_overlap("需要库存管理和订单追踪", ["库存管理", "订单追踪"])
        partial = score_overlap("需要库存管理", ["库存管理", "订单追踪"])
        assert 0.0 < partial < full

    def test_zh_no_overlap_scores_zero(self):
        assert score_overlap("需要量子等离子推进控制台", ["库存管理", "订单追踪"]) == 0.0

    def test_mixed_en_cn_sane(self):
        score = score_overlap("需要库存管理 payments and notifications", ["库存管理", "payments"])
        assert score >= 0.6

    def test_english_regression(self):
        score = score_overlap(
            "pet hotel booking with payments and notifications",
            ["pet hotel", "booking", "payments"],
        )
        assert score >= 0.6


class TestVerdict:
    async def test_zh_extend_own(self):
        node = HistoricalMatchNode()
        node.config.params = {
            "requirement": "需要库存管理和订单追踪",
            "candidates": [
                {"slug": "wms", "repo": "org/wms", "capabilities": ["库存管理", "订单追踪"], "customer": ""}
            ],
        }
        res = await node.execute({})
        assert res.data["verdict"] == "extend"
        assert res.data["score"] >= 0.6

    async def test_zh_no_match_new(self):
        node = HistoricalMatchNode()
        node.config.params = {
            "requirement": "需要量子等离子推进控制台",
            "candidates": [
                {"slug": "wms", "repo": "org/wms", "capabilities": ["库存管理", "订单追踪"], "customer": ""}
            ],
        }
        res = await node.execute({})
        assert res.data["verdict"] == "new"
        assert res.data["gaps"] == []
