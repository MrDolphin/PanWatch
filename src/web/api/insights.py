from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import List

from sqlalchemy.orm import Session

from src.models.market import MarketCode
from src.collectors.akshare_collector import _tencent_symbol, _fetch_tencent_quotes
from src.collectors.kline_collector import KlineCollector
from src.core.suggestion_pool import get_latest_suggestions
from src.web.api.chat import (
    _build_stock_context,
    _fetch_realtime_context,
    _fetch_technical_context,
    _get_ai_client,
)
from src.web.database import get_db
from src.web.models import Stock
import asyncio
import logging
import time

logger = logging.getLogger(__name__)

router = APIRouter()


class InsightItem(BaseModel):
    symbol: str = Field(..., description="股票代码")
    market: str = Field(..., description="市场: CN/HK/US")


class InsightsBatchRequest(BaseModel):
    items: List[InsightItem]


def _parse_market(market: str) -> MarketCode:
    try:
        return MarketCode(market)
    except ValueError:
        raise HTTPException(400, f"不支持的市场: {market}")


@router.post("/batch")
def insights_batch(payload: InsightsBatchRequest):
    """聚合返回行情 + K线摘要 + 最新建议"""
    if not payload.items:
        return []

    # 1) 批量行情（按市场）
    market_items: dict[MarketCode, list[str]] = {}
    for it in payload.items:
        market_code = _parse_market(it.market)
        market_items.setdefault(market_code, []).append(it.symbol)

    quotes_by_market: dict[MarketCode, dict[str, dict]] = {}
    for market_code, symbols in market_items.items():
        tencent_symbols = [_tencent_symbol(s, market_code) for s in symbols]
        try:
            items = _fetch_tencent_quotes(tencent_symbols)
        except Exception:
            items = []
        quotes_by_market[market_code] = {item["symbol"]: item for item in items}

    # 2) K线摘要（逐只，带 60s 简易缓存）
    kline_by_symbol: dict[str, dict] = {}
    now = time.time()
    TTL = 60.0
    # module-level cache
    global _KLINE_CACHE
    try:
        _KLINE_CACHE
    except NameError:
        _KLINE_CACHE = {}
    for it in payload.items:
        market_code = _parse_market(it.market)
        cache_key = f"{market_code.value}:{it.symbol}"
        cached = _KLINE_CACHE.get(cache_key)
        summary = None
        if cached and (now - cached[0] < TTL):
            summary = cached[1]
        else:
            try:
                collector = KlineCollector(market_code)
                summary = collector.get_kline_summary(it.symbol)
            except Exception:
                summary = {}
            _KLINE_CACHE[cache_key] = (now, summary)
        kline_by_symbol[cache_key] = summary

    # 3) 最新建议（建议池）
    stock_keys = [(it.symbol, _parse_market(it.market).value) for it in payload.items]
    latest_sugs = get_latest_suggestions(stock_keys=stock_keys, include_expired=False)

    # 4) 合并返回
    results = []
    for it in payload.items:
        market_code = _parse_market(it.market)
        quote = quotes_by_market.get(market_code, {}).get(it.symbol)
        results.append({
            "symbol": it.symbol,
            "market": market_code.value,
            "quote": {
                "name": quote.get("name") if quote else None,
                "current_price": quote.get("current_price") if quote else None,
                "change_pct": quote.get("change_pct") if quote else None,
                "open_price": quote.get("open_price") if quote else None,
                "high_price": quote.get("high_price") if quote else None,
                "low_price": quote.get("low_price") if quote else None,
                "volume": quote.get("volume") if quote else None,
                "turnover": quote.get("turnover") if quote else None,
            },
            "kline_summary": kline_by_symbol.get(f"{market_code.value}:{it.symbol}", {}),
            "suggestion": latest_sugs.get(f"{market_code.value}:{it.symbol}"),
        })

    return results


class AddPositionEvalRequest(BaseModel):
    symbol: str
    market: str = "CN"
    current_quantity: float = Field(0, ge=0, description="当前持仓股数(0=建仓)")
    current_cost: float = Field(0, ge=0, description="当前成本(单价)")
    add_quantity: float = Field(..., gt=0, description="加仓股数")
    add_price: float = Field(..., gt=0, description="加仓价格")
    model_id: int | None = None


_VERDICTS = ("不适合", "谨慎", "适合")  # 先长后短:'不适合' 含 '适合',顺序不能反


def _parse_verdict(text: str) -> str:
    """从 AI 回复粗解析结论标签;命中不到返回'未知'。"""
    head = (text or "")[:120]
    for v in _VERDICTS:
        if v in head:
            return v
    return "未知"


async def _fetch_fundamental_context(symbol: str, market: str) -> str:
    """基本面摘要:PE / 换手率 / 市值 / 今日振幅(取自实时行情,失败返回空)。"""
    try:
        mc = MarketCode(market) if market in ("CN", "HK", "US") else MarketCode.CN
        rows = await asyncio.to_thread(
            _fetch_tencent_quotes, [_tencent_symbol(symbol, mc)]
        )
        if not rows:
            return ""
        q = rows[0]
        parts: list[str] = []
        if q.get("pe_ratio") not in (None, 0):
            parts.append(f"市盈率 {q['pe_ratio']}")
        if q.get("turnover_rate") not in (None, 0):
            parts.append(f"换手率 {q['turnover_rate']}%")
        if q.get("circulating_market_value"):
            parts.append(f"流通市值 {q['circulating_market_value']}亿")
        if q.get("total_market_value"):
            parts.append(f"总市值 {q['total_market_value']}亿")
        hi, lo, pc = q.get("high_price"), q.get("low_price"), q.get("prev_close")
        if hi and lo and pc:
            parts.append(f"今日振幅 {(hi - lo) / pc * 100:.2f}%")
        return ("基本面:" + "，".join(parts)) if parts else ""
    except Exception as e:
        logger.debug(f"基本面获取失败 {symbol}: {e}")
        return ""


async def _fetch_message_context(db: Session, symbol: str, market: str) -> str:
    """消息面摘要:近 3 天新闻/公告标题 + 本地最近 AI 建议/分析(失败降级为空)。"""
    parts: list[str] = []
    try:
        from src.collectors.news_collector import NewsCollector

        stock = db.query(Stock).filter(Stock.symbol == symbol).first()
        name = stock.name if stock else symbol
        collector = NewsCollector.from_database()
        items = await collector.fetch_all(
            symbols=[symbol], since_hours=72, symbol_names={symbol: name}
        )
        items = sorted(items, key=lambda x: x.publish_time, reverse=True)[:5]
        if items:
            lines = [
                f"- {it.title}（{it.publish_time.strftime('%m-%d')}）" for it in items
            ]
            parts.append("近期新闻/公告:\n" + "\n".join(lines))
    except Exception as e:
        logger.debug(f"消息面新闻获取失败 {symbol}: {e}")

    try:
        ctx = _build_stock_context(db, symbol, market)
        if ctx:
            parts.append(ctx)
    except Exception:
        pass

    return "\n\n".join(parts)


@router.post("/add-position-eval")
async def add_position_eval(req: AddPositionEvalRequest, db: Session = Depends(get_db)):
    """加仓快速评估:按服务端口径算摊薄成本 + 让 AI 给 适合/谨慎/不适合 结论。"""
    market = _parse_market(req.market).value
    cur_q = max(0.0, float(req.current_quantity or 0))
    cur_c = max(0.0, float(req.current_cost or 0))
    add_q = float(req.add_quantity)
    add_p = float(req.add_price)
    if add_q <= 0 or add_p <= 0:
        raise HTTPException(400, "加仓股数与价格必须大于 0")

    new_q = cur_q + add_q
    new_cost = (cur_q * cur_c + add_q * add_p) / new_q if new_q > 0 else add_p
    is_add = cur_q > 0 and cur_c > 0
    dilute_abs = (cur_c - new_cost) if is_add else 0.0
    dilute_pct = (dilute_abs / cur_c * 100) if is_add and cur_c > 0 else 0.0
    action = "加仓" if is_add else "建仓"

    # 上下文:实时行情 + 基本面 + 技术面 + 消息面(新闻/公告/本地观点)
    realtime = await _fetch_realtime_context(req.symbol, market)
    fundamental = await _fetch_fundamental_context(req.symbol, market)
    technical = await _fetch_technical_context(req.symbol, market)
    message = await _fetch_message_context(db, req.symbol, market)

    holding_line = (
        f"当前持仓 {cur_q:.0f} 股,成本(单价) {cur_c:.3f}"
        if is_add
        else "当前空仓(本次为建仓)"
    )
    dilute_line = f",较现成本摊薄 {dilute_abs:.3f}({dilute_pct:.2f}%)" if is_add else ""
    user_content = (
        f"标的 {market}:{req.symbol}\n"
        f"{holding_line}\n"
        f"拟{action} {add_q:.0f} 股 @ {add_p:.3f}\n"
        f"{action}后成本(单价) {new_cost:.3f}{dilute_line}\n"
        + (f"{realtime}\n" if realtime else "")
        + (f"{fundamental}\n" if fundamental else "")
        + (f"{technical}\n" if technical else "")
        + (f"{message}\n" if message else "")
        + f"请综合估值/基本面与消息面,评估这次{action}是否合适。"
    )
    system_prompt = (
        "你是谨慎务实的股票交易助手。综合用户给出的持仓、价格、基本面、技术面与消息面信息,"
        f"评估这次{action}是否合适,不臆造数据、不做收益承诺。\n"
        "严格按以下格式输出,简洁:\n"
        "结论: 适合 / 谨慎 / 不适合(三选一)\n"
        "理由:\n- (2~3 条,结合摊薄成本、估值/基本面、技术面与消息面)\n"
        "风险: (一句话最大风险)"
    )

    try:
        client = _get_ai_client(db, req.model_id)
        content = await client.chat(system_prompt, user_content, temperature=0.3)
    except Exception as e:
        raise HTTPException(502, f"AI 评估失败: {e}")

    return {
        "symbol": req.symbol,
        "market": market,
        "action": action,
        "new_cost": round(new_cost, 4),
        "dilute_abs": round(dilute_abs, 4),
        "dilute_pct": round(dilute_pct, 4),
        "total_quantity": new_q,
        "total_invested": round(new_q * new_cost, 2),
        "verdict": _parse_verdict(content),
        "content": content,
    }
