"""TA load_ohlcv 接管:A股/港股走 PanWatch K线,美股透传 yfinance。

新上游 get_verified_market_snapshot → load_ohlcv 直连 yfinance,A股(无 .SS)拉不到
→ NoMarketDataError 整个分析失败。这里验证 PanWatch 接管能为 A股构建 OHLCV,且不误伤美股。
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from src.agents.tradingagents import toolkit_adapter as ta
from src.collectors.kline_collector import KlineCollector, KlineData


def _sample_klines(n: int = 40) -> list[KlineData]:
    base = date(2026, 4, 1)
    return [
        KlineData(
            date=str(base + timedelta(days=i)),
            open=1.0 + i,
            close=2.0 + i,
            high=3.0 + i,
            low=0.5 + i,
            volume=100.0 + i,
        )
        for i in range(n)
    ]


def test_build_df_columns_and_date_filter(monkeypatch):
    """构建的 DataFrame 含 Date/OHLCV 列,Date 为 datetime,且按 curr_date 截断。"""
    monkeypatch.setattr(KlineCollector, "get_klines", lambda self, symbol, days=60: _sample_klines(40))
    df = ta._build_panwatch_ohlcv_df("601238", "2026-04-20")
    assert list(df.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]
    assert str(df["Date"].dtype).startswith("datetime64")
    assert (df["Date"] <= pd.to_datetime("2026-04-20")).all()
    assert len(df) == 20  # 04-01..04-20


def test_load_ohlcv_routes_a_share_to_panwatch(monkeypatch):
    """A股调用走 PanWatch,不触发原生 yfinance load_ohlcv。"""
    monkeypatch.setattr(KlineCollector, "get_klines", lambda self, symbol, days=60: _sample_klines(10))
    real_calls = {"n": 0}

    def fake_real(*a, **k):
        real_calls["n"] += 1
        return pd.DataFrame()

    monkeypatch.setattr(ta, "_real_load_ohlcv", fake_real)
    df = ta._panwatch_load_ohlcv("601238", "2026-06-18")
    assert not df.empty
    assert real_calls["n"] == 0, "A股不应回落到 yfinance"


def test_load_ohlcv_passthrough_for_us(monkeypatch):
    """美股放行原生 load_ohlcv(yfinance),不被 PanWatch 接管。"""
    sentinel = pd.DataFrame({"Date": [pd.to_datetime("2026-01-01")], "Close": [1.0]})
    monkeypatch.setattr(ta, "_real_load_ohlcv", lambda symbol, curr_date, *a, **k: sentinel)
    out = ta._panwatch_load_ohlcv("AAPL", "2026-06-18")
    assert out is sentinel


def test_load_ohlcv_falls_back_when_no_klines(monkeypatch):
    """A股但取不到 K线时,兜底回落原生路径(不抛错)。"""
    monkeypatch.setattr(KlineCollector, "get_klines", lambda self, symbol, days=60: [])
    sentinel = pd.DataFrame({"Date": [pd.to_datetime("2026-01-01")], "Close": [1.0]})
    monkeypatch.setattr(ta, "_real_load_ohlcv", lambda symbol, curr_date, *a, **k: sentinel)
    out = ta._panwatch_load_ohlcv("601238", "2026-06-18")
    assert out is sentinel
