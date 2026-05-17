/**
 * TradingAgents 深度分析 API。
 * 复用现有 /api/stocks/:id/agents/:name/trigger,只是 agent_name = "tradingagents"。
 * 进度走新增的 /api/agents/runs/:trace_id/progress。
 */
import { fetchAPI } from './client'

export interface TradingAgentsTriggerResult {
  ok: boolean
  queued?: boolean
  trace_id?: string
  message?: string
  /** 后端幂等命中:已有在跑任务,trace_id 是现有任务的,不是新启的 */
  deduplicated?: boolean
}

export interface AnalystReports {
  market: string
  social: string
  news: string
  fundamentals: string
}

export interface DebateHistory {
  history: string
  current_response: string
  judge_decision: string
}

export interface DeepAnalysisSuggestion {
  action: 'buy' | 'hold' | 'sell'
  action_label: string
  signal: string
  reason: string
  should_alert: boolean
  agent_name: string
  agent_label: string
  confidence: number
}

export interface DeepAnalysisResult {
  agent_name: string
  title: string
  content: string
  raw_data: {
    suggestion: DeepAnalysisSuggestion
    cost_usd: number
    should_alert: boolean
    decision: string
    confidence: number
    debate_history: DebateHistory
    risk_judgment: string
    analyst_reports: AnalystReports
    final_decision: string
    trader_plan: string
    from_cache?: boolean
    notified?: boolean
    toolkit_diagnostic?: {
      summary: { hit: number; miss: number; passthrough: number; fallthrough?: number; error: number }
      recent: Array<{
        action?: string
        method?: string
        symbol?: string
        chars?: number
        snippet?: string
        source?: string
        reason?: string
      }>
    }
  }
  timestamp?: string
}

export interface ProgressStage {
  name: string
  status: 'pending' | 'running' | 'done'
  started_at?: string
  duration_sec?: number
  cost_usd?: number
}

export interface ToolkitHit {
  timestamp: string
  action: string  // HIT / MISS / PASSTHROUGH / ERROR
  method: string
  symbol: string
  reason?: string
  chars?: number
}

export interface ProgressResponse {
  trace_id: string
  status: 'not_found' | 'running' | 'success' | 'failed' | 'stale'
  current_stage?: string | null
  completed_stages: string[]
  started_at?: string | null
  elapsed_sec: number
  total_cost_usd: number
  stages: ProgressStage[]
  toolkit_summary?: { hit: number; miss: number; passthrough: number; fallthrough?: number; error: number }
  toolkit_recent?: ToolkitHit[]
  run?: {
    agent_name: string
    status: string
    result: string
    error: string
    duration_ms: number
    model_label: string
    notify_sent: boolean
  }
}

export interface BudgetInfo {
  used: number
  remaining: number
  limit: number
  exceeded: boolean
  runs_this_month: number
  estimate_next_run: {
    cost_low_usd: number
    cost_high_usd: number
    model: string
  }
  over_budget_action: 'reject' | 'warn' | 'continue'
  enabled: boolean
}

export interface HistoryComparisonItem {
  trace_id: string
  analysis_date: string
  action: 'buy' | 'hold' | 'sell'
  action_label: string
  confidence: number | null
  cost_usd: number | null
  price_at_analysis: number | null
  return_1d_pct: number | null
  return_5d_pct: number | null
  return_20d_pct: number | null
  hit_20d: boolean | null
}

export interface HistoryComparisonStats {
  total: number
  buy_count: number
  sell_count: number
  hold_count: number
  buy_hit_rate: number | null
  sell_hit_rate: number | null
  hold_hit_rate: number | null
  overall_hit_rate: number | null
  avg_return_20d_pct: number | null
}

export interface HistoryComparisonResponse {
  items: HistoryComparisonItem[]
  stats: HistoryComparisonStats
}

export const tradingAgentsApi = {
  /** 触发深度分析(异步排队)。force=true 跳过同日缓存。
   *  TradingAgents 不要求 StockAgent 绑定 — 始终带 allow_unbound=true。 */
  trigger(stockId: number, opts: { force?: boolean } = {}): Promise<TradingAgentsTriggerResult> {
    const qsParts = ['allow_unbound=true']
    if (opts.force) qsParts.push('force_refresh=true')
    return fetchAPI(
      `/stocks/${stockId}/agents/tradingagents/trigger?${qsParts.join('&')}`,
      {
        method: 'POST',
        body: JSON.stringify({}),
      },
    )
  },

  /** 读取本月预算 + 单次预估成本(用于触发前确认弹窗)。 */
  getBudget(): Promise<BudgetInfo> {
    return fetchAPI('/agents/tradingagents/budget')
  },

  /** 查某只股票最近 30 分钟有没有在跑或刚完成的 TA 任务(后端权威源)。
   *  返回 status: running | success | failed | none */
  findRunning(symbol: string): Promise<{
    trace_id: string | null
    status: 'running' | 'success' | 'failed' | 'none'
    last_activity_at?: string
  }> {
    return fetchAPI(`/agents/tradingagents/running?stock_symbol=${encodeURIComponent(symbol)}`)
  },

  /** 拉取进度(前端轮询)。 */
  getProgress(traceId: string): Promise<ProgressResponse> {
    return fetchAPI(`/agents/runs/${encodeURIComponent(traceId)}/progress`)
  },

  /** 历史决策 vs 实际涨跌对比。 */
  getHistoryComparison(
    symbol: string,
    market: string,
    days = 90,
  ): Promise<HistoryComparisonResponse> {
    const qs = new URLSearchParams({
      stock_symbol: symbol,
      market,
      days: String(days),
    })
    return fetchAPI(`/agents/tradingagents/history-comparison?${qs.toString()}`)
  },

  /** 拉取某只股票最近一次深度分析结果(含完整 raw_data)。 */
  getLatestForStock(symbol: string): Promise<DeepAnalysisResult | null> {
    return fetchAPI(
      `/agents/tradingagents/latest?stock_symbol=${encodeURIComponent(symbol)}`,
    ).then((item: unknown) => {
      if (!item || typeof item !== 'object') return null
      const rec = item as { content?: string; title?: string; raw_data?: unknown }
      if (!rec.content) return null
      return {
        agent_name: 'tradingagents',
        title: rec.title || '',
        content: rec.content || '',
        raw_data: (rec.raw_data || {}) as DeepAnalysisResult['raw_data'],
      }
    })
  },
}
