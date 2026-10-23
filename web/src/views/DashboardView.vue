<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { NAlert, NButton, NDrawer, NDrawerContent, NSelect, NSpin, NTag } from 'naive-ui'
import { Activity, ArrowUpRight, Bot, Check, ChevronRight, CircleAlert, Cpu, MessageSquare, Radio, RefreshCw, Zap } from 'lucide-vue-next'
import { getConversations, getRecords, getStats, type RecordKind, type StatsData, type StatsRange, type StatsRecord } from '@/api/stats'

const range = ref<StatsRange>('today')
const conversation = ref('all')
const chartMode = ref('messages')
const detail = ref<RecordKind | null>(null)
const ranges: { label: string; value: StatsRange }[] = [{ label: '今天', value: 'today' }, { label: '7 天', value: 'week' }, { label: '30 天', value: 'month' }]
const data = ref<StatsData | null>(null)
const loading = ref(false)
const loadError = ref('')
const conversationOptions = ref([{ label: '全部会话', value: 'all' }])
const records = ref<StatsRecord[]>([])
const recordsLoading = ref(false)
const recordsError = ref('')
const nextCursor = ref<number | null>(null)
let controller: AbortController | null = null
let recordsController: AbortController | null = null

async function load() {
  controller?.abort()
  const current = new AbortController()
  controller = current
  loading.value = true
  try {
    const [stats, conversations] = await Promise.all([
      getStats(range.value, conversation.value, current.signal),
      getConversations(current.signal),
    ])
    if (current.signal.aborted) return
    data.value = stats.data
    conversationOptions.value = [{ label: '全部会话', value: 'all' }, ...conversations.data.items.map(c => ({ label: c.name, value: String(c.id) }))]
    loadError.value = ''
  } catch {
    if (!current.signal.aborted) loadError.value = '统计加载失败，请重试。已有数据未更新。'
  } finally {
    if (!current.signal.aborted) loading.value = false
  }
}

async function loadRecords(append = false) {
  recordsController?.abort()
  if (!detail.value) return
  const current = new AbortController()
  recordsController = current
  recordsLoading.value = true
  recordsError.value = ''
  try {
    const response = await getRecords(detail.value, range.value, conversation.value, current.signal, append ? nextCursor.value ?? undefined : undefined)
    if (current.signal.aborted) return
    records.value = append ? [...records.value, ...response.data.items] : response.data.items
    nextCursor.value = response.data.next
  } catch {
    if (!current.signal.aborted) recordsError.value = '记录加载失败，请重试。'
  } finally {
    if (!current.signal.aborted) recordsLoading.value = false
  }
}

watch([range, conversation], () => {
  data.value = null
  void load()
}, { immediate: true })
watch([detail, range, conversation], () => {
  records.value = []
  nextCursor.value = null
  void loadRecords()
})
const refreshTimer = window.setInterval(() => { if (!document.hidden) void load() }, 30000)
onUnmounted(() => { controller?.abort(); recordsController?.abort(); window.clearInterval(refreshTimer) })

const format = (n: number) => n.toLocaleString('en-US')
const compact = (n: number) => n >= 1000000 ? `${(n / 1000000).toFixed(2)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}K` : format(n)
function timeLabel(value: string | null, date = false) {
  if (!value) return '暂无'
  return new Intl.DateTimeFormat('zh-CN', { timeZone: data.value?.timezone, ...(date ? { month: '2-digit', day: '2-digit' } as const : {}), hour: '2-digit', minute: '2-digit' }).format(new Date(value))
}
function relativeTime(value: string | null) {
  if (!value) return '暂无'
  const minutes = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 60000))
  return minutes < 1 ? '刚刚' : minutes < 60 ? `${minutes} 分钟前` : minutes < 1440 ? `${Math.floor(minutes / 60)} 小时前` : timeLabel(value, true)
}
const totals = computed(() => data.value?.totals ?? { messages: 0, replies: 0, tokens: 0, calls: 0, errors: 0, active_conversations: 0, failures: 0, input_tokens: 0, output_tokens: 0, cached_tokens: 0, cache_write_tokens: 0, reasoning_tokens: 0, missing_usage: 0 })
const palette = ['#8875e8', '#73a4dc', '#65b8a0', '#d6ad6e']
const rows = computed(() => (data.value?.conversations ?? []).map(g => ({ ...g, initials: Array.from(g.name)[0], color: palette[g.id % palette.length], active: relativeTime(g.last_active) })))
const slotLabels: Record<string, string> = { actor: '对话回复', trigger: '触发判断', collector: '信息收集', vision: '图片识别', image: '绘图', embedding: '向量嵌入' }
const slotColors: Record<string, string> = { actor: '#8875e8', trigger: '#73a4dc', collector: '#65b8a0', vision: '#d6ad6e', image: '#ce87b4', embedding: '#6ca8a6' }
const slots = computed(() => (data.value?.slots ?? []).map((s, i) => ({ ...s, name: s.slot, label: slotLabels[s.slot] ?? s.slot, share: totals.value.tokens ? s.tokens / totals.value.tokens * 100 : 0, color: slotColors[s.slot] ?? palette[i % palette.length] })))
const toolLabels: Record<string, string> = { web_search: '搜索', web_fetch: '网页提取' }
const tavily = computed(() => ['web_search', 'web_fetch'].map(tool => {
  const usage = data.value?.tools.find(t => t.provider === 'tavily' && t.tool === tool)
  return { tool, label: toolLabels[tool], calls: usage?.calls ?? 0, failures: usage?.failures ?? 0, credits: usage?.credits ?? null, missing: usage?.missing_credits ?? 0 }
}))
const metrics = computed(() => [
  { label: '收到消息', value: format(totals.value.messages), note: `${totals.value.active_conversations} 个活跃会话`, icon: MessageSquare, color: 'purple' },
  { label: 'Bot 发言', value: format(totals.value.replies), icon: Bot, color: 'blue' },
  { label: 'Token 用量', value: compact(totals.value.tokens), note: `输入 ${compact(totals.value.input_tokens)} / 输出 ${compact(totals.value.output_tokens)}`, icon: Zap, color: 'green', action: 'usage' },
  { label: '模型调用', value: format(totals.value.calls), note: `${totals.value.failures} 次失败`, icon: Cpu, color: 'amber', action: 'usage' },
])
const evaluations = computed(() => data.value?.trigger.evaluations ?? 0)
const results = computed(() => [
  { label: '不参与', count: data.value?.trigger.irrelevant ?? 0, color: '#a8a1c2' },
  { label: '继续观察', count: data.value?.trigger.relevant_more ?? 0, color: '#73a4dc' },
  { label: '参与对话', count: data.value?.trigger.relevant_done ?? 0, color: '#8875e8' },
  { label: '无效 / 未完成', count: (data.value?.trigger.invalid ?? 0) + (data.value?.trigger.incomplete ?? 0), color: '#d6ad6e' },
])
const errors = computed(() => (data.value?.errors ?? []).map(e => ({ ...e, title: `${e.stage} 调用异常`, context: `${e.conversation_name} · ${e.error}`, time: timeLabel(e.last_at, range.value !== 'today') })))
const bars = computed(() => {
  const trend = data.value?.trend ?? []
  const max = Math.max(1, ...trend.map(t => chartMode.value === 'messages' ? Math.max(t.messages, t.replies) : t.tokens))
  return trend.map(t => ({
    label: new Intl.DateTimeFormat('zh-CN', { timeZone: data.value?.timezone, ...(range.value === 'today' ? { hour: '2-digit', minute: '2-digit' } as const : { month: '2-digit', day: '2-digit' } as const) }).format(new Date(t.at)),
    value: chartMode.value === 'messages' ? t.messages : t.tokens,
    replies: t.replies,
    height: (chartMode.value === 'messages' ? t.messages : t.tokens) / max * 100,
    replyHeight: t.replies / max * 100,
  }))
})
const chartTicks = computed(() => range.value === 'today' ? ['00:00', '06:00', '12:00', '18:00', '23:00'] : [bars.value[0]?.label, bars.value[Math.floor(bars.value.length / 2)]?.label, bars.value.at(-1)?.label])
const drawerTitle = computed(() => detail.value === 'trigger' ? 'Trigger 评估记录' : detail.value === 'errors' ? '最近异常' : detail.value === 'tools' ? 'Tavily 调用记录' : '模型用量明细')
const sourceLabels: Record<string, string> = { simple: '简易触发', llm: 'LLM 评估', timeout: '等待超时触发' }
const statusLabels: Record<string, string> = { pending: '等待执行', ignored: '不参与', waiting: '继续观察', responding: '正在回复', replied: '已发送', no_reply: '未发送', rate_limited: '被限流', locked: '会话锁占用', failed: '失败', coalesced: '合并至待执行触发', invalid: '结果格式无效', interrupted: '重启中断', reevaluated: '已重新评估', waiting_timeout: '等待超时' }
</script>

<template>
  <div class="dashboard">
    <header class="page-header">
      <div class="heading"><h1>仪表盘</h1></div>
      <div class="filters">
        <div class="segmented" aria-label="时间范围">
          <button v-for="item in ranges" :key="item.value" :class="{ active: range === item.value }" :aria-pressed="range === item.value" @click="range = item.value">{{ item.label }}</button>
        </div>
        <NSelect v-model:value="conversation" :options="conversationOptions" class="conversation-select" aria-label="筛选会话" />
        <NButton quaternary circle :loading="loading" aria-label="刷新统计" @click="load"><template #icon><RefreshCw :size="16" /></template></NButton>
      </div>
    </header>

    <NAlert v-if="loadError" type="error" :bordered="false">{{ loadError }} <button class="text-button" @click="load">重试</button></NAlert>
    <div v-if="!data" class="initial-loading glass-panel"><NSpin v-if="loading" size="small" /><span>{{ loading ? '正在加载统计…' : '暂无可用统计' }}</span></div>
    <template v-if="data">
    <div class="status-strip glass-panel">
      <span class="status-item"><span class="status-dot" :class="{ offline: !data.status.connected_accounts?.length }" />{{ data.status.connected_accounts === null ? '连接状态未知' : data.status.connected_accounts.length ? '机器人在线' : '机器人离线' }}<span v-if="data.status.connected_accounts?.length" class="muted">{{ data.status.connected_accounts.length }} 个连接账号</span></span>
      <span class="status-item"><Activity :size="14" />{{ data.status.running }} 个回复任务 · {{ data.status.recognizing }} 条消息富化中</span>
      <span class="status-item muted" :title="timeLabel(data.status.last_received, true)">最近收信：{{ relativeTime(data.status.last_received) }}</span>
      <span class="demo-note">{{ data.timezone }} · {{ timeLabel(data.end) }} 更新</span>
    </div>
    <NAlert v-if="totals.missing_usage" type="warning" :bordered="false">{{ totals.missing_usage }} 次成功调用未返回 Token 用量，未计入消耗。</NAlert>

    <section class="metrics" aria-label="关键指标">
      <article v-for="metric in metrics" :key="metric.label" class="metric glass-panel">
        <div class="metric-top"><span>{{ metric.label }}</span><span class="metric-icon" :class="metric.color"><component :is="metric.icon" :size="18" /></span></div>
        <strong class="metric-value">{{ metric.value }}</strong>
        <span v-if="metric.note" class="metric-note">{{ metric.note }}</span>
        <button v-if="metric.action" class="metric-link" :aria-label="`查看${metric.label}明细`" @click="detail = 'usage'"><ArrowUpRight :size="14" /></button>
      </article>
    </section>

    <div class="overview-grid">
      <section class="panel glass-panel trend-panel">
        <div class="panel-heading">
          <h2>活动趋势</h2>
          <div class="segmented small" aria-label="趋势类型">
            <button :class="{ active: chartMode === 'messages' }" :aria-pressed="chartMode === 'messages'" @click="chartMode = 'messages'">聊天活动</button>
            <button :class="{ active: chartMode === 'tokens' }" :aria-pressed="chartMode === 'tokens'" @click="chartMode = 'tokens'">Token</button>
          </div>
        </div>
        <div class="chart-caption"><span class="legend"><i class="legend-dot purple-dot" />{{ chartMode === 'messages' ? '收到消息' : 'Token 用量' }}</span><span v-if="chartMode === 'messages'" class="legend"><i class="legend-dot blue-dot" />Bot 发言</span><span class="chart-unit">{{ range === 'today' ? '按小时' : '按天' }} · {{ chartMode === 'messages' ? '条' : 'token' }}</span></div>
        <div class="chart" role="img" :aria-label="`${chartMode === 'messages' ? '聊天活动' : 'Token 用量'}趋势柱状图`">
          <div class="chart-grid"><span /><span /><span /><span /></div>
          <div v-for="bar in bars" :key="bar.label" class="bar-group" tabindex="0" :aria-label="`${bar.label}：${format(bar.value)}`">
            <div class="bar primary-bar" :style="{ height: `${bar.height}%` }" />
            <div v-if="chartMode === 'messages'" class="bar secondary-bar" :style="{ height: `${bar.replyHeight}%` }" />
            <span class="bar-tooltip">{{ bar.label }}<br>{{ format(bar.value) }} {{ chartMode === 'messages' ? '条消息' : 'token' }}<template v-if="chartMode === 'messages'"><br>Bot 发言 {{ format(bar.replies) }} 条</template></span>
          </div>
        </div>
        <div class="chart-ticks"><span v-for="tick in chartTicks" :key="tick">{{ tick }}</span></div>
      </section>

      <section class="panel glass-panel usage-panel">
        <div class="panel-heading"><h2>资源用量</h2></div>
        <div class="resource-heading"><h3>模型</h3><button class="text-button" @click="detail = 'usage'">明细 <ArrowUpRight :size="14" /></button></div>
        <div class="usage-total"><strong>{{ compact(totals.tokens) }}</strong><span class="muted">tokens</span></div>
        <div class="stacked-bar"><span v-for="slot in slots" :key="slot.name" :style="{ width: `${slot.share}%`, background: slot.color }" /></div>
        <p v-if="!slots.length" class="muted empty-copy">所选范围暂无模型调用</p>
        <div class="slot-list">
          <div v-for="slot in slots" :key="slot.name" class="slot-row">
            <span class="slot-name"><i class="legend-dot" :style="{ background: slot.color }" />{{ slot.name }}<span class="slot-label">{{ slot.label }}</span></span>
            <span class="slot-value">{{ slot.missing_usage && !slot.tokens ? '—' : compact(slot.tokens) }}<span class="muted">{{ slot.missing_usage && !slot.tokens ? '' : `${slot.share.toFixed(0)}%` }}</span></span>
          </div>
        </div>
        <div class="usage-footnote">缓存命中 {{ compact(totals.cached_tokens) }}</div>
        <div class="tavily-usage">
          <div class="resource-heading"><h3>Tavily</h3><button class="text-button" @click="detail = 'tools'">明细 <ArrowUpRight :size="14" /></button></div>
          <div v-for="tool in tavily" :key="tool.tool" class="tool-usage-row">
            <span>{{ tool.label }}</span>
            <span class="tool-values"><span>{{ format(tool.calls) }} 次</span><span v-if="tool.failures" class="tool-failures">{{ tool.failures }} 失败</span><span v-if="tool.credits !== null">{{ tool.missing ? '≥ ' : '' }}{{ format(tool.credits) }} credits</span></span>
          </div>
        </div>
      </section>
    </div>

    <section class="panel glass-panel conversations-panel">
      <div class="panel-heading"><h2>会话概览 <span class="heading-count">{{ rows.length }}</span></h2></div>
      <div class="table-scroll">
        <table>
          <thead><tr><th>会话</th><th>收到消息</th><th>Bot 发言</th><th>Token 用量</th><th>最近活跃</th><th><span class="sr-only">操作</span></th></tr></thead>
          <tbody><tr v-for="group in rows" :key="group.id">
            <td><span class="group-name"><span class="avatar" :style="{ background: `${group.color}20`, color: group.color }">{{ group.initials }}</span>{{ group.name }}</span></td>
            <td>{{ format(group.messages) }}</td><td>{{ format(group.replies) }}</td>
            <td><span class="table-usage"><span>{{ compact(group.tokens) }}</span><span class="mini-track"><span :style="{ width: `${totals.tokens ? group.tokens / totals.tokens * 100 : 0}%` }" /></span></span></td>
            <td class="muted">{{ group.active }}</td>
            <td><button class="text-button" :aria-label="`查看${group.name}用量`" @click="conversation = String(group.id); detail = 'usage'"><ChevronRight :size="16" /></button></td>
          </tr></tbody>
        </table>
      </div>
      <p v-if="!rows.length" class="muted empty-copy">所选范围暂无会话活动</p>
    </section>

    <div class="bottom-grid">
      <section class="panel glass-panel trigger-panel">
        <div class="panel-heading"><h2><Radio :size="17" />Trigger</h2><button class="text-button" @click="detail = 'trigger'">评估记录 <ArrowUpRight :size="14" /></button></div>
        <div class="trigger-metrics">
          <div><strong>{{ format(data.trigger.simple) }}</strong><span>简易触发</span></div>
          <div><strong>{{ format(evaluations) }}</strong><span>LLM 评估</span></div>
          <div><strong>{{ data.trigger.average_ms === null ? '—' : Math.round(data.trigger.average_ms) }} <small v-if="data.trigger.average_ms !== null">ms</small></strong><span>平均评估耗时</span></div>
        </div>
        <div class="stacked-bar result-bar"><span v-for="result in results" :key="result.label" :style="{ width: `${evaluations ? result.count / evaluations * 100 : 0}%`, background: result.color }" /></div>
        <div class="result-labels"><span v-for="result in results" :key="result.label"><i class="legend-dot" :style="{ background: result.color }" />{{ result.label }}<strong>{{ format(result.count) }}</strong></span></div>
        <div class="trigger-footer"><span>限流跳过 <b>{{ data.trigger.rate_limited }}</b></span><span>锁占用跳过 <b>{{ data.trigger.locked }}</b></span><span class="muted">实际发言 {{ data.trigger.replied }} 次触发</span></div>
      </section>

      <section class="panel glass-panel errors-panel">
        <div class="panel-heading"><h2>最近异常 <span class="error-count">{{ totals.errors }}</span></h2><button v-if="errors.length" class="text-button" @click="detail = 'errors'">查看详情 <ArrowUpRight :size="14" /></button></div>
        <div v-if="!errors.length" class="empty-state"><Check :size="24" /><span>所选会话暂无异常</span></div>
        <button v-for="error in errors" :key="`${error.conversation_id}:${error.stage}:${error.error}`" class="error-row" @click="detail = 'errors'">
          <CircleAlert :size="17" class="error-icon" /><span class="error-content"><span class="error-title">{{ error.title }}<span class="repeat-count">×{{ error.count }}</span></span><span class="error-context">{{ error.context }}</span></span><time>{{ error.time }}</time>
        </button>
      </section>
    </div>
    </template>

    <NDrawer :show="detail !== null" width="min(520px, 100vw)" @update:show="value => { if (!value) detail = null }">
      <NDrawerContent :title="drawerTitle" closable>
        <NTag :bordered="false" size="small">{{ ranges.find(r => r.value === range)?.label }} · {{ conversationOptions.find(c => c.value === conversation)?.label }}</NTag>
        <template v-if="detail === 'usage'">
          <div v-for="model in data?.models" :key="`${model.provider}:${model.model}`" class="detail-card"><h3>{{ model.model }}</h3><p>{{ model.provider }} · {{ model.calls }} 次调用 · {{ model.failures }} 次失败</p><strong>{{ format(model.tokens) }} tokens</strong><p class="muted">输入 {{ format(model.input_tokens) }} / 输出 {{ format(model.output_tokens) }} · 缓存命中 {{ format(model.cached_tokens) }}</p><p v-if="model.missing_usage" class="muted">{{ model.missing_usage }} 次成功调用未返回用量</p></div>
        </template>
        <template v-if="detail === 'tools'">
          <div v-for="tool in tavily" :key="tool.tool" class="detail-card"><h3>{{ tool.label }}</h3><p>{{ format(tool.calls) }} 次请求 · {{ format(tool.failures) }} 次失败</p><p v-if="tool.credits !== null">{{ tool.missing ? '≥ ' : '' }}{{ format(tool.credits) }} credits</p></div>
        </template>
        <NAlert v-if="recordsError" type="error" :bordered="false">{{ recordsError }} <button class="text-button" @click="loadRecords(false)">重试</button></NAlert>
        <div v-for="record in records" :key="record.id" class="detail-card">
          <p class="muted">{{ timeLabel(record.created_at, true) }} · {{ record.conversation_name }}</p>
          <template v-if="detail === 'usage'">
            <h3>{{ record.slot }} · {{ record.model }} <NTag size="small" :bordered="false" :type="record.success ? 'success' : 'error'">{{ record.success ? '成功' : '失败' }}</NTag></h3>
            <p>{{ record.provider }} · {{ record.duration_ms }} ms</p>
            <p>{{ record.total_tokens === null ? '用量未返回' : `${format(record.total_tokens ?? 0)} tokens` }}</p>
            <p class="muted">输入 {{ record.input_tokens ?? '—' }} / 输出 {{ record.output_tokens ?? '—' }} · 推理 {{ record.reasoning_tokens ?? '—' }}</p>
            <p class="muted">缓存读取 {{ record.cached_tokens ?? '—' }} / 缓存写入 {{ record.cache_write_tokens ?? '—' }}</p>
          </template>
          <template v-else-if="detail === 'trigger'">
            <h3>{{ sourceLabels[record.source ?? ''] ?? record.source }} <NTag size="small" :bordered="false">{{ statusLabels[record.status ?? ''] ?? record.status }}</NTag></h3>
            <p v-if="record.result">评估：{{ record.result }}{{ record.waiting ? '（重评）' : '' }} · {{ record.duration_ms }} ms</p>
            <p v-if="record.raw_result" class="raw-result">{{ record.raw_result }}</p>
            <p>实际发送 {{ record.sent_count }} 条消息<span v-if="record.message_id"> · 消息 #{{ record.message_id }}</span></p>
          </template>
          <template v-else-if="detail === 'tools'">
            <h3>{{ toolLabels[record.tool ?? ''] ?? record.tool }} <NTag size="small" :bordered="false" :type="record.success ? 'success' : 'error'">{{ record.success ? '成功' : '失败' }}</NTag></h3>
            <p>{{ record.provider }} · {{ record.duration_ms }} ms</p><p>{{ record.credits == null ? '— credits' : `${format(record.credits)} credits` }}</p><p v-if="record.error">{{ record.error }}</p>
          </template>
          <template v-else><h3>{{ record.stage }}</h3><p>{{ record.error }}</p></template>
          <p v-if="record.run_id" class="muted run-id">运行 {{ record.run_id }}</p>
        </div>
        <div v-if="recordsLoading" class="initial-loading"><NSpin size="small" /></div>
        <p v-else-if="!records.length && !recordsError" class="muted empty-copy">所选范围暂无记录</p>
        <NButton v-if="nextCursor" block :loading="recordsLoading" @click="loadRecords(true)">加载更多</NButton>
        <RouterLink v-if="detail === 'errors'" class="text-button" to="/logs">打开完整日志 <ArrowUpRight :size="14" /></RouterLink>
        <template #footer><NButton @click="detail = null">关闭</NButton></template>
      </NDrawerContent>
    </NDrawer>
  </div>
</template>

<style scoped>
.dashboard { --muted: #74748a; --accent: #7763d8; --line: rgba(120, 110, 155, .13); display: flex; flex-direction: column; gap: 20px; max-width: 1500px; margin: 0 auto; }
:global(html.dark) .dashboard { --muted: #a09bb6; --accent: #ae9bf8; --line: rgba(180, 165, 220, .13); }
h1 { font-size: 24px; font-weight: 650; letter-spacing: -.6px; }
h2 { font-size: 15px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
button { font: inherit; color: inherit; cursor: pointer; }
button:focus-visible, .bar-group:focus-visible { outline: 2px solid var(--accent); outline-offset: 4px; }
.page-header, .heading, .filters, .panel-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.filters { flex-wrap: wrap; }
.heading { justify-content: flex-start; }
.conversation-select { width: 160px; }
.segmented { display: inline-flex; padding: 3px; border: 1px solid var(--line); border-radius: 9px; background: rgba(120, 110, 155, .05); flex-shrink: 0; }
.segmented button { border: 0; border-radius: 6px; padding: 5px 14px; background: transparent; color: var(--muted); font-size: 13px; transition: background .15s; }
.segmented button.active { background: var(--glass-bg-heavy); color: var(--accent); box-shadow: 0 1px 4px #00000009; }
.segmented.small button { padding: 4px 10px; font-size: 12px; }
.muted, .small-text { color: var(--muted, #858097); }
.small-text { font-size: 12px; }
.status-strip { display: flex; align-items: center; flex-wrap: wrap; gap: 12px 26px; padding: 12px 18px; font-size: 12px; box-shadow: none; }
.status-item { display: flex; align-items: center; gap: 8px; }
.status-dot { width: 7px; height: 7px; border-radius: 50%; background: #50aa8a; box-shadow: 0 0 0 4px #50aa8a12; margin-right: 2px; }
.demo-note { margin-left: auto; color: var(--muted); font-size: 11px; }
.metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; }
.metric { display: flex; flex-direction: column; padding: 18px 20px; position: relative; }
.metric-link { position: absolute; bottom: 15px; right: 16px; border: 0; padding: 4px; color: var(--accent); background: transparent; }
.initial-loading { min-height: 140px; display: flex; align-items: center; justify-content: center; gap: 12px; font-size: 13px; }
.status-dot.offline { background: #a6a0ad; box-shadow: none; }
.empty-copy { text-align: center; padding: 22px 0; font-size: 12px; }
.raw-result { background: #8d79dc0c; padding: 8px; border-radius: 6px; white-space: pre-wrap; overflow-wrap: anywhere; }
.run-id { overflow-wrap: anywhere; font-family: var(--font-mono); font-size: 10px !important; }
.metric-top { display: flex; align-items: center; justify-content: space-between; font-size: 13px; color: var(--muted); }
.metric-icon { display: grid; place-items: center; width: 32px; height: 32px; border-radius: 10px; }
.purple { color: #8d79dc; background: #8d79dc15; }.blue { color: #6596d3; background: #6596d315; }.green { color: #52a88c; background: #52a88c15; }.amber { color: #bb914a; background: #bb914a15; }
.metric-value { font-size: 32px; font-weight: 600; letter-spacing: -1px; line-height: 1.4; margin: 8px 0 6px; font-variant-numeric: tabular-nums; }
.metric-note { font-size: 11px; color: var(--muted); }
.overview-grid { display: grid; grid-template-columns: minmax(0, 1.8fr) minmax(280px, 1fr); gap: 20px; }
.panel { padding: 20px; min-width: 0; }
.trend-panel { display: flex; flex-direction: column; }
.trend-panel .chart { flex: 1; min-height: 170px; }
.panel-heading { margin-bottom: 20px; }
.text-button { background: none; border: 0; padding: 3px 0; color: var(--accent, #9682e4); display: inline-flex; align-items: center; gap: 4px; font-size: 12px; }
.text-button:hover { opacity: .7; }
.chart-caption, .legend { display: flex; align-items: center; gap: 7px; font-size: 11px; color: var(--muted); }
.chart-caption { gap: 16px; margin-bottom: 20px; }
.legend-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.purple-dot { background: #9985e5; }.blue-dot { background: #74acd8; }
.chart-unit { margin-left: auto; }
.chart { height: 170px; display: flex; align-items: flex-end; gap: 7px; position: relative; padding-top: 8px; }
.chart-grid { position: absolute; inset: 0; display: flex; flex-direction: column; justify-content: space-between; pointer-events: none; }
.chart-grid span { border-bottom: 1px dashed var(--line); }
.bar-group { height: 100%; flex: 1; min-width: 0; position: relative; display: flex; align-items: flex-end; gap: 2px; }
.bar { flex: 1; min-width: 0; border-radius: 3px 3px 0 0; transition: height .25s; z-index: 1; }
.primary-bar { background: linear-gradient(0deg, #9985e580, #9985e5da); }.secondary-bar { background: #74acd8; }
.bar-tooltip { display: none; position: absolute; bottom: 100%; left: 50%; transform: translateX(-50%); white-space: nowrap; background: var(--glass-bg-heavy); padding: 8px 10px; border: 1px solid var(--glass-border); border-radius: 6px; box-shadow: var(--glass-shadow); font-size: 11px; z-index: 3; pointer-events: none; }
.bar-group:hover .bar-tooltip, .bar-group:focus .bar-tooltip { display: block; }
.chart-ticks { display: flex; justify-content: space-between; font-size: 10px; color: var(--muted); margin-top: 10px; }
.usage-total { display: flex; align-items: baseline; gap: 8px; font-size: 12px; margin: -3px 0 16px; }
.resource-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.resource-heading h3 { font-size: 13px; font-weight: 500; }
.tavily-usage { margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--line); }
.tool-usage-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 12px; margin-top: 12px; }
.tool-values { display: flex; align-items: center; justify-content: flex-end; gap: 12px; flex-wrap: wrap; font-variant-numeric: tabular-nums; }
.tool-failures { color: #c58a50; }
.usage-total strong { font-size: 27px; font-weight: 600; letter-spacing: -.7px; }
.stacked-bar { height: 8px; border-radius: 5px; display: flex; gap: 3px; overflow: hidden; }
.stacked-bar span { min-width: 0; }
.slot-list { display: flex; flex-direction: column; gap: 14px; margin: 20px 0; }
.slot-row, .slot-name, .slot-value { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.slot-row { justify-content: space-between; }.slot-label { color: var(--muted); font-size: 11px; }.slot-value { font-variant-numeric: tabular-nums; }.slot-value .muted { width: 30px; text-align: right; }
.usage-footnote { display: flex; justify-content: space-between; border-top: 1px solid var(--line); padding-top: 12px; font-size: 11px; color: var(--muted); }
.heading-count, .error-count { font-size: 11px; font-weight: 500; padding: 1px 7px; background: #8d79dc15; border-radius: 5px; color: var(--accent); }
.conversations-panel .panel-heading { margin-bottom: 12px; }
.table-scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; white-space: nowrap; font-size: 12px; }
th { color: var(--muted); font-weight: 400; font-size: 11px; text-align: right; padding: 8px 16px 12px; border-bottom: 1px solid var(--line); }
td { text-align: right; padding: 13px 16px; font-variant-numeric: tabular-nums; border-bottom: 1px solid var(--line); }
th:first-child, td:first-child { text-align: left; padding-left: 0; }th:last-child, td:last-child { padding-right: 0; }tbody tr:last-child td { border-bottom: none; }tbody tr:hover { background: #8d79dc05; }
.group-name { display: inline-flex; align-items: center; gap: 10px; font-weight: 500; }.avatar { width: 30px; height: 30px; border-radius: 9px; display: inline-grid; place-items: center; font-size: 12px; }
.table-usage { display: inline-flex; align-items: center; gap: 12px; }.mini-track { width: 54px; height: 4px; border-radius: 3px; background: #8d79dc16; overflow: hidden; }.mini-track span { display: block; height: 100%; border-radius: inherit; background: #9985e5; }
.bottom-grid { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr); gap: 20px; }
.trigger-metrics { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin: 4px 0 22px; }.trigger-metrics div { display: flex; flex-direction: column; gap: 5px; }.trigger-metrics strong { font-size: 23px; font-weight: 550; font-variant-numeric: tabular-nums; }.trigger-metrics small { font-size: 12px; font-weight: 400; color: var(--muted); }.trigger-metrics span { font-size: 11px; color: var(--muted); }
.result-bar { height: 6px; }.result-labels { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 10px; margin-top: 12px; }.result-labels > span { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--muted); }.result-labels strong { font-weight: 500; color: var(--accent); margin-left: 3px; }
.trigger-footer { border-top: 1px solid var(--line); padding-top: 14px; margin-top: 20px; display: flex; flex-wrap: wrap; gap: 16px; font-size: 11px; }.trigger-footer b { font-weight: 500; margin-left: 5px; }.trigger-footer > .muted { margin-left: auto; }
.error-count { color: #b48647; background: #b4864715; }.error-row { display: flex; align-items: flex-start; gap: 10px; width: 100%; text-align: left; border: 0; border-bottom: 1px solid var(--line); background: none; padding: 14px 0; }.error-row:hover { background: #8d79dc05; }.error-icon { color: #c39758; flex-shrink: 0; margin-top: 2px; }.error-content { display: flex; flex-direction: column; gap: 7px; min-width: 0; }.error-title { font-size: 12px; display: flex; align-items: center; gap: 8px; }.repeat-count { font-size: 10px; color: var(--muted); padding: 0 5px; border-radius: 4px; background: #8d79dc0c; }.error-context { font-size: 11px; color: var(--muted); line-height: 1.6; }.error-row time { font-size: 10px; color: var(--muted); margin-left: auto; }.errors-footer { font-size: 10px; color: var(--muted); margin-top: 14px; }.empty-state { display: flex; align-items: center; justify-content: center; flex-direction: column; gap: 12px; color: #55a78d; min-height: 155px; font-size: 12px; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
.detail-card { padding: 18px 0; border-bottom: 1px solid var(--glass-border); }.detail-card h3 { font-size: 14px; font-weight: 600; margin-bottom: 12px; }.detail-card h3 span { font-size: 12px; font-weight: 400; }.detail-card p { font-size: 12px; margin-top: 8px; }.detail-line { display: flex; justify-content: space-between; }.event-timeline { padding-left: 20px; font-size: 12px; line-height: 2.8; }
@media (min-width: 1500px) { .chart { height: 195px; } }
@media (max-width: 1100px) { .overview-grid { grid-template-columns: minmax(0, 1.4fr) minmax(260px, 1fr); }.bottom-grid { grid-template-columns: 1fr; }.demo-note { width: 100%; margin-left: 0; }.slot-label { display: none; } }
@media (max-width: 767px) { .dashboard { gap: 16px; }.page-header { flex-wrap: wrap; }.heading { width: 100%; }.filters { width: 100%; }.conversation-select { width: 145px; }.metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }.metric { padding: 14px; }.metric-value { font-size: 28px; }.metric-note { font-size: 10px; }.overview-grid { grid-template-columns: 1fr; gap: 16px; }.panel { padding: 16px; }.status-strip { gap: 12px 18px; padding: 12px 14px; }.status-item .muted { display: none; }.slot-label { display: inline; }.chart { gap: 4px; }.trigger-footer > .muted { margin-left: 0; }.page-footer { flex-wrap: wrap; }.bar-tooltip { left: auto; right: 0; transform: none; } }
</style>
