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


DECISION_LABEL_MAP = {
    "buy": "买入",
    "hold": "持有",
    "sell": "卖出",
}


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
    decision_raw = (ta_result.get("decision") or "HOLD").strip().lower()
    decision = decision_raw if decision_raw in DECISION_LABEL_MAP else "hold"
    state = ta_result.get("final_state") or {}
    cost_usd = float(ta_result.get("cost_usd", 0.0) or 0.0)

    confidence = _extract_confidence(state)
    short_reason = _short_reason(state)

    suggestion = {
        "action": decision,
        "action_label": DECISION_LABEL_MAP[decision],
        "signal": _truncate(state.get("trader_investment_plan", ""), 200),
        "reason": state.get("final_trade_decision") or short_reason,
        "should_alert": decision in ("buy", "sell"),
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
            "decision": decision,
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

    parts.append(
        f"## 最终决策\n\n"
        f"**{suggestion['action_label']}** · 置信度 {suggestion['confidence']:.1f}/10\n"
    )

    if state.get("final_trade_decision"):
        parts.append(f"### 核心理由\n\n{state['final_trade_decision']}\n")

    if state.get("trader_investment_plan"):
        parts.append(f"### 交易员建议\n\n{state['trader_investment_plan']}\n")

    if state.get("risk_judge_decision"):
        parts.append(f"### 风控判定\n\n{state['risk_judge_decision']}\n")

    parts.append(
        "\n---\n"
        f"_本分析由 TradingAgents 多 Agent 框架生成,仅供学习研究参考,不构成投资建议。_\n"
        f"\n成本:${cost_usd:.4f}"
    )
    if model_label:
        parts.append(f" · AI:{model_label}")

    return "\n".join(parts)
