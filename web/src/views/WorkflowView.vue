<script setup lang="ts">
import { NButton, NInput, NInputNumber, NPopconfirm, NSelect, NSwitch, NTag, NText, useMessage } from 'naive-ui'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { Handle, MarkerType, Position, VueFlow, type Edge, type Node } from '@vue-flow/core'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/controls/dist/style.css'
import { Eye, Image, MessageSquare, RotateCcw, Save, Sparkles, Zap } from 'lucide-vue-next'
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import api from '@/api'

interface PortSpec {
  port_id: string
  description: string
  payload_schema: Record<string, unknown>
  model_callable: boolean
}

interface NodeSpec {
  node_id: string
  kind: string
  title: string
  system_prompt: string
  instructions: string
  model: {
    slot: string
    temperature: number | null
    max_tokens: number | null
    max_tool_rounds: number | null
    require_tool_call: boolean
  } | null
  tools: { include_categories: string[]; include_names: string[]; exclude_names: string[] }
  input_ports: PortSpec[]
  output_ports: PortSpec[]
  [key: string]: unknown
}

interface EdgeSpec {
  edge_id: string
  from_node: string
  from_port: string
  to_node: string
  to_port: string
  trigger: string
  max_traversals: number | null
}

interface Workflow {
  workflow_id: string
  revision: string
  entry_node: string
  nodes: NodeSpec[]
  edges: EdgeSpec[]
  layout: Record<string, { x: number; y: number }>
}

interface Setting {
  key: string
  label: string
  type: 'int' | 'float'
  min: number
  max: number
  value: number
  default: number
}

interface SlotBinding {
  alias: string
  model: string
  api_type: string
}

interface Catalog {
  node_slots: string[]
  slots: Record<string, SlotBinding>
  settings: Record<string, Setting[]>
  stage_prompts: Record<string, string[]>
  stage_slots: Record<string, string>
  node_variables: Record<string, string>
  tools: { name: string; category: string }[]
}

interface ProviderInfo {
  alias: string
  models: string[]
  base_urls: Record<string, string>
}

interface RunSummary {
  run_id: string
  conversation_id: number | null
  status: string
  current_node: string | null
  started_at: number
  finished_at: number | null
  error: string | null
}

const STAGES = ['vision', 'trigger'] as const
type StageId = (typeof STAGES)[number]
const STAGE_META: Record<StageId, { title: string; hint: string }> = {
  vision: { title: '图片识别', hint: '每条消息' },
  trigger: { title: '触发判断', hint: '决定是否回复' },
}

const message = useMessage()

const workflow = ref<Workflow | null>(null)
const savedJson = ref('')
const source = ref<'preset' | 'configured'>('preset')
const catalog = ref<Catalog | null>(null)
const providers = ref<ProviderInfo[]>([])
const selected = ref<string>('')
const saving = ref(false)

const settingDrafts = reactive<Record<string, number>>({})
const slotDrafts = reactive<Record<string, SlotBinding>>({})

const dirty = computed(() => workflow.value != null && JSON.stringify(workflow.value) !== savedJson.value)
const settingsDirty = computed(() =>
  Object.values(catalog.value?.settings ?? {})
    .flat()
    .some((s) => settingDrafts[s.key] !== s.value),
)

function errorReason(error: any, fallback: string): string {
  return error?.response?.data?.error ?? fallback
}

// ── 数据加载 ──
async function loadWorkflow() {
  const { data } = await api.get('/workflows/active')
  workflow.value = data.workflow
  source.value = data.source
  savedJson.value = JSON.stringify(data.workflow)
}

async function loadCatalog() {
  const { data } = await api.get('/workflows/catalog')
  catalog.value = data
  for (const item of Object.values(data.settings as Record<string, Setting[]>).flat()) {
    settingDrafts[item.key] = item.value
  }
  for (const [slot, binding] of Object.entries(data.slots as Record<string, SlotBinding>)) {
    slotDrafts[slot] = { ...binding, api_type: binding.api_type || 'openai' }
  }
}

async function loadProviders() {
  const { data } = await api.get('/providers')
  providers.value = data.providers
}

// ── 画布 ──
const DEFAULT_POSITIONS: Record<string, { x: number; y: number }> = {
  'stage:vision': { x: 0, y: 0 },
  'stage:trigger': { x: 0, y: 130 },
}

function positionOf(id: string, index: number): { x: number; y: number } {
  const layout = workflow.value?.layout ?? {}
  return layout[id] ?? DEFAULT_POSITIONS[id] ?? { x: 300 + index * 380, y: 130 }
}

const flowNodes = computed<Node[]>(() => {
  const wf = workflow.value
  if (!wf) return []
  const stageNodes: Node[] = STAGES.map((stage) => ({
    id: `stage:${stage}`,
    type: 'stage',
    position: positionOf(`stage:${stage}`, 0),
    data: { stage },
    draggable: true,
  }))
  const graphNodes: Node[] = wf.nodes.map((node, index) => ({
    id: node.node_id,
    type: node.kind === 'terminal' ? 'terminal' : 'llm',
    position: positionOf(node.node_id, index),
    data: { node },
  }))
  return [...stageNodes, ...graphNodes]
})

const flowEdges = computed<Edge[]>(() => {
  const wf = workflow.value
  if (!wf) return []
  const edges: Edge[] = [
    {
      id: 'stage-link-vision',
      source: 'stage:vision',
      target: 'stage:trigger',
      sourceHandle: 'out',
      targetHandle: 'in',
      class: 'edge--stage',
      animated: activeStages.vision,
    },
    {
      id: 'stage-link-entry',
      source: 'stage:trigger',
      target: wf.entry_node,
      sourceHandle: 'out',
      targetHandle: `in:${wf.nodes.find((n) => n.node_id === wf.entry_node)?.input_ports[0]?.port_id ?? ''}`,
      class: 'edge--stage',
      animated: activeStages.trigger,
    },
  ]
  const nodeIndex = (id: string) => wf.nodes.findIndex((n) => n.node_id === id)
  for (const edge of wf.edges) {
    const backward = positionOf(edge.to_node, nodeIndex(edge.to_node)).x < positionOf(edge.from_node, nodeIndex(edge.from_node)).x
    edges.push({
      type: backward ? 'smoothstep' : 'default',
      pathOptions: backward ? { offset: 40, borderRadius: 12 } : undefined,
      id: edge.edge_id,
      source: edge.from_node,
      target: edge.to_node,
      sourceHandle: `out:${edge.from_port}`,
      targetHandle: `in:${edge.to_port}`,
      label: edge.max_traversals ? `≤${edge.max_traversals}` : undefined,
      markerEnd: MarkerType.ArrowClosed,
      class: [`edge--${edge.trigger}`, { 'edge--active': lastEdge.value === edge.edge_id }],
      animated: lastEdge.value === edge.edge_id,
    })
  }
  return edges
})

function onNodeDragStop({ node }: { node: Node }) {
  if (!workflow.value) return
  workflow.value.layout = {
    ...workflow.value.layout,
    [node.id]: { x: Math.round(node.position.x), y: Math.round(node.position.y) },
  }
}

function onNodeClick({ node }: { node: Node }) {
  selected.value = node.id
}

// ── 选中节点编辑 ──
const selectedNode = computed(() => workflow.value?.nodes.find((n) => n.node_id === selected.value) ?? null)
const selectedStage = computed<StageId | null>(() =>
  selected.value.startsWith('stage:') ? (selected.value.slice(6) as StageId) : null,
)

const slotOptions = computed(() => (catalog.value?.node_slots ?? []).map((s) => ({ label: s, value: s })))
const categoryOptions = [
  { label: '读取 (input)', value: 'input' },
  { label: '输出 (output)', value: 'output' },
]
const toolOptions = computed(() =>
  (catalog.value?.tools ?? []).map((t) => ({ label: `${t.name} · ${t.category}`, value: t.name })),
)

function providerOptions() {
  return providers.value.map((p) => ({ label: p.alias, value: p.alias }))
}

function modelOptions(slot: string) {
  const alias = slotDrafts[slot]?.alias
  const models = providers.value.find((p) => p.alias === alias)?.models ?? []
  const options = models.map((m) => ({ label: m, value: m }))
  const current = slotDrafts[slot]?.model
  if (current && !models.includes(current)) options.unshift({ label: current, value: current })
  return options
}

function apiTypeOptions(slot: string) {
  const alias = slotDrafts[slot]?.alias
  const urls = providers.value.find((p) => p.alias === alias)?.base_urls ?? {}
  return Object.keys(urls).map((key) => ({ label: key, value: key }))
}

function slotChanged(slot: string): boolean {
  const current = catalog.value?.slots[slot]
  const draft = slotDrafts[slot]
  if (!current || !draft) return false
  return current.alias !== draft.alias || current.model !== draft.model || (current.api_type || 'openai') !== draft.api_type
}

async function applySlot(slot: string) {
  try {
    const { data } = await api.put(`/workflows/slots/${slot}`, slotDrafts[slot])
    if (catalog.value) catalog.value.slots = data.slots
    message.success(data.message)
  } catch (error) {
    message.error(errorReason(error, '切换失败'))
  }
}

async function saveSettings() {
  try {
    const values = Object.fromEntries(
      Object.values(catalog.value?.settings ?? {})
        .flat()
        .filter((s) => settingDrafts[s.key] !== s.value)
        .map((s) => [s.key, settingDrafts[s.key]]),
    )
    const { data } = await api.put('/workflows/settings', { values })
    if (catalog.value) catalog.value.settings = data.settings
    message.success('设置已保存')
  } catch (error) {
    message.error(errorReason(error, '保存设置失败'))
  }
}

async function saveWorkflow() {
  if (!workflow.value) return
  saving.value = true
  try {
    const next = { ...workflow.value, revision: String(Number(workflow.value.revision || 0) + 1) }
    const { data } = await api.put('/workflows/active', { workflow: next })
    workflow.value = data.workflow
    savedJson.value = JSON.stringify(data.workflow)
    source.value = 'configured'
    message.success(`已激活 revision ${data.workflow.revision}`)
  } catch (error) {
    message.error(errorReason(error, '保存失败'))
  } finally {
    saving.value = false
  }
}

async function resetWorkflow() {
  try {
    const { data } = await api.delete('/workflows/active')
    workflow.value = data.workflow
    savedJson.value = JSON.stringify(data.workflow)
    source.value = 'preset'
    message.success('已恢复内置工作流')
  } catch (error) {
    message.error(errorReason(error, '恢复失败'))
  }
}

function insertVariable(name: string) {
  if (!selectedNode.value) return
  selectedNode.value.system_prompt += `{{${name}}}`
}

// ── 实时运行 ──
const runs = ref<RunSummary[]>([])
const activeNode = ref<string | null>(null)
const lastEdge = ref<string | null>(null)
const activeStages = reactive<Record<StageId, boolean>>({ vision: false, trigger: false })
const stageResult = reactive<Record<StageId, string>>({ vision: '', trigger: '' })
const nodeActivity = reactive<Record<string, string>>({})
let source$: EventSource | null = null
const stageTimers: Partial<Record<StageId, ReturnType<typeof setTimeout>>> = {}

function flashStage(stage: StageId, result: string) {
  activeStages[stage] = true
  stageResult[stage] = result
  clearTimeout(stageTimers[stage])
  stageTimers[stage] = setTimeout(() => (activeStages[stage] = false), 2500)
}

function handleStream(event: MessageEvent) {
  const msg = JSON.parse(event.data)
  if (msg.type === 'run_started') {
    runs.value = [msg.run, ...runs.value].slice(0, 20)
    activeNode.value = msg.run.current_node
    lastEdge.value = null
  } else if (msg.type === 'run_event') {
    const ev = msg.event
    if (ev.kind === 'node_entered') activeNode.value = ev.node_id
    if (ev.kind === 'edge') lastEdge.value = ev.edge_id
    if (ev.kind === 'tool_call') nodeActivity[ev.node_id] = `调用 ${ev.name}`
    if (ev.kind === 'llm_response') nodeActivity[ev.node_id] = `第 ${ev.round} 轮`
    const run = runs.value.find((r) => r.run_id === msg.run_id)
    if (run && ev.kind === 'node_entered') run.current_node = ev.node_id
  } else if (msg.type === 'run_finished') {
    const index = runs.value.findIndex((r) => r.run_id === msg.run.run_id)
    if (index >= 0) runs.value[index] = msg.run
    setTimeout(() => {
      if (runs.value[0]?.run_id === msg.run.run_id) {
        activeNode.value = null
        lastEdge.value = null
        for (const key of Object.keys(nodeActivity)) delete nodeActivity[key]
      }
    }, 3000)
  } else if (msg.type === 'stage_event') {
    const stage = msg.stage as StageId
    if (stage === 'trigger') flashStage(stage, msg.result)
    if (stage === 'vision') flashStage(stage, msg.status === 'started' ? '识别中' : msg.status)
  }
}

function connectStream() {
  source$ = new EventSource('/api/workflows/runs/stream')
  source$.onmessage = handleStream
  source$.onerror = () => {
    source$?.close()
    setTimeout(connectStream, 3000)
  }
}

function formatTime(ts: number | null) {
  return ts ? new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour12: false }) : ''
}

function durationOf(run: RunSummary) {
  return run.finished_at ? `${(run.finished_at - run.started_at).toFixed(1)}s` : '运行中'
}

watch(selectedNode, (node) => {
  if (node?.model && node.model.max_tool_rounds === undefined) node.model.max_tool_rounds = null
})

onMounted(async () => {
  try {
    await Promise.all([loadWorkflow(), loadCatalog(), loadProviders()])
    const { data } = await api.get('/workflows/runs')
    runs.value = data.runs.slice(0, 20)
  } catch (error) {
    message.error(errorReason(error, '加载工作流失败'))
  }
  selected.value = workflow.value?.entry_node ?? ''
  connectStream()
})

onBeforeUnmount(() => {
  source$?.close()
  Object.values(stageTimers).forEach(clearTimeout)
})
</script>

<template>
  <div class="workflow-page">
    <div class="page-header">
      <h1 class="page-title">工作流</h1>
      <NTag size="small" :bordered="false" :type="source === 'configured' ? 'info' : 'default'">
        {{ source === 'configured' ? `revision ${workflow?.revision}` : '内置' }}
      </NTag>
      <div class="spacer" />
      <NPopconfirm v-if="source === 'configured'" @positive-click="resetWorkflow">
        <template #trigger>
          <NButton size="small" quaternary>
            <template #icon><RotateCcw :size="14" /></template>
            恢复内置
          </NButton>
        </template>
        删除自定义工作流并恢复内置图？
      </NPopconfirm>
      <NButton size="small" type="primary" :disabled="!dirty" :loading="saving" @click="saveWorkflow">
        <template #icon><Save :size="14" /></template>
        保存并激活
      </NButton>
    </div>

    <div class="workflow-layout">
      <section class="canvas glass-panel-heavy">
        <VueFlow
          v-if="workflow"
          :nodes="flowNodes"
          :edges="flowEdges"
          :default-viewport="{ x: 40, y: 20, zoom: 0.9 }"
          :min-zoom="0.3"
          :max-zoom="1.6"
          :nodes-connectable="false"
          fit-view-on-init
          @node-drag-stop="onNodeDragStop"
          @node-click="onNodeClick"
          @pane-click="selected = ''"
        >
          <Background :gap="22" :size="1.2" pattern-color="rgba(127,127,148,0.35)" />
          <Controls :show-interactive="false" position="bottom-left" />

          <template #node-stage="{ id, data }">
            <div
              class="flow-card flow-card--stage"
              :class="{ 'flow-card--selected': selected === id, 'flow-card--active': activeStages[data.stage as StageId] }"
            >
              <Handle v-if="data.stage === 'trigger'" id="in" type="target" :position="Position.Top" />
              <div class="flow-card__head">
                <component :is="data.stage === 'vision' ? Image : Zap" :size="15" />
                <span class="flow-card__title">{{ STAGE_META[data.stage as StageId].title }}</span>
                <span class="flow-card__kind">{{ STAGE_META[data.stage as StageId].hint }}</span>
              </div>
              <div class="flow-card__row">
                <span>{{ catalog?.slots[data.stage]?.model || '未配置模型' }}</span>
              </div>
              <div v-if="activeStages[data.stage as StageId]" class="flow-card__activity">
                {{ stageResult[data.stage as StageId] }}
              </div>
              <Handle
                id="out"
                type="source"
                :position="data.stage === 'vision' ? Position.Bottom : Position.Right"
              />
            </div>
          </template>

          <template #node-llm="{ id, data }">
            <div
              class="flow-card"
              :class="{ 'flow-card--selected': selected === id, 'flow-card--active': activeNode === id }"
            >
              <div class="flow-card__head">
                <Sparkles :size="15" />
                <span class="flow-card__title">{{ data.node.title || data.node.node_id }}</span>
                <span class="flow-card__kind">{{ data.node.model?.slot }}</span>
              </div>
              <div class="flow-card__row">
                <span>{{ catalog?.slots[data.node.model?.slot]?.model || '未配置模型' }}</span>
              </div>
              <div class="flow-card__ports">
                <div class="flow-card__port-col">
                  <div v-for="port in data.node.input_ports" :key="port.port_id" class="flow-card__port">
                    <Handle :id="`in:${port.port_id}`" type="target" :position="Position.Left" />
                    {{ port.port_id }}
                  </div>
                </div>
                <div class="flow-card__port-col flow-card__port-col--out">
                  <div v-for="port in data.node.output_ports" :key="port.port_id" class="flow-card__port">
                    {{ port.port_id }}
                    <Handle :id="`out:${port.port_id}`" type="source" :position="Position.Right" />
                  </div>
                </div>
              </div>
              <div v-if="activeNode === id && nodeActivity[id]" class="flow-card__activity">
                {{ nodeActivity[id] }}
              </div>
            </div>
          </template>

          <template #node-terminal="{ id, data }">
            <div
              class="flow-card flow-card--terminal"
              :class="{ 'flow-card--selected': selected === id, 'flow-card--active': activeNode === id }"
            >
              <Handle
                v-for="port in data.node.input_ports"
                :id="`in:${port.port_id}`"
                :key="port.port_id"
                type="target"
                :position="Position.Left"
              />
              <MessageSquare :size="15" />
              <span class="flow-card__title">{{ data.node.title || data.node.node_id }}</span>
            </div>
          </template>
        </VueFlow>
      </section>

      <aside class="inspector glass-panel-heavy">
        <!-- 图节点 -->
        <template v-if="selectedNode && selectedNode.model">
          <header class="inspector__head">
            <h2>{{ selectedNode.title || selectedNode.node_id }}</h2>
            <code>{{ selectedNode.node_id }}</code>
          </header>

          <label class="field">
            <span>名称</span>
            <NInput v-model:value="selectedNode.title" size="small" />
          </label>

          <div class="field-grid">
            <label class="field">
              <span>模型槽</span>
              <NSelect v-model:value="selectedNode.model.slot" :options="slotOptions" size="small" />
            </label>
            <label class="field">
              <span>强制工具调用</span>
              <NSwitch v-model:value="selectedNode.model.require_tool_call" />
            </label>
            <label class="field">
              <span>最大输出 token</span>
              <NInputNumber v-model:value="selectedNode.model.max_tokens" size="small" :min="64" :max="65536" clearable placeholder="默认" />
            </label>
            <label class="field">
              <span>温度</span>
              <NInputNumber v-model:value="selectedNode.model.temperature" size="small" :min="0" :max="2" :step="0.1" clearable placeholder="默认" />
            </label>
            <label class="field">
              <span>工具轮数上限</span>
              <NInputNumber v-model:value="selectedNode.model.max_tool_rounds" size="small" :min="1" :max="200" clearable placeholder="默认" />
            </label>
          </div>

          <div class="field">
            <span>System prompt</span>
            <div class="variable-bar">
              <button
                v-for="(desc, name) in catalog?.node_variables"
                :key="name"
                type="button"
                class="variable-chip"
                :title="desc"
                @click="insertVariable(String(name))"
              >
                {{ name }}
              </button>
            </div>
            <NInput
              v-model:value="selectedNode.system_prompt"
              type="textarea"
              class="mono-input"
              :autosize="{ minRows: 2, maxRows: 8 }"
              placeholder="{{prompt.persona.system}}"
            />
          </div>

          <label class="field">
            <span>节点指令</span>
            <NInput
              v-model:value="selectedNode.instructions"
              type="textarea"
              class="mono-input"
              :autosize="{ minRows: 3, maxRows: 10 }"
            />
          </label>

          <div class="field-grid">
            <label class="field">
              <span>工具类别</span>
              <NSelect v-model:value="selectedNode.tools.include_categories" :options="categoryOptions" multiple size="small" />
            </label>
            <label class="field">
              <span>排除工具</span>
              <NSelect v-model:value="selectedNode.tools.exclude_names" :options="toolOptions" multiple filterable size="small" />
            </label>
          </div>

          <div class="slot-block">
            <span class="slot-block__label">模型槽 {{ selectedNode.model.slot }}（所有使用该槽的节点共享）</span>
            <div class="field-grid">
              <NSelect v-model:value="slotDrafts[selectedNode.model.slot].alias" :options="providerOptions()" size="small" filterable />
              <NSelect v-model:value="slotDrafts[selectedNode.model.slot].api_type" :options="apiTypeOptions(selectedNode.model.slot)" size="small" />
            </div>
            <NSelect v-model:value="slotDrafts[selectedNode.model.slot].model" :options="modelOptions(selectedNode.model.slot)" size="small" filterable tag />
            <NButton size="small" secondary :disabled="!slotChanged(selectedNode.model.slot)" @click="applySlot(selectedNode.model.slot)">
              立即切换
            </NButton>
          </div>
        </template>

        <!-- 前置阶段 -->
        <template v-else-if="selectedStage && catalog">
          <header class="inspector__head">
            <h2>{{ STAGE_META[selectedStage].title }}</h2>
          </header>

          <div class="slot-block">
            <span class="slot-block__label">模型</span>
            <div class="field-grid">
              <NSelect v-model:value="slotDrafts[selectedStage].alias" :options="providerOptions()" size="small" filterable />
              <NSelect v-model:value="slotDrafts[selectedStage].api_type" :options="apiTypeOptions(selectedStage)" size="small" />
            </div>
            <NSelect v-model:value="slotDrafts[selectedStage].model" :options="modelOptions(selectedStage)" size="small" filterable tag />
            <NButton size="small" secondary :disabled="!slotChanged(selectedStage)" @click="applySlot(selectedStage)">
              立即切换
            </NButton>
          </div>

          <div class="field-grid">
            <label v-for="item in catalog.settings[selectedStage]" :key="item.key" class="field">
              <span>{{ item.label }}</span>
              <NInputNumber
                v-model:value="settingDrafts[item.key]"
                size="small"
                :min="item.min"
                :max="item.max"
                :step="item.key === 'random_trigger_percent' ? 1 : item.type === 'int' ? 1 : 0.5"
                :precision="item.type === 'int' ? 0 : undefined"
              />
            </label>
          </div>

          <div class="field">
            <span>Prompt</span>
            <div class="prompt-links">
              <RouterLink
                v-for="key in catalog.stage_prompts[selectedStage]"
                :key="key"
                :to="{ name: 'prompts', query: { key } }"
                class="prompt-link"
              >
                {{ key }}
              </RouterLink>
            </div>
          </div>
        </template>

        <!-- 全局 -->
        <template v-else-if="catalog">
          <header class="inspector__head">
            <h2>全局</h2>
          </header>
          <div class="field-grid">
            <label v-for="item in catalog.settings.run" :key="item.key" class="field">
              <span>{{ item.label }}</span>
              <NInputNumber v-model:value="settingDrafts[item.key]" size="small" :min="item.min" :max="item.max" />
            </label>
          </div>
          <NText depth="3" class="hint">点击画布上的节点编辑它的配置</NText>
        </template>

        <div v-if="settingsDirty" class="save-row">
          <NButton size="small" type="primary" @click="saveSettings">
            <template #icon><Save :size="14" /></template>
            保存设置
          </NButton>
        </div>

        <section class="runs">
          <div class="runs__head">
            <Eye :size="14" />
            <span>最近运行</span>
          </div>
          <div v-if="!runs.length" class="runs__empty">暂无运行记录</div>
          <div v-for="run in runs" :key="run.run_id" class="run-row" :title="run.error ?? ''">
            <span class="run-dot" :class="`run-dot--${run.status}`" />
            <span class="run-row__time">{{ formatTime(run.started_at) }}</span>
            <span class="run-row__conv">#{{ run.conversation_id }}</span>
            <span class="run-row__node">{{ run.status === 'running' ? run.current_node : run.status }}</span>
            <span class="run-row__dur">{{ durationOf(run) }}</span>
          </div>
        </section>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.workflow-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  height: calc(100vh - 96px);
}

.page-header {
  display: flex;
  align-items: center;
  gap: 10px;
}

.page-title {
  font-size: 1.5rem;
  font-weight: 600;
}

.spacer {
  flex: 1;
}

.workflow-layout {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 360px;
  gap: 16px;
}

.canvas {
  min-height: 0;
  overflow: hidden;
}

/* ── 卡片 ── */
.flow-card {
  position: relative;
  min-width: 200px;
  padding: 10px 14px 12px;
  border-radius: 12px;
  border: 1px solid var(--glass-border);
  background: var(--glass-bg-heavy);
  box-shadow: var(--glass-shadow);
  font-size: 0.82rem;
  transition: border-color 0.2s ease, box-shadow 0.2s ease;
}

.flow-card--selected {
  border-color: var(--brand-gradient-start);
}

.flow-card--active {
  border-color: var(--brand-gradient-start);
  box-shadow: 0 0 0 3px rgba(81, 63, 224, 0.25), var(--glass-shadow);
}

.flow-card--stage {
  min-width: 180px;
  border-style: dashed;
}

.flow-card--terminal {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 14px;
}

.flow-card__head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}

.flow-card__title {
  font-weight: 600;
  font-size: 0.9rem;
}

.flow-card__kind {
  margin-left: auto;
  font-family: var(--font-mono);
  font-size: 0.7rem;
  opacity: 0.55;
}

.flow-card__row {
  font-family: var(--font-mono);
  font-size: 0.75rem;
  opacity: 0.7;
}

.flow-card__ports {
  margin: 8px -14px 0;
  display: flex;
  justify-content: space-between;
  gap: 16px;
}

.flow-card__port-col {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.flow-card__port-col--out {
  align-items: flex-end;
}

.flow-card__port {
  position: relative;
  font-family: var(--font-mono);
  font-size: 0.7rem;
  padding: 1px 14px;
  opacity: 0.8;
}

.flow-card__activity {
  margin-top: 8px;
  font-size: 0.72rem;
  color: var(--brand-gradient-start);
}

.canvas :deep(.vue-flow__edge.edge--host path) {
  stroke-dasharray: 5 4;
}

.canvas :deep(.vue-flow__edge.edge--stage path) {
  stroke-dasharray: 2 4;
  opacity: 0.6;
}

.canvas :deep(.vue-flow__edge.edge--active path) {
  stroke: var(--brand-gradient-start);
  stroke-width: 2;
}

.canvas :deep(.vue-flow__edge-textbg) {
  fill: var(--glass-bg-heavy);
}

.canvas :deep(.vue-flow__edge-text) {
  font-family: var(--font-mono);
  font-size: 10px;
  fill: currentColor;
}

.canvas :deep(.vue-flow__handle) {
  width: 8px;
  height: 8px;
  background: var(--brand-gradient-start);
  border: 2px solid var(--glass-bg-heavy);
}

.canvas :deep(.vue-flow__controls) {
  box-shadow: var(--glass-shadow);
  border-radius: 8px;
  overflow: hidden;
}

/* ── 检查器 ── */
.inspector {
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  overflow: auto;
  min-height: 0;
}

.inspector__head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.inspector__head h2 {
  font-size: 1.05rem;
  font-weight: 600;
}

.inspector__head code {
  font-family: var(--font-mono);
  font-size: 0.78rem;
  opacity: 0.55;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 0.82rem;
  font-weight: 500;
}

.field-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.mono-input :deep(textarea) {
  font-family: var(--font-mono);
  font-size: 0.8rem;
  line-height: 1.55;
}

.variable-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.variable-chip {
  padding: 1px 6px;
  border: 1px solid var(--glass-border);
  border-radius: 5px;
  background: rgba(127, 127, 148, 0.08);
  color: inherit;
  font-family: var(--font-mono);
  font-size: 0.7rem;
  cursor: pointer;
}

.variable-chip:hover {
  border-color: var(--brand-gradient-start);
}

.slot-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border-radius: 8px;
  background: rgba(127, 127, 148, 0.06);
}

.slot-block__label {
  font-size: 0.78rem;
  opacity: 0.65;
}

.prompt-links {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.prompt-link {
  font-family: var(--font-mono);
  font-size: 0.78rem;
  padding: 2px 8px;
  border-radius: 6px;
  background: rgba(81, 63, 224, 0.1);
  color: inherit;
  text-decoration: none;
}

.prompt-link:hover {
  background: rgba(81, 63, 224, 0.18);
}

.hint {
  font-size: 0.8rem;
}

.save-row {
  display: flex;
  justify-content: flex-end;
}

/* ── 运行列表 ── */
.runs {
  margin-top: auto;
  padding-top: 12px;
  border-top: 1px solid var(--glass-border);
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.runs__head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.82rem;
  font-weight: 600;
  margin-bottom: 4px;
}

.runs__empty {
  font-size: 0.78rem;
  opacity: 0.6;
}

.run-row {
  display: grid;
  grid-template-columns: 10px 64px 40px 1fr auto;
  align-items: center;
  gap: 6px;
  font-family: var(--font-mono);
  font-size: 0.74rem;
}

.run-row__node {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.run-row__dur {
  opacity: 0.6;
}

.run-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #999;
}

.run-dot--running {
  background: var(--brand-gradient-start);
  animation: pulse 1s ease-in-out infinite;
}

.run-dot--completed {
  background: #6ad818;
}

.run-dot--failed {
  background: #e1684e;
}

@keyframes pulse {
  50% {
    opacity: 0.35;
  }
}

@media (max-width: 1000px) {
  .workflow-page {
    height: auto;
  }

  .workflow-layout {
    grid-template-columns: 1fr;
  }

  .canvas {
    height: 60vh;
  }
}
</style>
