"""上游 5 档评级 → PanWatch 3 档 action 映射回归测试。

根因 bug:上游 PM 用 Buy/Overweight/Hold/Underweight/Sell 五档,我们只识别 3 档,
Overweight/Underweight 被兜底到 hold,导致顶层显示"持有"但 PM 实际给出"减持"。
"""

from __future__ import annotations

from types import SimpleNamespace

from src.agents.tradingagents.result_mapper import (
    RATING_ACTION_MAP,
    RATING_LABEL_MAP,
    _parse_rating_from_text,
    map_state_to_result,
)


def _stock():
    return SimpleNamespace(symbol="601127", name="赛力斯", market=SimpleNamespace(value="CN"))


def _result(decision_raw: str, final_decision_text: str = "") -> dict:
    """构造一份 ta_result 给 map_state_to_result 用"""
    return {
        "decision": decision_raw,
        "final_state": {
            "final_trade_decision": final_decision_text,
            "trader_investment_plan": "Action: Sell\n\nReasoning: 风险大",
        },
        "cost_usd": 0.05,
    }


# ============================================================
# 5 档评级 → 3 档 action + 中文标签
# ============================================================

def test_buy_rating_maps_to_buy():
    r = map_state_to_result(stock=_stock(), ta_result=_result("Buy"))
    assert r.raw_data["suggestion"]["action"] == "buy"
    assert r.raw_data["suggestion"]["action_label"] == "买入"


def test_overweight_rating_maps_to_buy_with_zh_label():
    """Overweight(增持) → action=buy,但 label 显示"增持"区分于 buy"""
    r = map_state_to_result(stock=_stock(), ta_result=_result("Overweight"))
    assert r.raw_data["suggestion"]["action"] == "buy"
    assert r.raw_data["suggestion"]["action_label"] == "增持"
    assert r.raw_data["suggestion"]["rating_raw"] == "overweight"


def test_hold_rating_maps_to_hold():
    r = map_state_to_result(stock=_stock(), ta_result=_result("Hold"))
    assert r.raw_data["suggestion"]["action"] == "hold"
    assert r.raw_data["suggestion"]["action_label"] == "持有"


def test_underweight_rating_maps_to_sell_with_zh_label():
    """关键 bug 回归:Underweight(减持) 之前被错误兜底到 hold,
    现在应该 action=sell + label=减持"""
    r = map_state_to_result(stock=_stock(), ta_result=_result("Underweight"))
    assert r.raw_data["suggestion"]["action"] == "sell"
    assert r.raw_data["suggestion"]["action_label"] == "减持"
    assert r.raw_data["suggestion"]["rating_raw"] == "underweight"
    # 应触发提醒(不是 hold)
    assert r.raw_data["suggestion"]["should_alert"] is True


def test_sell_rating_maps_to_sell():
    r = map_state_to_result(stock=_stock(), ta_result=_result("Sell"))
    assert r.raw_data["suggestion"]["action"] == "sell"
    assert r.raw_data["suggestion"]["action_label"] == "卖出"


# ============================================================
# Fallback:propagate() 没返回 5 档,从文本里抽
# ============================================================

def test_decision_text_with_rating_label():
    """final_trade_decision 含 'Rating: Underweight' → 解析出 underweight"""
    text = "After thorough analysis...\n\n**Rating**: Underweight\n\nReason: high leverage"
    assert _parse_rating_from_text(text) == "underweight"


def test_decision_text_chinese_label():
    """中文'评级:减持' → underweight"""
    text = "综合考虑:**评级:减持**,建议降低仓位"
    assert _parse_rating_from_text(text) == "underweight"


def test_decision_text_final_transaction_proposal():
    """'FINAL TRANSACTION PROPOSAL: SELL' → sell"""
    text = "...\n\nFINAL TRANSACTION PROPOSAL: **SELL**"
    assert _parse_rating_from_text(text) == "sell"


def test_decision_text_fallback_to_keyword_scan():
    """没显式标签也能从文本里找到 5 档词"""
    text = "Based on macro headwinds, recommend Overweight position in defensive sectors."
    assert _parse_rating_from_text(text) == "overweight"


def test_decision_empty_falls_back_to_hold():
    """propagate 返回空 + 文本也没评级词 → 默认 hold"""
    r = map_state_to_result(stock=_stock(), ta_result=_result("", final_decision_text=""))
    assert r.raw_data["suggestion"]["action"] == "hold"
    assert r.raw_data["suggestion"]["rating_raw"] == "hold"


def test_decision_unrecognized_then_text_has_underweight():
    """propagate 返回 'xxxx' 不识别 → 从 final_decision 文本抽 underweight"""
    r = map_state_to_result(
        stock=_stock(),
        ta_result=_result("xxxx", final_decision_text="...\n**Rating**: Underweight\n..."),
    )
    assert r.raw_data["suggestion"]["action"] == "sell"
    assert r.raw_data["suggestion"]["action_label"] == "减持"


# ============================================================
# Markdown 渲染:5 档评级标签写进 markdown 头部
# ============================================================

def test_markdown_shows_5_tier_rating_in_header():
    """Markdown 顶部应显示原始 5 档评级,避免"建议卖出但顶部写持有"的歧义"""
    r = map_state_to_result(
        stock=_stock(),
        ta_result=_result("Underweight", final_decision_text="Rating: Underweight\n\nReason: ..."),
    )
    assert "减持" in r.content
    # 既要有 action_label,也要有 rating note
    assert r.content.count("减持") >= 1


# ============================================================
# raw_data 里同时保留 3 档(decision) + 5 档(rating)
# ============================================================

def test_raw_data_has_both_decision_and_rating():
    """前端兼容:既要有 3 档 decision 给老代码,也要有 5 档 rating 给新展示"""
    r = map_state_to_result(stock=_stock(), ta_result=_result("Overweight"))
    assert r.raw_data["decision"] == "buy"  # 3 档
    assert r.raw_data["rating"] == "overweight"  # 5 档


# ============================================================
# 静态 mapping 完整性
# ============================================================

def test_all_5_ratings_have_label():
    for r in ("buy", "overweight", "hold", "underweight", "sell"):
        assert r in RATING_LABEL_MAP
        assert r in RATING_ACTION_MAP


def test_action_map_only_uses_3_actions():
    """3 档 action 只能是 buy/hold/sell(前端类型)"""
    assert set(RATING_ACTION_MAP.values()) == {"buy", "hold", "sell"}


# ============================================================
# Markdown 完整性:9 个 Agent 的产出都体现
# ============================================================

def _full_state():
    return {
        "final_trade_decision": "**Rating: Underweight** 详细决策书...",
        "trader_investment_plan": "Action: Sell\n建议减仓 70%",
        "risk_judge_decision": "风控辩论:激进/保守/中立讨论后,建议谨慎",
        "investment_debate_state": {
            "history": "Bull: ...\nBear: ...\nBull: ...\nBear: ...",
            "judge_decision": "研究主管:综合看多看空双方,倾向谨慎持有",
        },
        "market_report": "技术面:MACD 死叉,空头排列,趋势偏弱..." * 30,
        "social_report": "情绪面:讨论度下降,看空声音增加..." * 20,
        "news_report": "新闻面:公告:Q1 净利润下降..." * 20,
        "fundamentals_report": "基本面:营收增长但毛利下降,ROE 转负..." * 20,
    }


def test_markdown_contains_all_9_agent_outputs():
    """markdown 必须体现 9 个 Agent 的产出(全部或概览)"""
    r = map_state_to_result(
        stock=_stock(),
        ta_result={"decision": "Underweight", "final_state": _full_state(), "cost_usd": 0.05},
    )
    content = r.content
    # PM 决策书
    assert "PM 最终决策书" in content
    # 交易员
    assert "交易员执行计划" in content
    # 研究主管(之前缺,只在折叠里)
    assert "研究主管裁决" in content
    assert "倾向谨慎持有" in content
    # 风控
    assert "风控辩论裁决" in content
    # 4 位分析师概览
    assert "技术分析师" in content
    assert "情绪分析师" in content
    assert "新闻分析师" in content
    assert "基本面分析师" in content


def test_markdown_analyst_summary_truncated():
    """4 位分析师摘要每位最多 300 字,避免 markdown 爆炸"""
    r = map_state_to_result(
        stock=_stock(),
        ta_result={"decision": "Hold", "final_state": _full_state(), "cost_usd": 0.05},
    )
    # 每位分析师的摘要应该被截断(原始内容 600+ 字)
    # 找到"技术分析师" section,验证其下方截断
    idx = r.content.find("📈 技术分析师")
    next_idx = r.content.find("💬 情绪分析师", idx)
    market_section = r.content[idx:next_idx]
    # 应该有 ...(截断标记)
    assert "..." in market_section


def test_markdown_skips_empty_analyst_sections():
    """某位分析师没产出时,跳过该 section,不留空标题"""
    state = _full_state()
    state["social_report"] = ""  # 情绪分析师没跑
    r = map_state_to_result(
        stock=_stock(),
        ta_result={"decision": "Hold", "final_state": state, "cost_usd": 0.05},
    )
    # 其他三位还在
    assert "技术分析师" in r.content
    assert "新闻分析师" in r.content
    # 情绪分析师 section 不应该出现(因为没数据)
    assert "💬 情绪分析师" not in r.content


def test_markdown_skips_judge_when_no_debate():
    """没辩论历史时不渲染'研究主管裁决' section"""
    state = _full_state()
    state["investment_debate_state"] = {"history": "", "judge_decision": ""}
    r = map_state_to_result(
        stock=_stock(),
        ta_result={"decision": "Hold", "final_state": state, "cost_usd": 0.05},
    )
    assert "研究主管裁决" not in r.content
