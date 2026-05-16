"""把 PanWatch Provider 体系适配进 TradingAgents 数据流。

TradingAgents 上游(0.2.x)默认通过 `tradingagents.dataflows.interface.route_to_vendor`
把数据请求路由到 yfinance / alpha_vantage 等 vendor。**没有公开 toolkit 注入入口**。

我们的策略:**monkeypatch route_to_vendor**。当 LangGraph 节点调用 `get_stockstats_*`
等方法时,我们的 patch 检测 symbol 是 A 股代码(6 位数字)就走 PanWatch Provider,
否则放行到上游默认 vendor(yfinance 等)。

这避免:
- TradingAgents 用 yfinance 拉 A 股拉不到(A 股 yfinance 不全)
- 重复请求外部 API(PanWatch 已有缓存的 quote/kline 直接复用)

也保留:
- US/HK 走上游 yfinance vendor 不变
- 用户可关闭 patch 走原生路径

注意:本模块对上游 TradingAgents API 有强依赖,如上游重构 route_to_vendor 接口
需要同步更新。已通过 `tradingagents` 软依赖 + try/except 优雅降级。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)


# 缓存:在 patch 上下文里把 PanWatch 拉好的数据塞这里,patch 命中时直接返回
_PANWATCH_DATA_CACHE: dict[str, Any] = {}


@contextmanager
def panwatch_data_context(data: dict[str, Any]):
    """在调用 TradingAgents 的代码块周围用本 context manager 注入数据。

    用法:
        with panwatch_data_context({"klines": ..., "quote": ..., "events": ...}):
            graph = TradingAgentsGraph(...)
            final_state, decision = graph.propagate("600519", "2026-05-16")

    退出 context 时清空数据,避免跨请求污染。
    """
    global _PANWATCH_DATA_CACHE
    prev = _PANWATCH_DATA_CACHE
    _PANWATCH_DATA_CACHE = dict(data)
    try:
        yield
    finally:
        _PANWATCH_DATA_CACHE = prev


def is_a_share(symbol: str) -> bool:
    """A 股代码判定:6 位纯数字。"""
    return bool(symbol) and len(symbol) == 6 and symbol.isdigit()


@contextmanager
def patch_route_to_vendor():
    """Monkeypatch tradingagents.dataflows.interface.route_to_vendor。

    当请求 A 股代码时,从 _PANWATCH_DATA_CACHE 返回 PanWatch 已拉的数据。
    非 A 股放行到原函数。

    退出时恢复原函数。**幂等**:多次进入互不影响。

    如果 tradingagents 库未安装,本 context manager 是 no-op,不抛异常。
    """
    try:
        from tradingagents.dataflows import interface as ta_interface
    except ImportError:
        logger.warning("[TA toolkit] tradingagents 未安装,跳过 monkeypatch")
        yield
        return

    if not hasattr(ta_interface, "route_to_vendor"):
        logger.warning(
            "[TA toolkit] route_to_vendor 不存在 (上游 API 可能变更),"
            "走默认 vendor 路径"
        )
        yield
        return

    original = ta_interface.route_to_vendor

    def _patched(method_name: str, **kwargs):
        symbol = kwargs.get("symbol") or kwargs.get("ticker") or ""
        if is_a_share(symbol) and _PANWATCH_DATA_CACHE:
            try:
                return _serve_from_panwatch(method_name, symbol, kwargs)
            except Exception as e:
                logger.warning(
                    f"[TA toolkit] PanWatch 数据回填失败 (symbol={symbol}, "
                    f"method={method_name}): {e},退回上游 vendor"
                )
        return original(method_name, **kwargs)

    ta_interface.route_to_vendor = _patched
    try:
        yield
    finally:
        ta_interface.route_to_vendor = original


def _serve_from_panwatch(method_name: str, symbol: str, kwargs: dict) -> str:
    """从 _PANWATCH_DATA_CACHE 构造 TradingAgents 期望的数据格式(CSV / JSON 字符串)。

    上游各 vendor 方法返回类型不一,通常是 str(已格式化的 CSV/表格/JSON)。
    本函数尽量兼容常见 method_name。**未识别的 method 返回空串,触发上游默认 vendor。**
    """
    method = (method_name or "").lower()

    # 1) K 线相关:get_stockstats / get_yfin_data 等
    if any(k in method for k in ("stockstats", "yfin", "ohlcv", "kline", "price")):
        klines = _PANWATCH_DATA_CACHE.get("klines") or []
        if klines:
            return _klines_to_csv(klines)

    # 2) 公告/事件:get_finnhub_news / get_news / get_events
    if any(k in method for k in ("news", "event", "announce")):
        events = _PANWATCH_DATA_CACHE.get("events") or []
        if events:
            return _events_to_text(events, limit=20)

    # 3) 资金流
    if any(k in method for k in ("flow", "capital", "fund")):
        flow = _PANWATCH_DATA_CACHE.get("capital_flow")
        if flow:
            return _flow_to_text(flow)

    # 4) 基本面 / 财务:暂不实现(PanWatch 没采集)。返回 fallback 提示
    if "fundamental" in method or "financial" in method or "income" in method:
        return f"[基本面数据暂未接入 PanWatch,symbol={symbol}]"

    # 未识别:让上游走默认 vendor
    raise NotImplementedError(f"no panwatch backing for {method_name}")


def _klines_to_csv(klines) -> str:
    """KlineData list → CSV 字符串。

    TradingAgents 上游期望:date,open,high,low,close,volume
    """
    if not klines:
        return "date,open,high,low,close,volume\n"
    lines = ["date,open,high,low,close,volume"]
    for k in klines:
        date_v = getattr(k, "date", None) or (k.get("date") if isinstance(k, dict) else "")
        open_v = _attr(k, "open")
        high_v = _attr(k, "high")
        low_v = _attr(k, "low")
        close_v = _attr(k, "close")
        vol_v = _attr(k, "volume")
        lines.append(f"{date_v},{open_v},{high_v},{low_v},{close_v},{vol_v}")
    return "\n".join(lines)


def _events_to_text(events, limit: int = 20) -> str:
    if not events:
        return "无近期公告/事件"
    out = []
    for ev in events[:limit]:
        title = getattr(ev, "title", None) or (
            ev.get("title") if isinstance(ev, dict) else str(ev)
        )
        ts = getattr(ev, "publish_time", None) or (
            ev.get("publish_time") if isinstance(ev, dict) else ""
        )
        out.append(f"- [{ts}] {title}")
    return "\n".join(out)


def _flow_to_text(flow) -> str:
    if isinstance(flow, list):
        flow = flow[0] if flow else None
    if not flow:
        return "无资金流向数据"
    main_net = _attr(flow, "main_net_inflow")
    main_pct = _attr(flow, "main_net_inflow_pct")
    return f"主力净流入:{main_net} / {main_pct}%"


def _attr(obj, name, default=""):
    if hasattr(obj, name):
        v = getattr(obj, name)
        return v if v is not None else default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return default
