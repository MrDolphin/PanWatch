"""TradingAgentsAgent 单元测试 — 不依赖 tradingagents 上游库安装。

覆盖:
- collect() 从 Provider 体系收集数据
- _check_availability 软依赖检测
- llm_adapter 配置桥接
- result_mapper 状态映射
- cost_tracker 预算估算
- toolkit_adapter monkeypatch 上下文
- progress 聚合
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from src.agents.tradingagents.agent import TradingAgentsAgent, TradingAgentsUnavailable
from src.agents.tradingagents.cost_tracker import (
    check_budget,
    estimate_cost,
    get_today_cache_key,
)
from src.agents.tradingagents.llm_adapter import (
    VALID_ANALYSTS,
    build_ta_llm_config,
    inject_api_key_env,
)
from src.agents.tradingagents.progress import (
    PanWatchProgressHandler,
    aggregate_progress,
    STAGES_ORDER,
)
from src.agents.tradingagents.result_mapper import (
    DECISION_LABEL_MAP,
    map_state_to_result,
)
from src.agents.tradingagents.toolkit_adapter import (
    is_a_share,
    panwatch_data_context,
    patch_route_to_vendor,
)


# ============================================================================
# llm_adapter
# ============================================================================


class TestLLMAdapter(unittest.TestCase):
    def test_valid_analysts_set(self):
        """合法分析师集合包含 4 个上游期望值"""
        self.assertEqual(VALID_ANALYSTS, {"market", "social", "news", "fundamentals"})

    def test_build_ta_llm_config_basic(self):
        """生成 TradingAgents config dict — 关键字段齐全"""
        ai_client = MagicMock()
        ai_client.base_url = "https://api.deepseek.com"
        ai_client.model = "deepseek-chat"
        ai_client.api_key = "sk-test"

        config = build_ta_llm_config(
            ai_client, debate_rounds=2, selected_analysts=["market", "news"]
        )
        # 用 openrouter 走标准 chat completions,避开 OpenAI Responses API 的兼容性问题
        self.assertEqual(config["llm_provider"], "openrouter")
        self.assertEqual(config["backend_url"], "https://api.deepseek.com")
        self.assertEqual(config["deep_think_llm"], "deepseek-chat")
        self.assertEqual(config["max_debate_rounds"], 2)
        self.assertEqual(set(config["selected_analysts"]), {"market", "news"})
        self.assertEqual(config["output_language"], "Chinese")
        self.assertFalse(config["checkpoint_enabled"])

    def test_build_ta_llm_config_rejects_invalid_analyst(self):
        """非法分析师名 — 抛 ValueError"""
        ai_client = MagicMock()
        with self.assertRaises(ValueError):
            build_ta_llm_config(
                ai_client, selected_analysts=["market", "technical"]
            )

    def test_inject_api_key_env(self):
        """API key 注入到环境变量 — OPENAI_API_KEY 被设置"""
        import os
        ai_client = MagicMock(api_key="sk-test-key")
        inject_api_key_env(ai_client)
        self.assertEqual(os.environ.get("OPENAI_API_KEY"), "sk-test-key")


# ============================================================================
# result_mapper
# ============================================================================


class TestResultMapper(unittest.TestCase):
    def _mock_stock(self):
        stock = MagicMock()
        stock.symbol = "600519"
        stock.name = "贵州茅台"
        return stock

    def test_decision_buy_maps_to_chinese_label(self):
        """BUY 决策 — 映射成 「买入」"""
        stock = self._mock_stock()
        ta_result = {
            "decision": "BUY",
            "final_state": {
                "final_trade_decision": "估值修复 + 资金流持续净流入",
                "trader_investment_plan": "建议加仓",
            },
            "cost_usd": 0.05,
        }
        result = map_state_to_result(stock=stock, ta_result=ta_result, model_label="deepseek/deepseek-chat")
        self.assertEqual(result.agent_name, "tradingagents")
        self.assertIn("买入", result.title)
        sug = result.raw_data["suggestion"]
        self.assertEqual(sug["action"], "buy")
        self.assertEqual(sug["action_label"], "买入")
        self.assertTrue(sug["should_alert"])
        self.assertEqual(result.raw_data["cost_usd"], 0.05)

    def test_decision_hold_no_alert(self):
        """HOLD 决策 — should_alert=False"""
        stock = self._mock_stock()
        ta_result = {"decision": "HOLD", "final_state": {}, "cost_usd": 0.01}
        result = map_state_to_result(stock=stock, ta_result=ta_result, model_label="")
        self.assertFalse(result.raw_data["suggestion"]["should_alert"])

    def test_unknown_decision_falls_back_to_hold(self):
        """未知决策值 — 兜底成 hold,不抛异常"""
        stock = self._mock_stock()
        ta_result = {"decision": "STRONG_BUY", "final_state": {}, "cost_usd": 0}
        result = map_state_to_result(stock=stock, ta_result=ta_result, model_label="")
        self.assertEqual(result.raw_data["suggestion"]["action"], "hold")

    def test_extract_confidence_from_text(self):
        """从文本提取 confidence — 「confidence: 7/10」匹配到 7.0"""
        stock = self._mock_stock()
        ta_result = {
            "decision": "BUY",
            "final_state": {
                "final_trade_decision": "Strong recommendation. confidence: 7/10",
            },
            "cost_usd": 0,
        }
        result = map_state_to_result(stock=stock, ta_result=ta_result, model_label="")
        self.assertEqual(result.raw_data["confidence"], 7.0)

    def test_analyst_reports_preserved(self):
        """4 分析师报告 — 全部保留到 raw_data"""
        stock = self._mock_stock()
        ta_result = {
            "decision": "SELL",
            "final_state": {
                "market_report": "技术面看跌",
                "social_report": "社交情绪偏空",
                "news_report": "近期无重大利好",
                "fundamentals_report": "估值偏高",
            },
            "cost_usd": 0.03,
        }
        result = map_state_to_result(stock=stock, ta_result=ta_result, model_label="")
        reports = result.raw_data["analyst_reports"]
        self.assertEqual(reports["market"], "技术面看跌")
        self.assertEqual(reports["fundamentals"], "估值偏高")


# ============================================================================
# cost_tracker
# ============================================================================


class TestCostTracker(unittest.TestCase):
    def test_estimate_cost_deepseek_shallow(self):
        """deepseek-chat shallow — 单次估算应在 $0.02-$0.06 范围"""
        est = estimate_cost(
            debate_rounds=1,
            selected_analysts=["market", "social", "news", "fundamentals"],
            model="deepseek-chat",
        )
        self.assertEqual(est["model"], "deepseek-chat")
        self.assertGreater(est["cost_low_usd"], 0.005)
        self.assertLess(est["cost_high_usd"], 0.20)
        self.assertGreater(est["cost_high_usd"], est["cost_low_usd"])

    def test_estimate_cost_unknown_model_falls_back(self):
        """未知模型 — 不抛异常,fallback 到 deepseek 单价"""
        est = estimate_cost(debate_rounds=1, selected_analysts=["market"], model="my-custom-llm")
        self.assertGreater(est["cost_low_usd"], 0)

    def test_get_today_cache_key_includes_today(self):
        """缓存键 — 含日期 + symbol + market + debate_rounds + model"""
        key = get_today_cache_key("600519", "CN", 1, "deepseek-chat")
        today_str = datetime.now().strftime("%Y-%m-%d")
        self.assertIn(today_str, key)
        self.assertIn("600519", key)
        self.assertIn("CN", key)
        self.assertIn("r1", key)
        self.assertIn("deepseek-chat", key)


# ============================================================================
# toolkit_adapter
# ============================================================================


class TestToolkitAdapter(unittest.TestCase):
    def test_is_a_share_six_digits(self):
        """A 股识别 — 6 位纯数字才算"""
        self.assertTrue(is_a_share("600519"))
        self.assertTrue(is_a_share("000001"))
        self.assertFalse(is_a_share("AAPL"))
        self.assertFalse(is_a_share("00700"))  # 港股 5 位
        self.assertFalse(is_a_share("12345"))   # 5 位
        self.assertFalse(is_a_share(""))

    def test_panwatch_data_context_isolation(self):
        """数据上下文 — 进入/退出时不污染外部"""
        from src.agents.tradingagents import toolkit_adapter
        self.assertEqual(toolkit_adapter._PANWATCH_DATA_CACHE, {})
        with panwatch_data_context({"klines": [1, 2, 3]}):
            self.assertEqual(toolkit_adapter._PANWATCH_DATA_CACHE.get("klines"), [1, 2, 3])
        self.assertEqual(toolkit_adapter._PANWATCH_DATA_CACHE, {})

    def test_patch_route_to_vendor_noop_when_lib_absent(self):
        """tradingagents 未安装 — patch 上下文 no-op,不抛异常"""
        # 当 import 失败时,patch 应该静默 yield
        with patch_route_to_vendor():
            pass  # 不应抛异常


# ============================================================================
# progress
# ============================================================================


class TestProgress(unittest.TestCase):
    def test_progress_handler_records_cost(self):
        """ProgressHandler — record_cost 累加 total_cost"""
        handler = PanWatchProgressHandler(trace_id="test-123")
        handler.record_cost(0.01)
        handler.record_cost(0.02)
        self.assertAlmostEqual(handler._total_cost, 0.03)

    def test_aggregate_progress_empty(self):
        """聚合空日志 — 所有阶段 pending"""
        result = aggregate_progress([])
        self.assertEqual(len(result["stages"]), len(STAGES_ORDER))
        for stage in result["stages"]:
            self.assertEqual(stage["status"], "pending")

    def test_aggregate_progress_with_stages(self):
        """聚合日志 — stage_start/stage_end 正确标记状态"""
        logs = [
            {
                "timestamp": "2026-05-16T09:00:00",
                "tags": {"stage": "market_analyst", "action": "stage_start", "total_cost_usd": 0.0},
            },
            {
                "timestamp": "2026-05-16T09:00:30",
                "tags": {"stage": "market_analyst", "action": "stage_end", "total_cost_usd": 0.005},
            },
            {
                "timestamp": "2026-05-16T09:00:35",
                "tags": {"stage": "social_analyst", "action": "stage_start", "total_cost_usd": 0.005},
            },
        ]
        result = aggregate_progress(logs)
        self.assertIn("market_analyst", result["completed_stages"])
        self.assertEqual(result["current_stage"], "social_analyst")
        self.assertEqual(result["total_cost_usd"], 0.005)


# ============================================================================
# Agent class
# ============================================================================


class TestTradingAgentsAgent(unittest.TestCase):
    def test_agent_init_defaults(self):
        """默认实例化 — 4 个分析师,1 轮辩论"""
        agent = TradingAgentsAgent()
        self.assertEqual(set(agent.analyst_types), VALID_ANALYSTS)
        self.assertEqual(agent.debate_rounds, 1)
        self.assertEqual(agent.monthly_budget_usd, 10.0)

    def test_agent_init_rejects_invalid_analyst(self):
        """初始化时校验 analyst 类型 — 非法值抛 ValueError"""
        with self.assertRaises(ValueError):
            TradingAgentsAgent(analyst_types=["market", "technical"])

    def test_agent_availability_reflects_library_install(self):
        """tradingagents 软依赖 — 库在则 _available=True,否则 False + import_error 非空"""
        agent = TradingAgentsAgent()
        try:
            import tradingagents  # noqa: F401
            self.assertTrue(agent._available)
            self.assertEqual(agent._import_error, "")
        except ImportError:
            self.assertFalse(agent._available)
            self.assertIn("tradingagents", agent._import_error)

    async def _run_analyze_unavailable(self):
        agent = TradingAgentsAgent()
        # 强制标记不可用,验证 analyze 立即抛错而不会进入 propagate
        agent._available = False
        agent._import_error = "mocked unavailable"
        context = MagicMock()
        with self.assertRaises(TradingAgentsUnavailable):
            await agent.analyze(context, {"stock": MagicMock(symbol="600519", name="X")})

    def test_analyze_raises_when_unavailable(self):
        """库未安装时 analyze() 抛 TradingAgentsUnavailable(强制标记验证)"""
        import asyncio
        asyncio.run(self._run_analyze_unavailable())


# ============================================================================
# Integration: collect with mocked Providers
# ============================================================================


class TestAgentCollect(unittest.IsolatedAsyncioTestCase):
    async def test_collect_from_providers(self):
        """collect() — 并发拉 4 个 orchestrator 的数据"""
        from src.core.providers.base import ProviderResponse

        agent = TradingAgentsAgent()

        # mock stock
        stock = MagicMock()
        stock.symbol = "600519"
        stock.name = "贵州茅台"
        stock.market = MagicMock()
        stock.market.value = "CN"

        context = MagicMock()
        context.watchlist = [stock]

        # patch 4 个 orchestrator,各返回 fake 数据
        quote_resp = ProviderResponse(success=True, data=[{"symbol": "600519", "current_price": 1332.95}])
        kline_resp = ProviderResponse(success=True, data=[{"date": "2026-05-15", "close": 1332.95}])
        flow_resp = ProviderResponse(success=True, data=[{"main_net_inflow": 1000000}])
        events_resp = ProviderResponse(success=True, data=[])

        with patch(
            "src.core.providers.get_quote_orchestrator"
        ) as mock_quote, patch(
            "src.core.providers.get_kline_orchestrator"
        ) as mock_kline, patch(
            "src.core.providers.get_capital_flow_orchestrator"
        ) as mock_flow, patch(
            "src.core.providers.get_events_orchestrator"
        ) as mock_events:
            mock_quote.return_value.fetch = AsyncMock(return_value=quote_resp)
            mock_kline.return_value.fetch = AsyncMock(return_value=kline_resp)
            mock_flow.return_value.fetch = AsyncMock(return_value=flow_resp)
            mock_events.return_value.fetch = AsyncMock(return_value=events_resp)

            data = await agent.collect(context)

        self.assertEqual(data["stock"], stock)
        self.assertEqual(data["quote"]["symbol"], "600519")
        self.assertEqual(len(data["klines"]), 1)
        self.assertIn("fetched_at", data)


if __name__ == "__main__":
    unittest.main()
