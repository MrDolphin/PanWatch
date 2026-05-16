"""TradingAgentsAgent — PanWatch 的 BaseAgent 子类,集成 TauricResearch/TradingAgents。

设计要点(详见 .docs/tradingagents/02-technical-design.md):
1. collect() 走 PanWatch Provider Orchestrator,4 类数据并发拉
2. analyze() 重写,不走单次 ai_client.chat,而是调 TradingAgentsGraph
3. monkeypatch route_to_vendor 让 TradingAgents 拿到 PanWatch 数据(A 股专用)
4. progress callback + cost tracker + 月度预算 + 同日缓存
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Any

from src.agents.base import AgentContext, AnalysisResult, BaseAgent
from src.agents.tradingagents.cost_tracker import (
    check_budget,
    estimate_cost,
    get_today_cache_key,
)
from src.agents.tradingagents.langchain_compat import apply_compat_patches
from src.agents.tradingagents.llm_adapter import (
    VALID_ANALYSTS,
    build_ta_llm_config,
    inject_api_key_env,
)
from src.agents.tradingagents.progress import PanWatchProgressHandler
from src.agents.tradingagents.result_mapper import map_state_to_result
from src.agents.tradingagents.toolkit_adapter import (
    panwatch_data_context,
    patch_route_to_vendor,
)
from src.core.analysis_history import get_analysis, save_analysis

logger = logging.getLogger(__name__)


class TradingAgentsUnavailable(RuntimeError):
    """tradingagents 库未安装或上游 API 变更导致不可用。"""


class TradingAgentsAgent(BaseAgent):
    name = "tradingagents"
    display_name = "TradingAgents 深度分析"
    description = "多 Agent 投资决策框架,3-5 分钟,~$0.05/次 (deepseek-chat)"

    def __init__(
        self,
        analyst_types: list[str] | None = None,
        debate_rounds: int = 1,
        monthly_budget_usd: float = 10.0,
        over_budget_action: str = "reject",  # reject / warn / continue
        cache_ttl_hours: int = 12,
        output_language: str = "Chinese",
    ):
        # 校验分析师配置
        analysts = list(analyst_types or sorted(VALID_ANALYSTS))
        invalid = [a for a in analysts if a not in VALID_ANALYSTS]
        if invalid:
            raise ValueError(
                f"非法 analyst 名: {invalid}; "
                f"合法值: {sorted(VALID_ANALYSTS)}"
            )

        self.analyst_types = analysts
        self.debate_rounds = max(1, int(debate_rounds))
        self.monthly_budget_usd = float(monthly_budget_usd)
        self.over_budget_action = over_budget_action
        self.cache_ttl_hours = max(0, int(cache_ttl_hours))
        self.output_language = output_language

        # 软依赖检测
        self._available, self._import_error = self._check_availability()

    # ---- BaseAgent 抽象方法 ----

    async def collect(self, context: AgentContext) -> dict:
        """从 PanWatch Provider 体系收集数据,并发拉 4 类。"""
        from src.core.providers import (
            ProviderRequest,
            get_capital_flow_orchestrator,
            get_events_orchestrator,
            get_kline_orchestrator,
            get_quote_orchestrator,
        )

        if not context.watchlist:
            raise ValueError("TradingAgents 需要至少 1 只股票")
        # 单只标的为粒度;若 watchlist 多只,取第一只
        stock = context.watchlist[0]
        req = ProviderRequest(symbols=(stock.symbol,), market=stock.market.value)

        kline_req = ProviderRequest(
            symbols=(stock.symbol,),
            market=stock.market.value,
            extra=(("days", 120),),
        )

        quotes_t = get_quote_orchestrator().fetch(req)
        klines_t = get_kline_orchestrator().fetch(kline_req)
        capital_t = get_capital_flow_orchestrator().fetch(req)
        events_t = get_events_orchestrator().fetch(req)

        try:
            quotes, klines, capital, events = await asyncio.gather(
                quotes_t, klines_t, capital_t, events_t
            )
        except Exception as e:
            logger.warning(f"[TA] 数据收集部分失败: {e}")
            quotes = klines = capital = events = None

        def _data(resp, default):
            return resp.data if resp and resp.success else default

        return {
            "stock": stock,
            "quote": (_data(quotes, []) or [{}])[0] if _data(quotes, []) else {},
            "klines": _data(klines, []) or [],
            "capital_flow": _data(capital, []) or [],
            "events": _data(events, []) or [],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    def build_prompt(self, data: dict, context: AgentContext) -> tuple[str, str]:
        # BaseAgent 抽象要求,但本 agent 不走单次 prompt
        return "", ""

    # ---- 重写 analyze:走 TradingAgents 多 Agent 流 ----

    async def analyze(self, context: AgentContext, data: dict) -> AnalysisResult:
        if not self._available:
            raise TradingAgentsUnavailable(self._import_error)

        stock = data["stock"]
        trace_id = getattr(context, "_trace_id", "") or self._make_trace_id(stock.symbol)

        # 0) 同日缓存命中
        cached = self._try_cache_hit(stock)
        if cached is not None:
            logger.info(
                f"[TA] 命中同日缓存 (agent=tradingagents symbol={stock.symbol})"
            )
            cached.raw_data["from_cache"] = True
            return cached

        # 1) 预算检查
        budget = check_budget(self.monthly_budget_usd, self.name)
        if budget["exceeded"]:
            if self.over_budget_action == "reject":
                raise RuntimeError(
                    f"本月 TradingAgents 预算已用尽 "
                    f"(${budget['used']:.2f} / ${self.monthly_budget_usd:.2f})。"
                    f"如需继续使用,请在「设置」中调高预算上限。"
                )
            elif self.over_budget_action == "warn":
                logger.warning(
                    f"[TA] 预算已超,但策略=warn,继续执行 "
                    f"(${budget['used']:.2f} / ${self.monthly_budget_usd:.2f})"
                )

        # 2) 构造 TradingAgents config
        ta_config = build_ta_llm_config(
            context.ai_client,
            debate_rounds=self.debate_rounds,
            selected_analysts=self.analyst_types,
            output_language=self.output_language,
        )

        # 3) 进度回调
        progress_handler = PanWatchProgressHandler(trace_id, self.name)

        # 4) 同步阻塞,丢到线程池
        ta_result = await asyncio.to_thread(
            self._run_tradingagents_sync,
            ai_client=context.ai_client,
            symbol=stock.symbol,
            market=stock.market.value,
            ta_config=ta_config,
            progress_handler=progress_handler,
            panwatch_data=data,
        )

        # 5) 映射成 AnalysisResult
        result = map_state_to_result(
            stock=stock,
            ta_result=ta_result,
            model_label=context.model_label,
        )

        # 6) 落库到 AnalysisHistory:供 UI 查最近一次结果 (DeepAnalysisModal 弹窗) +
        # 月度成本预算聚合。同标的同日复跑会覆盖 (analysis_history.save_analysis 语义)。
        try:
            save_analysis(
                agent_name=self.name,
                stock_symbol=stock.symbol,
                content=result.content,
                title=result.title,
                raw_data=result.raw_data,
            )
        except Exception as e:
            logger.warning(f"[TA] save_analysis 失败,不影响主流程: {e}")

        return result

    # ---- 私有方法 ----

    def _check_availability(self) -> tuple[bool, str]:
        """检测 tradingagents 是否可用。"""
        try:
            import tradingagents  # noqa: F401
            from tradingagents.graph.trading_graph import TradingAgentsGraph  # noqa: F401
        except ImportError as e:
            return False, (
                "tradingagents 未安装。运行 `pip install -r requirements.txt` "
                "(或单独 `pip install \"tradingagents @ git+https://github.com/TauricResearch/TradingAgents.git\"`)。"
                "公司代理下若失败,临时 `env -u HTTP_PROXY -u HTTPS_PROXY pip install -r requirements.txt`。"
                f"原始错误: {e}"
            )
        except Exception as e:
            return False, f"tradingagents 加载失败: {e}"
        return True, ""

    def _make_trace_id(self, symbol: str) -> str:
        return f"ta-{symbol}-{int(datetime.now().timestamp())}"

    def _try_cache_hit(self, stock) -> AnalysisResult | None:
        """同标的同日是否已分析过 → 返回缓存的 AnalysisResult。"""
        if self.cache_ttl_hours <= 0:
            return None
        try:
            history = get_analysis(
                agent_name=self.name,
                stock_symbol=stock.symbol,
                analysis_date=date.today(),
            )
        except Exception:
            return None
        if not history or not history.raw_data:
            return None
        return AnalysisResult(
            agent_name=self.name,
            title=history.title or f"【深度·缓存】{stock.name}({stock.symbol})",
            content=history.content,
            raw_data=dict(history.raw_data),
        )

    def _run_tradingagents_sync(
        self,
        *,
        ai_client,
        symbol: str,
        market: str,
        ta_config: dict,
        progress_handler,
        panwatch_data: dict,
    ) -> dict[str, Any]:
        """在 worker 线程跑同步 TradingAgents 流程。

        步骤:
        1. inject_api_key_env 注入 API key 到环境变量
        2. patch_route_to_vendor 让 A 股请求路由到 PanWatch 数据
        3. TradingAgentsGraph.propagate 跑 3-5 分钟
        4. 返回 decision + final_state + cost_usd
        """
        # 关键依赖延迟 import,确保 _check_availability 失败时这里不被调用
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        # 应用 LangChain 兼容性补丁:让小模型 (Qwen 7B 等) 返回的
        # tool_calls.args 字符串被自动转 dict。
        apply_compat_patches()
        inject_api_key_env(ai_client)

        # patch + 数据上下文,确保 TradingAgents 调 route_to_vendor 时拿到 PanWatch 数据
        with patch_route_to_vendor(), panwatch_data_context(panwatch_data):
            graph = TradingAgentsGraph(
                selected_analysts=ta_config["selected_analysts"],
                debug=False,
                config=ta_config,
                # callbacks 接受 langchain BaseCallbackHandler 列表;LLM 级别用
                callbacks=[progress_handler] if progress_handler else None,
            )

            # 注入 LangGraph 节点级 callbacks(propagator.get_graph_args 默认 callbacks=None,
            # 不会触发 on_chain_start/end → 进度条永远卡 pending)
            if progress_handler is not None:
                self._inject_graph_callbacks(graph, progress_handler)

            date_str = datetime.now().strftime("%Y-%m-%d")
            try:
                final_state, decision = graph.propagate(symbol, date_str)
            except TypeError:
                # 上游版本可能签名不同(propagate(symbol, date) vs propagate(company_name, trade_date))
                final_state, decision = graph.propagate(
                    company_name=symbol, trade_date=date_str
                )

        # 成本提取(TradingAgents 内部 token 统计;若上游未暴露,fallback 用 estimate)
        cost_usd = self._extract_cost_from_graph(graph) or self._fallback_cost_estimate(
            ta_config
        )

        return {
            "decision": str(decision or "HOLD").upper(),
            "final_state": dict(final_state) if final_state else {},
            "cost_usd": float(cost_usd or 0.0),
        }

    @staticmethod
    def _inject_graph_callbacks(graph, handler):
        """Monkey-patch graph.propagator.get_graph_args 让 LangGraph 节点级 callbacks 也注入。

        否则只有 on_llm_start/end 会触发,on_chain_start/end (节点切换) 不会,进度条卡死。
        """
        try:
            propagator = getattr(graph, "propagator", None)
            if propagator is None or not hasattr(propagator, "get_graph_args"):
                return
            original = propagator.get_graph_args

            def _patched(callbacks=None):
                cbs = list(callbacks or [])
                if handler not in cbs:
                    cbs.append(handler)
                return original(callbacks=cbs)

            propagator.get_graph_args = _patched  # type: ignore[method-assign]
        except Exception as e:
            logger.warning(f"[TA] 注入 LangGraph callbacks 失败: {e}")

    @staticmethod
    def _extract_cost_from_graph(graph) -> float:
        """尝试从 TradingAgentsGraph 实例提取累计成本。上游未必暴露字段,容错。"""
        for attr in ("total_cost", "total_cost_usd", "_total_cost"):
            v = getattr(graph, attr, None)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return 0.0

    def _fallback_cost_estimate(self, ta_config: dict) -> float:
        """fallback 用 estimate 平均值。"""
        est = estimate_cost(
            debate_rounds=ta_config.get("max_debate_rounds", 1),
            selected_analysts=ta_config.get("selected_analysts", []),
            model=ta_config.get("deep_think_llm", "deepseek-chat"),
        )
        return (est["cost_low_usd"] + est["cost_high_usd"]) / 2
