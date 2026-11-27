"""issue #439: CJK tokenization — Chinese requirement vs Chinese capability words no longer always score 0.

CJK runs are split into character bigrams (single chars kept), Latin words unchanged; the scoring contract stays deterministic zero-LLM.
"""

from __future__ import annotations

from fusion_cowork.nodes import import_all_nodes
from fusion_cowork.nodes.ops.match_node import HistoricalMatchNode, _tokenize, score_overlap

import_all_nodes()


class TestTokenize:
    def test_cjk_run_becomes_bigrams(self):
        tokens = _tokenize("库存管理")  # i18n: user-facing
        # 管理 is a stopword and gets filtered — same rule on both sides, symmetric scoring
        assert {"库存", "存管"} <= tokens  # i18n: user-facing

    def test_single_cjk_char_kept(self):
        assert _tokenize("账") == {"账"}  # i18n: user-facing

    def test_latin_unchanged(self):
        assert _tokenize("Pet Hotel Booking") == {"pet", "hotel", "booking"}

    def test_mixed_en_cn(self):
        tokens = _tokenize("需要库存管理 and payments")  # i18n: user-facing
        assert "库存" in tokens  # i18n: user-facing
        assert "payments" in tokens

    def test_stopword_bigram_filtered_both_sides(self):
        # The stopword bigram is filtered — same rule on both sides, symmetric scoring
        assert "管理" not in _tokenize("库存管理")  # i18n: user-facing


class TestScoring:
    def test_zh_requirement_vs_zh_caps_full(self):
        score = score_overlap("需要库存管理和订单追踪", ["库存管理", "订单追踪"])  # i18n: user-facing
        assert score >= 0.6

    def test_zh_exact_subset_reaches_one(self):
        score = score_overlap("库存管理和订单追踪", ["库存管理", "订单追踪"])  # i18n: user-facing
        assert score == 1.0

    def test_zh_partial_overlap(self):
        full = score_overlap("需要库存管理和订单追踪", ["库存管理", "订单追踪"])  # i18n: user-facing
        partial = score_overlap("需要库存管理", ["库存管理", "订单追踪"])  # i18n: user-facing
        assert 0.0 < partial < full

    def test_zh_no_overlap_scores_zero(self):
        assert score_overlap("需要量子等离子推进控制台", ["库存管理", "订单追踪"]) == 0.0  # i18n: user-facing

    def test_mixed_en_cn_sane(self):
        score = score_overlap("需要库存管理 payments and notifications", ["库存管理", "payments"])  # i18n: user-facing
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
            "requirement": "需要库存管理和订单追踪",  # i18n: user-facing
            "candidates": [
                {
                    "slug": "wms",
                    "repo": "org/wms",
                    "capabilities": ["库存管理", "订单追踪"],  # i18n: user-facing
                    "customer": "",
                }  # i18n: user-facing
            ],
        }
        res = await node.execute({})
        assert res.data["verdict"] == "extend"
        assert res.data["score"] >= 0.6

    async def test_zh_no_match_new(self):
        node = HistoricalMatchNode()
        node.config.params = {
            "requirement": "需要量子等离子推进控制台",  # i18n: user-facing
            "candidates": [
                {
                    "slug": "wms",
                    "repo": "org/wms",
                    "capabilities": ["库存管理", "订单追踪"],  # i18n: user-facing
                    "customer": "",
                }  # i18n: user-facing
            ],
        }
        res = await node.execute({})
        assert res.data["verdict"] == "new"
        assert res.data["gaps"] == []
