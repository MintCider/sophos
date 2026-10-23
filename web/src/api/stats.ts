import api from '@/api'

export type StatsRange = 'today' | 'week' | 'month'
export type RecordKind = 'usage' | 'trigger' | 'errors' | 'tools'

export interface UsageTotals {
  calls: number
  failures: number
  missing_usage: number
  input_tokens: number
  output_tokens: number
  cached_tokens: number
  cache_write_tokens: number
  reasoning_tokens: number
  tokens: number
}

export interface StatsData {
  range: StatsRange
  start: string
  end: string
  timezone: string
  totals: UsageTotals & { messages: number; replies: number; active_conversations: number; errors: number }
  status: { connected_accounts: number[] | null; running: number; recognizing: number; last_received: string | null }
  slots: (UsageTotals & { slot: string })[]
  models: (UsageTotals & { provider: string; model: string })[]
  tools: { provider: string; tool: string; calls: number; failures: number; credits: number | null; credit_calls: number; missing_credits: number }[]
  trend: { at: string; messages: number; replies: number; tokens: number }[]
  conversations: { id: number; name: string; kind: string; messages: number; replies: number; tokens: number; last_active: string | null }[]
  trigger: {
    simple: number; evaluations: number; irrelevant: number; relevant_more: number; relevant_done: number
    invalid: number; incomplete: number; rate_limited: number; locked: number; replied: number; average_ms: number | null
  }
  errors: { conversation_id: number | null; conversation_name: string; stage: string; error: string; count: number; last_at: string }[]
}

export interface StatsRecord {
  id: number
  conversation_id: number | null
  conversation_name: string
  run_id: string | null
  created_at: string
  slot?: string
  provider?: string
  model?: string
  success?: boolean
  input_tokens?: number | null
  output_tokens?: number | null
  cached_tokens?: number | null
  cache_write_tokens?: number | null
  reasoning_tokens?: number | null
  total_tokens?: number | null
  duration_ms?: number | null
  message_id?: number | null
  source?: string
  result?: string | null
  raw_result?: string | null
  status?: string
  waiting?: boolean
  sent_count?: number
  stage?: string
  error?: string
  tool?: string
  credits?: number | null
}

export function getStats(range: StatsRange, conversation: string, signal: AbortSignal) {
  return api.get<StatsData>('/stats', { params: params(range, conversation), signal })
}

export function getConversations(signal: AbortSignal) {
  return api.get<{ items: { id: number; name: string; kind: string }[] }>('/stats/conversations', { signal })
}

export function getRecords(kind: RecordKind, range: StatsRange, conversation: string, signal: AbortSignal, before?: number) {
  return api.get<{ items: StatsRecord[]; next: number | null }>(`/stats/${kind}`, {
    params: { ...params(range, conversation), before }, signal,
  })
}

function params(range: StatsRange, conversation: string) {
  return { range, conversation_id: conversation === 'all' ? undefined : conversation }
}
