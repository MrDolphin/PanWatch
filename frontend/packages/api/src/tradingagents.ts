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

export interface ProgressResponse {
  trace_id: string
  status: 'not_found' | 'running' | 'success' | 'failed'
  current_stage?: string | null
  completed_stages: string[]
  started_at?: string | null
  elapsed_sec: number
  total_cost_usd: number
  stages: ProgressStage[]
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

export const tradingAgentsApi = {
  /** 触发深度分析(异步排队)。 */
  trigger(stockId: number): Promise<TradingAgentsTriggerResult> {
    return fetchAPI(`/stocks/${stockId}/agents/tradingagents/trigger`, {
      method: 'POST',
      body: JSON.stringify({}),
    })
  },

  /** 拉取进度(前端轮询)。 */
  getProgress(traceId: string): Promise<ProgressResponse> {
    return fetchAPI(`/agents/runs/${encodeURIComponent(traceId)}/progress`)
  },

  /** 拉取最近的深度分析历史(走通用 history 接口)。 */
  getLatestForStock(symbol: string): Promise<DeepAnalysisResult | null> {
    return fetchAPI(
      `/history?agent_name=tradingagents&stock_symbol=${encodeURIComponent(symbol)}&limit=1`,
    ).then((items: unknown) => {
      if (Array.isArray(items) && items.length > 0) {
        const item = items[0] as { content: string; title: string; raw_data: unknown }
        return {
          agent_name: 'tradingagents',
          title: item.title,
          content: item.content,
          raw_data: item.raw_data as DeepAnalysisResult['raw_data'],
        }
      }
      return null
    })
  },
}
