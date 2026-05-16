"""事件日历 Provider:东方财富。

包装现有 `EastMoneyEventsCollector`。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from src.collectors.events_collector import EastMoneyEventsCollector
from src.core.providers.base import EventsProvider, ProviderRequest, ProviderResponse

logger = logging.getLogger(__name__)


class EastmoneyEventsProvider(EventsProvider):
    name = "eastmoney"
    supports_markets = {"CN"}

    def __init__(self, config: dict | None = None):
        super().__init__(config=config)
        cfg = self.config or {}
        self._collector = EastMoneyEventsCollector(
            timeout_s=cfg.get("timeout_s", cfg.get("timeout", 10.0)),
        )

    async def fetch(self, req: ProviderRequest) -> ProviderResponse:
        if not req.symbols:
            return ProviderResponse(success=True, data=[])
        if req.market != "CN":
            return ProviderResponse(success=False, error="eastmoney events 仅支持 CN")

        # since_hours 可选,默认 168h(7 天)
        since = datetime.now() - timedelta(hours=req.since_hours or 168)
        try:
            events = await self._collector.fetch_events(
                symbols=list(req.symbols), since=since
            )
        except Exception as e:
            return ProviderResponse(success=False, error=str(e))

        return ProviderResponse(success=True, data=events or [])
