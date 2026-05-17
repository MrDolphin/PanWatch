"""TradingAgents 输出 → PanWatch AnalysisResult 映射。

TradingAgents 的 `final_state` 是 LangGraph 累积的 dict,关键字段(摘自上游):
- market_report / social_report / news_report / fundamentals_report: 4 个分析师报告
- investment_debate_state: 看多看空辩论历史 {history, current_response, judge_decision}
- trader_investment_plan: 交易员意见
- risk_judge_decision: 风控判定
- final_trade_decision: PM 整合后的最终决策书
- (processed_signal): "BUY" / "HOLD" / "SELL"
"""

from __future__ import annotations

import re
from typing import Any

from src.agents.base import AnalysisResult


# 上游 5 档评级 → PanWatch 显示标签
RATING_LABEL_MAP = {
    "buy": "买入",
    "overweight": "增持",
    "hold": "持有",
    "underweight": "减持",
    "sell": "卖出",
}

# 5 档 → 3 档(给 action 字段;前端 'buy' | 'hold' | 'sell')
RATING_ACTION_MAP = {
    "buy": "buy",
    "overweight": "buy",
    "hold": "hold",
    "underweight": "sell",
    "sell": "sell",
}

# 旧字段名兼容:某些下游代码可能 import DECISION_LABEL_MAP
DECISION_LABEL_MAP = RATING_LABEL_MAP


def map_state_to_result(
    *,
    stock: Any,
    ta_result: dict[str, Any],
    model_label: str = "",
) -> AnalysisResult:
    """主入口:把 TradingAgents 的 final_state 映射成 AnalysisResult。

    Args:
        stock: PanWatch StockConfig(symbol/name/market)
        ta_result: {"decision": str, "final_state": dict, "cost_usd": float}
        model_label: 形如 "deepseek/deepseek-chat",写到 markdown 末尾
    """
    state = ta_result.get("final_state") or {}
    cost_usd = float(ta_result.get("cost_usd", 0.0) or 0.0)

    # 上游 PM 返回 5 档评级("Buy"/"Overweight"/"Hold"/"Underweight"/"Sell")
    # 如果 propagate() 第二个返回拿不到 5 档值,从 final_trade_decision 文本里 fallback 解析
    rating_raw = (ta_result.get("decision") or "").strip().lower()
    if rating_raw not in RATING_LABEL_MAP:
        rating_raw = _parse_rating_from_text(state.get("final_trade_decision") or "")

    action = RATING_ACTION_MAP.get(rating_raw, "hold")
    action_label = RATING_LABEL_MAP.get(rating_raw, "持有")

    confidence = _extract_confidence(state)
    short_reason = _short_reason(state)

    suggestion = {
        "action": action,
        "action_label": action_label,
        "rating_raw": rating_raw or "hold",  # 保留原始 5 档,前端/历史可查
        "signal": _truncate(state.get("trader_investment_plan", ""), 200),
        "reason": state.get("final_trade_decision") or short_reason,
        "should_alert": rating_raw in ("buy", "overweight", "underweight", "sell"),
        "agent_name": "tradingagents",
        "agent_label": "TradingAgents 深度",
        "confidence": confidence,
    }

    content = _render_markdown(state, suggestion, model_label, cost_usd)

    return AnalysisResult(
        agent_name="tradingagents",
        title=f"【深度】{stock.name}({stock.symbol}):{suggestion['action_label']}",
        content=content,
        raw_data={
            "suggestion": suggestion,
            "cost_usd": cost_usd,
            "should_alert": suggestion["should_alert"],
            "decision": action,           # 兼容旧字段(3 档)
            "rating": rating_raw or "hold",  # 新字段(5 档原始)
            "confidence": confidence,
            "debate_history": _extract_debate(state),
            "risk_judgment": state.get("risk_judge_decision") or "",
            "analyst_reports": {
                "market": state.get("market_report") or "",
                "social": state.get("social_report") or "",
                "news": state.get("news_report") or "",
                "fundamentals": state.get("fundamentals_report") or "",
            },
            "final_decision": state.get("final_trade_decision") or "",
            "trader_plan": state.get("trader_investment_plan") or "",
        },
    )


# ---- helpers ----


_RATING_TEXT_RE = re.compile(
    r"(?:Rating|评级|最终交易决策|Final\s+(?:Trade\s+)?Decision|FINAL\s+TRANSACTION\s+PROPOSAL)"
    r"[\s\*:\-—]+(\*\*)?\s*(Buy|Overweight|Hold|Underweight|Sell|买入|增持|持有|减持|卖出)",
    re.I,
)
_RATING_ZH_TO_EN = {
    "买入": "buy", "增持": "overweight", "持有": "hold",
    "减持": "underweight", "卖出": "sell",
}


def _parse_rating_from_text(text: str) -> str:
    """从文本里抽 5 档评级。优先 'Rating: X' 标签,然后第一个 5 档词。"""
    if not text:
        return ""
    m = _RATING_TEXT_RE.search(text)
    if m:
        word = m.group(2).lower()
        if word in _RATING_ZH_TO_EN:
            word = _RATING_ZH_TO_EN[word]
        if word in RATING_LABEL_MAP:
            return word
    # 兜底:扫描整段文本里第一个出现的 5 档英文/中文词
    text_low = text.lower()
    for word in ("overweight", "underweight", "buy", "sell", "hold"):
        if word in text_low:
            return word
    for zh, en in _RATING_ZH_TO_EN.items():
        if zh in text:
            return en
    return ""


_CONFIDENCE_PATTERNS = [
    re.compile(r"confidence[:\s]+(\d+(?:\.\d+)?)\s*(?:/10)?", re.I),
    re.compile(r"置信度[:\s]+(\d+(?:\.\d+)?)", re.I),
    re.compile(r"信心(?:度)?[:\s]+(\d+(?:\.\d+)?)", re.I),
]


def _extract_confidence(state: dict) -> float:
    """从 PM/risk/trader 文本里粗暴提取置信度(0-10),失败默认 5.0。"""
    candidates = [
        state.get("final_trade_decision", ""),
        state.get("risk_judge_decision", ""),
        state.get("trader_investment_plan", ""),
    ]
    for text in candidates:
        if not text:
            continue
        for pat in _CONFIDENCE_PATTERNS:
            m = pat.search(text)
            if m:
                try:
                    v = float(m.group(1))
                    # 处理百分制(转 0-10)
                    if v > 10:
                        v = v / 10
                    return max(0.0, min(10.0, v))
                except (ValueError, IndexError):
                    continue
    return 5.0


def _short_reason(state: dict, limit: int = 120) -> str:
    """取一段精炼理由,优先 final_trade_decision 前 120 字。"""
    for key in ("final_trade_decision", "trader_investment_plan", "risk_judge_decision"):
        text = state.get(key) or ""
        text = text.strip()
        if text:
            return _truncate(text, limit)
    return ""


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _extract_debate(state: dict) -> dict:
    """提取辩论历史。上游 investment_debate_state 大致结构:
    {
        "history": "...",      # 全量辩论文本
        "current_response": ...,
        "judge_decision": ...,
    }
    """
    debate = state.get("investment_debate_state") or {}
    if not isinstance(debate, dict):
        return {}
    return {
        "history": debate.get("history", ""),
        "current_response": debate.get("current_response", ""),
        "judge_decision": debate.get("judge_decision", ""),
    }


def _render_markdown(
    state: dict, suggestion: dict, model_label: str, cost_usd: float
) -> str:
    parts = []

    rating_raw = suggestion.get("rating_raw") or ""
    rating_note = (
        f"(评级:{RATING_LABEL_MAP.get(rating_raw, '持有')})"
        if rating_raw in RATING_LABEL_MAP else ""
    )
    parts.append(
        f"## 最终决策\n\n"
        f"**{suggestion['action_label']}** {rating_note} · 置信度 {suggestion['confidence']:.1f}/10\n"
    )

    # 9 个 Agent 链路:PM(决策书) → Trader → 研究主管 → 风控 → 4 位分析师摘要
    if state.get("final_trade_decision"):
        parts.append(f"### 🎯 PM 最终决策书\n\n{state['final_trade_decision']}\n")

    if state.get("trader_investment_plan"):
        parts.append(f"### 💼 交易员执行计划\n\n{state['trader_investment_plan']}\n")

    # 研究主管裁决 — 看多/看空辩论后的结论,之前只在折叠的辩论 section 末尾
    debate = state.get("investment_debate_state") or {}
    judge_decision = ""
    if isinstance(debate, dict):
        judge_decision = (debate.get("judge_decision") or "").strip()
    if judge_decision:
        parts.append(f"### ⚖️ 研究主管裁决(看多 vs 看空)\n\n{judge_decision}\n")

    if state.get("risk_judge_decision"):
        parts.append(f"### 🛡️ 风控辩论裁决\n\n{state['risk_judge_decision']}\n")

    # 4 位分析师摘要(每位截 300 字),避免 markdown 过长又能扫到关键观点
    analyst_summary = _render_analyst_overview(state)
    if analyst_summary:
        parts.append(f"### 📊 4 位分析师观点概览\n\n{analyst_summary}\n")

    parts.append(
        "\n---\n"
        f"_本分析由 TradingAgents 9-Agent 框架生成(技术/情绪/新闻/基本面 → 看多看空辩论 "
        f"→ 研究主管 → 交易员 → 风控辩论 → PM)。仅供学习研究参考,不构成投资建议。_\n"
        f"\n成本:${cost_usd:.4f}"
    )
    if model_label:
        parts.append(f" · AI:{model_label}")

    return "\n".join(parts)


def _render_analyst_overview(state: dict, per_analyst_chars: int = 300) -> str:
    """4 位分析师产出摘要(每位 N 字),让 markdown 主体也能看到他们的核心结论。"""
    analysts = [
        ("market_report", "📈 技术分析师"),
        ("social_report", "💬 情绪分析师"),
        ("news_report", "📰 新闻分析师"),
        ("fundamentals_report", "💰 基本面分析师"),
    ]
    lines = []
    for field, label in analysts:
        text = (state.get(field) or "").strip()
        if not text:
            continue
        snippet = _truncate(text, per_analyst_chars)
        lines.append(f"**{label}**\n\n{snippet}\n")
    return "\n".join(lines)
