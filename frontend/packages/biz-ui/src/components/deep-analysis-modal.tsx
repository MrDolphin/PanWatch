/**
 * 深度分析弹窗(TradingAgents)。
 *
 * 三种状态:
 * 1. 触发中 — 显示「分析需 3-5 分钟,确认开始?」+ 成本预估
 * 2. 运行中 — polling /agents/runs/{trace_id}/progress,显示阶段进度
 * 3. 完成 — 顶层摘要 + Markdown 推理 + 可展开 4 分析师报告 + 辩论
 */
import { useEffect, useState, useCallback, useRef } from 'react'
import ReactMarkdown from 'react-markdown'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@panwatch/base-ui/components/ui/dialog'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import {
  tradingAgentsApi,
  type DeepAnalysisResult,
  type ProgressResponse,
  type ProgressStage,
} from '@panwatch/api'

const STAGE_LABEL: Record<string, string> = {
  market_analyst: '技术分析师',
  social_analyst: '情绪分析师',
  news_analyst: '新闻分析师',
  fundamentals_analyst: '基本面分析师',
  bull_bear_debate: '看多看空辩论',
  research_manager: '研究主管',
  trader: '交易员决策',
  risk_judge: '风控判定',
  final_decision: 'PM 整合',
}

const DECISION_COLOR: Record<string, string> = {
  buy: 'text-emerald-600 dark:text-emerald-400',
  hold: 'text-amber-600 dark:text-amber-400',
  sell: 'text-rose-600 dark:text-rose-400',
}

const POLL_INTERVAL_MS = 2000

export interface DeepAnalysisModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  stockId: number
  stockName: string
  stockSymbol: string
  /** 历史分析(若有,直接展示) */
  initialResult?: DeepAnalysisResult | null
}

export function DeepAnalysisModal({
  open,
  onOpenChange,
  stockId,
  stockName,
  stockSymbol,
  initialResult = null,
}: DeepAnalysisModalProps) {
  const { toast } = useToast()
  const [stage, setStage] = useState<'idle' | 'running' | 'done' | 'error'>('idle')
  const [traceId, setTraceId] = useState<string | null>(null)
  const [progress, setProgress] = useState<ProgressResponse | null>(null)
  const [result, setResult] = useState<DeepAnalysisResult | null>(initialResult)
  const [error, setError] = useState<string>('')
  const [showAnalystDetails, setShowAnalystDetails] = useState(false)
  const [showDebate, setShowDebate] = useState(false)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // 弹窗关闭时清理 polling
  useEffect(() => {
    if (!open) {
      if (timerRef.current) {
        clearInterval(timerRef.current)
        timerRef.current = null
      }
    }
  }, [open])

  // 重置初始状态
  useEffect(() => {
    if (open) {
      if (initialResult) {
        setResult(initialResult)
        setStage('done')
      } else {
        setStage('idle')
        setResult(null)
        setError('')
        setProgress(null)
        setTraceId(null)
      }
    }
  }, [open, initialResult])

  const pollProgress = useCallback(
    async (tid: string) => {
      try {
        const resp = await tradingAgentsApi.getProgress(tid)
        setProgress(resp)
        if (resp.status === 'success' && resp.run) {
          // 完成,拉历史结果
          if (timerRef.current) {
            clearInterval(timerRef.current)
            timerRef.current = null
          }
          const latest = await tradingAgentsApi.getLatestForStock(stockSymbol)
          if (latest) {
            setResult(latest)
            setStage('done')
          } else {
            setError('结果未落库,请稍后到「AI 历史」查看')
            setStage('error')
          }
        } else if (resp.status === 'failed') {
          if (timerRef.current) {
            clearInterval(timerRef.current)
            timerRef.current = null
          }
          setError(resp.run?.error || '分析失败')
          setStage('error')
        }
      } catch (e) {
        // polling 失败不立即终止,记一次错误
        console.warn('progress poll error:', e)
      }
    },
    [stockSymbol],
  )

  const handleStart = useCallback(async () => {
    setStage('running')
    setError('')
    setProgress(null)
    try {
      const triggerResp = await tradingAgentsApi.trigger(stockId)
      const tid = triggerResp.trace_id || ''
      setTraceId(tid)
      if (!tid) {
        // 后端未返回 trace_id,只显示 message
        setStage('done')
        toast(triggerResp.message || '已触发', 'success')
        return
      }
      // 启动 polling
      timerRef.current = setInterval(() => {
        pollProgress(tid)
      }, POLL_INTERVAL_MS)
      // 立即拉一次
      pollProgress(tid)
    } catch (e) {
      setStage('error')
      setError(e instanceof Error ? e.message : '触发失败')
    }
  }, [stockId, pollProgress, toast])

  const handleClose = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current)
      timerRef.current = null
    }
    onOpenChange(false)
  }, [onOpenChange])

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl w-[92vw] max-h-[85vh] overflow-y-auto scrollbar">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            🧠 深度分析 · {stockName} ({stockSymbol})
          </DialogTitle>
          <DialogDescription>
            TradingAgents 多 Agent 决策框架 · 仅供学习研究参考,不构成投资建议
          </DialogDescription>
        </DialogHeader>

        {stage === 'idle' && (
          <IdleView
            stockSymbol={stockSymbol}
            onStart={handleStart}
            onCancel={handleClose}
          />
        )}

        {stage === 'running' && (
          <RunningView progress={progress} traceId={traceId || ''} onClose={handleClose} />
        )}

        {stage === 'done' && result && <DoneView
          result={result}
          showAnalystDetails={showAnalystDetails}
          setShowAnalystDetails={setShowAnalystDetails}
          showDebate={showDebate}
          setShowDebate={setShowDebate}
        />}

        {stage === 'error' && (
          <div className="space-y-3 text-[13px]">
            <div className="rounded-lg bg-rose-500/10 border border-rose-500/30 p-3 text-rose-600">
              <div className="font-semibold mb-1">分析失败</div>
              <div className="text-[12px]">{error}</div>
            </div>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={handleClose}>关闭</Button>
              <Button onClick={handleStart}>重试</Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

function IdleView({
  stockSymbol,
  onStart,
  onCancel,
}: {
  stockSymbol: string
  onStart: () => void
  onCancel: () => void
}) {
  return (
    <div className="space-y-4 text-[13px]">
      <div className="rounded-lg bg-accent/30 p-3 space-y-1.5">
        <div className="font-medium">即将分析:{stockSymbol}</div>
        <div className="text-muted-foreground">
          调用 4 类分析师(技术 / 情绪 / 新闻 / 基本面) + 看多看空辩论 + 风控 + PM 整合
        </div>
        <div className="text-[11px] text-muted-foreground mt-2">
          ⏱ 预计耗时:3-5 分钟<br />
          💰 预估成本:$0.02 - $0.05 (deepseek-chat)<br />
          ℹ️ 异步执行,可关闭弹窗,完成时通过通知渠道推送
        </div>
      </div>
      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={onCancel}>取消</Button>
        <Button onClick={onStart}>开始分析</Button>
      </div>
    </div>
  )
}

function RunningView({
  progress,
  traceId,
  onClose,
}: {
  progress: ProgressResponse | null
  traceId: string
  onClose: () => void
}) {
  const elapsed = progress?.elapsed_sec ?? 0
  const cost = progress?.total_cost_usd ?? 0
  const stages = progress?.stages ?? []

  return (
    <div className="space-y-4 text-[13px]">
      <div className="rounded-lg bg-accent/30 p-3 space-y-2">
        <div className="flex items-center gap-2">
          <span className="inline-block w-3 h-3 rounded-full bg-primary animate-pulse" />
          <span className="font-medium">分析进行中...</span>
          <span className="ml-auto text-[11px] text-muted-foreground">
            已用 {formatElapsed(elapsed)} · ${cost.toFixed(4)}
          </span>
        </div>
        <div className="space-y-1 mt-3">
          {stages.length > 0 ? stages.map((s) => (
            <StageRow key={s.name} stage={s} />
          )) : (
            <div className="text-[12px] text-muted-foreground">准备中...</div>
          )}
        </div>
        <div className="text-[10px] text-muted-foreground/70 mt-3 font-mono">
          trace_id: {traceId.slice(0, 16)}...
        </div>
      </div>
      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={onClose}>
          后台运行 (完成时推送通知)
        </Button>
      </div>
    </div>
  )
}

function StageRow({ stage }: { stage: ProgressStage }) {
  const label = STAGE_LABEL[stage.name] || stage.name
  const icon =
    stage.status === 'done' ? '✓' : stage.status === 'running' ? '🔄' : '⏸'
  const cls =
    stage.status === 'done'
      ? 'text-emerald-600 dark:text-emerald-400'
      : stage.status === 'running'
      ? 'text-primary'
      : 'text-muted-foreground/60'
  return (
    <div className={`flex items-center gap-2 text-[12px] ${cls}`}>
      <span className="w-4">{icon}</span>
      <span>{label}</span>
      {stage.cost_usd ? (
        <span className="ml-auto text-[10px] opacity-70 font-mono">
          ${stage.cost_usd.toFixed(4)}
        </span>
      ) : null}
    </div>
  )
}

function DoneView({
  result,
  showAnalystDetails,
  setShowAnalystDetails,
  showDebate,
  setShowDebate,
}: {
  result: DeepAnalysisResult
  showAnalystDetails: boolean
  setShowAnalystDetails: (v: boolean) => void
  showDebate: boolean
  setShowDebate: (v: boolean) => void
}) {
  const sug = result.raw_data.suggestion
  const reports = result.raw_data.analyst_reports || {}
  const debate = result.raw_data.debate_history
  const fromCache = result.raw_data.from_cache

  return (
    <div className="space-y-4 text-[13px]">
      {fromCache && (
        <div className="rounded-lg bg-amber-500/10 border border-amber-500/30 p-2 text-[12px] text-amber-700 dark:text-amber-400">
          ℹ️ 当日缓存:今天已经分析过这只股票,展示缓存结果(无新成本)
        </div>
      )}

      {/* 顶层摘要 */}
      <div className="rounded-lg bg-accent/30 p-4 space-y-2">
        <div className="flex items-center gap-3">
          <span className={`text-[22px] font-bold ${DECISION_COLOR[sug.action] || ''}`}>
            {sug.action_label}
          </span>
          <span className="text-[12px] text-muted-foreground">
            置信度 {sug.confidence?.toFixed(1) ?? '-'} / 10
          </span>
        </div>
        <div className="text-[12px] text-foreground/80">{sug.reason?.slice(0, 200)}</div>
        <div className="flex items-center gap-3 text-[10px] text-muted-foreground mt-2">
          <span>成本:${result.raw_data.cost_usd?.toFixed(4) ?? '-'}</span>
        </div>
      </div>

      {/* Markdown 推理 */}
      <div className="rounded-lg border border-border/50 p-4">
        <div className="prose prose-sm dark:prose-invert max-w-none">
          <ReactMarkdown>{result.content}</ReactMarkdown>
        </div>
      </div>

      {/* 分析师报告(可展开) */}
      <div>
        <button
          className="text-[12px] text-muted-foreground hover:text-foreground flex items-center gap-1"
          onClick={() => setShowAnalystDetails(!showAnalystDetails)}
        >
          {showAnalystDetails ? '▼' : '▶'} 4 位分析师报告
        </button>
        {showAnalystDetails && (
          <div className="space-y-3 mt-2 pl-3 border-l-2 border-border/40">
            {(['market', 'social', 'news', 'fundamentals'] as const).map((k) => {
              const text = (reports as unknown as Record<string, string>)[k] || ''
              if (!text) return null
              return (
                <details key={k} open className="text-[12px]">
                  <summary className="font-medium cursor-pointer">
                    {STAGE_LABEL[`${k}_analyst`] || k}
                  </summary>
                  <div className="mt-2 text-[11px] text-foreground/80 whitespace-pre-wrap">
                    {text.slice(0, 1500)}
                    {text.length > 1500 && '... (截断)'}
                  </div>
                </details>
              )
            })}
          </div>
        )}
      </div>

      {/* 辩论历史(可展开) */}
      {debate && debate.history && (
        <div>
          <button
            className="text-[12px] text-muted-foreground hover:text-foreground flex items-center gap-1"
            onClick={() => setShowDebate(!showDebate)}
          >
            {showDebate ? '▼' : '▶'} 看多看空辩论
          </button>
          {showDebate && (
            <div className="mt-2 pl-3 border-l-2 border-border/40 text-[11px] text-foreground/80 whitespace-pre-wrap max-h-96 overflow-y-auto">
              {debate.history}
              {debate.judge_decision && (
                <>
                  <div className="font-medium mt-3 mb-1">研究主管裁决:</div>
                  <div>{debate.judge_decision}</div>
                </>
              )}
            </div>
          )}
        </div>
      )}

      {/* 免责声明 */}
      <div className="text-[10px] text-muted-foreground/70 italic border-t border-border/30 pt-2">
        本分析由 AI 多 Agent 框架生成,仅供学习研究参考,不构成任何投资建议。
        投资有风险,决策需自主判断。
      </div>
    </div>
  )
}

function formatElapsed(sec: number): string {
  if (sec < 60) return `${sec.toFixed(0)}s`
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return `${m}m${s.toString().padStart(2, '0')}s`
}
