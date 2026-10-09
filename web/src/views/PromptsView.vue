<script setup lang="ts">
import {
  NButton,
  NDrawer,
  NDrawerContent,
  NInput,
  NPopconfirm,
  NSelect,
  NTag,
  NText,
  NTooltip,
  useMessage,
} from 'naive-ui'
import { Eye, History, Lock, LockOpen, Plus, RotateCcw, Save, Trash2 } from 'lucide-vue-next'
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api from '@/api'
import { lineDiff } from '@/utils/lineDiff'

interface PromptSummary {
  key: string
  title: string
  group: string
  description: string
  locked: boolean
  is_custom: boolean
  modified: boolean
  updated_at: string | null
  length: number
}

interface PromptVersion {
  id: number
  content: string
  note: string
  created_at: string
}

interface PromptDetail extends PromptSummary {
  content: string
  default: string
  variables: Record<string, string>
  version: PromptVersion | null
  used_by: string[]
}

interface PromptGroup {
  key: string
  label: string
  prompts: PromptSummary[]
}

interface Conversation {
  conversation_id: number
  kind: string
  display_name: string
  platform: string
}

const message = useMessage()
const route = useRoute()
const router = useRouter()

const groups = ref<PromptGroup[]>([])
const detail = ref<PromptDetail | null>(null)
const draft = ref('')
const note = ref('')
const unlocked = ref(false)
const saving = ref(false)
const editorRef = ref<InstanceType<typeof NInput> | null>(null)

const selectedKey = computed(() => (route.query.key as string) || '')
const dirty = computed(() => detail.value != null && draft.value !== detail.value.content)
const readonly = computed(() => !!detail.value?.locked && !unlocked.value)
const allKeys = computed(() => groups.value.flatMap((g) => g.prompts.map((p) => p.key)))

function errorReason(error: any, fallback: string): string {
  return error?.response?.data?.error ?? fallback
}

function formatTime(iso: string | null): string {
  if (!iso) return ''
  return new Date(iso).toLocaleString('zh-CN', { hour12: false })
}

async function loadList() {
  try {
    const { data } = await api.get('/prompts')
    groups.value = data.groups
  } catch (error) {
    message.error(errorReason(error, '加载 prompt 列表失败'))
  }
}

async function loadDetail(key: string) {
  try {
    const { data } = await api.get(`/prompts/${encodeURIComponent(key)}`)
    detail.value = data
    draft.value = data.content
    note.value = ''
    unlocked.value = false
    preview.value = null
  } catch (error) {
    message.error(errorReason(error, '加载 prompt 失败'))
  }
}

function select(key: string) {
  if (key === selectedKey.value) return
  if (dirty.value && !window.confirm('当前修改尚未保存，确定切换？')) return
  router.replace({ query: { key } })
}

watch(selectedKey, (key) => {
  if (key) void loadDetail(key)
})

async function save(content = draft.value, saveNote = note.value) {
  if (!detail.value) return
  saving.value = true
  try {
    const { data } = await api.put(`/prompts/${encodeURIComponent(detail.value.key)}`, {
      content,
      note: saveNote,
      base_version_id: detail.value.version?.id ?? null,
      unlock: unlocked.value,
    })
    detail.value = data
    draft.value = data.content
    note.value = ''
    message.success('已保存')
    await loadList()
    if (historyOpen.value) await loadVersions()
    if (preview.value) await runPreview()
  } catch (error) {
    message.error(errorReason(error, '保存失败'))
  } finally {
    saving.value = false
  }
}

function insertToken(token: string) {
  const el = editorRef.value?.textareaElRef as HTMLTextAreaElement | undefined
  const text = `{{${token}}}`
  if (!el) {
    draft.value += text
    return
  }
  const start = el.selectionStart
  const end = el.selectionEnd
  draft.value = draft.value.slice(0, start) + text + draft.value.slice(end)
  void nextTick(() => {
    el.focus()
    el.setSelectionRange(start + text.length, start + text.length)
  })
}

// ── 预览 ──
const conversations = ref<Conversation[]>([])
const previewConversation = ref<number | null>(null)
const preview = ref<{ text: string; length: number; missing: string[]; includes: string[] } | null>(null)
const previewLoading = ref(false)

const usesRuntime = computed(() => Object.keys(detail.value?.variables ?? {}).includes('runtime_context'))

const conversationOptions = computed(() =>
  conversations.value.map((c) => ({
    label: `${c.display_name || '未命名'} · ${c.kind} #${c.conversation_id}`,
    value: c.conversation_id,
  })),
)

async function loadConversations() {
  if (conversations.value.length) return
  try {
    const { data } = await api.get('/permissions/conversations')
    conversations.value = data.conversations
    previewConversation.value ??= conversations.value[0]?.conversation_id ?? null
  } catch {
    // 预览仍可在无会话时进行
  }
}

async function runPreview() {
  if (!detail.value) return
  previewLoading.value = true
  try {
    if (usesRuntime.value) await loadConversations()
    const { data } = await api.post(`/prompts/${encodeURIComponent(detail.value.key)}/preview`, {
      content: draft.value,
      conversation_id: usesRuntime.value ? previewConversation.value : null,
    })
    preview.value = data
  } catch (error) {
    message.error(errorReason(error, '预览失败'))
  } finally {
    previewLoading.value = false
  }
}

function togglePreview() {
  if (preview.value) preview.value = null
  else void runPreview()
}

watch(previewConversation, () => {
  if (preview.value) void runPreview()
})

// ── 历史 ──
const historyOpen = ref(false)
const versions = ref<PromptVersion[]>([])
const compareId = ref<number | null>(null)

const compareVersion = computed(() => versions.value.find((v) => v.id === compareId.value) ?? null)
const diff = computed(() =>
  compareVersion.value ? lineDiff(compareVersion.value.content, draft.value) : [],
)

async function loadVersions() {
  if (!detail.value) return
  const { data } = await api.get(`/prompts/${encodeURIComponent(detail.value.key)}/versions`)
  versions.value = data.versions
  compareId.value ??= versions.value[1]?.id ?? versions.value[0]?.id ?? null
}

async function openHistory() {
  compareId.value = null
  historyOpen.value = true
  try {
    await loadVersions()
  } catch (error) {
    message.error(errorReason(error, '加载历史失败'))
  }
}

async function rollback(version: PromptVersion) {
  await save(version.content, `回滚到 #${version.id}`)
}

// ── 共享片段 ──
const createOpen = ref(false)
const createForm = ref({ key: '', title: '', description: '', content: '' })
const includeToken = computed(() => `{{prompt.${createForm.value.key || 'key'}}}`)

async function createPrompt() {
  try {
    const { data } = await api.post('/prompts', createForm.value)
    createOpen.value = false
    createForm.value = { key: '', title: '', description: '', content: '' }
    await loadList()
    router.replace({ query: { key: data.key } })
  } catch (error) {
    message.error(errorReason(error, '创建失败'))
  }
}

async function deletePrompt() {
  if (!detail.value) return
  try {
    await api.delete(`/prompts/${encodeURIComponent(detail.value.key)}`)
    detail.value = null
    await loadList()
    router.replace({ query: { key: allKeys.value[0] } })
  } catch (error) {
    message.error(errorReason(error, '删除失败'))
  }
}

onMounted(async () => {
  await loadList()
  if (selectedKey.value) await loadDetail(selectedKey.value)
  else if (allKeys.value.length) router.replace({ query: { key: allKeys.value[0] } })
})
</script>

<template>
  <div class="prompts-page">
    <div class="page-header">
      <h1 class="page-title">Prompt</h1>
    </div>

    <div class="prompts-layout">
      <aside class="prompt-list glass-panel-heavy">
        <div v-for="group in groups" :key="group.key" class="prompt-group">
          <div class="group-label">
            <span>{{ group.label }}</span>
            <NButton
              v-if="group.key === 'custom'"
              quaternary
              circle
              size="tiny"
              @click="createOpen = true"
            >
              <template #icon><Plus :size="14" /></template>
            </NButton>
          </div>
          <button
            v-for="item in group.prompts"
            :key="item.key"
            type="button"
            class="prompt-item"
            :class="{ 'prompt-item--active': item.key === selectedKey }"
            @click="select(item.key)"
          >
            <span class="prompt-item__title">
              {{ item.title }}
              <Lock v-if="item.locked" :size="12" class="prompt-item__icon" />
              <span v-if="item.modified" class="prompt-item__dot" title="已修改" />
            </span>
            <code class="prompt-item__key">{{ item.key }}</code>
          </button>
        </div>
        <NButton
          v-if="!groups.some((g) => g.key === 'custom')"
          quaternary
          size="small"
          class="create-button"
          @click="createOpen = true"
        >
          <template #icon><Plus :size="14" /></template>
          新建共享片段
        </NButton>
      </aside>

      <section v-if="detail" class="prompt-editor glass-panel-heavy">
        <header class="editor-header">
          <div class="editor-title">
            <h2>{{ detail.title }}</h2>
            <code>{{ detail.key }}</code>
            <NTag v-if="detail.modified" size="small" :bordered="false" type="info">已修改</NTag>
            <NTag v-if="detail.locked && unlocked" size="small" :bordered="false" type="warning">已解锁</NTag>
          </div>
          <NText v-if="detail.updated_at" depth="3" class="editor-meta">
            {{ formatTime(detail.updated_at) }} · #{{ detail.version?.id }}
          </NText>
        </header>

        <p v-if="detail.description" class="editor-description">{{ detail.description }}</p>

        <div v-if="detail.locked && !unlocked" class="lock-banner">
          <Lock :size="14" />
          <span>已锁定</span>
          <NPopconfirm @positive-click="unlocked = true">
            <template #trigger>
              <NButton size="tiny" secondary>
                <template #icon><LockOpen :size="12" /></template>
                解锁编辑
              </NButton>
            </template>
            修改可能导致解析失败，确定解锁？
          </NPopconfirm>
        </div>

        <div v-if="Object.keys(detail.variables).length" class="variable-bar">
          <NTooltip v-for="(desc, name) in detail.variables" :key="name" placement="top">
            <template #trigger>
              <button type="button" class="variable-chip" :disabled="readonly" @click="insertToken(String(name))">
                {{ name }}
              </button>
            </template>
            {{ desc }}
          </NTooltip>
        </div>

        <div class="editor-body" :class="{ 'editor-body--split': preview }">
          <NInput
            ref="editorRef"
            v-model:value="draft"
            type="textarea"
            class="editor-input"
            :readonly="readonly"
            placeholder="Prompt 内容"
          />
          <div v-if="preview" class="preview-panel">
            <div class="preview-header">
              <span>渲染结果</span>
              <NText depth="3" class="char-count">{{ preview.length }} 字</NText>
              <NText v-if="preview.includes.length" depth="3" class="char-count">
                引入 {{ preview.includes.join('、') }}
              </NText>
            </div>
            <NSelect
              v-if="usesRuntime"
              v-model:value="previewConversation"
              :options="conversationOptions"
              size="small"
              filterable
              placeholder="选择会话"
            />
            <pre class="preview-text" :class="{ 'preview-text--loading': previewLoading }">{{ preview.text }}</pre>
            <NText v-if="preview.missing.length" depth="3" class="char-count">
              未提供值：{{ preview.missing.join('、') }}
            </NText>
          </div>
        </div>

        <div class="editor-footer">
          <NText depth="3" class="char-count">{{ draft.length }} 字</NText>
          <NText v-if="detail.used_by.length" depth="3" class="char-count">
            被 {{ detail.used_by.join('、') }} 引用
          </NText>
          <div class="spacer" />
          <NButton size="small" quaternary :type="preview ? 'primary' : 'default'" @click="togglePreview">
            <template #icon><Eye :size="14" /></template>
            预览
          </NButton>
          <NButton size="small" quaternary @click="openHistory">
            <template #icon><History :size="14" /></template>
            历史
          </NButton>
          <NButton
            v-if="!detail.is_custom && draft !== detail.default"
            size="small"
            quaternary
            :disabled="readonly"
            @click="draft = detail.default"
          >
            <template #icon><RotateCcw :size="14" /></template>
            填入默认
          </NButton>
          <NPopconfirm v-if="detail.is_custom" @positive-click="deletePrompt">
            <template #trigger>
              <NButton size="small" quaternary>
                <template #icon><Trash2 :size="14" /></template>
                删除
              </NButton>
            </template>
            删除片段及其全部历史？
          </NPopconfirm>
        </div>

        <div v-if="dirty" class="save-row">
          <NInput v-model:value="note" size="small" placeholder="修改备注（可选）" class="note-input" @keyup.enter="save()" />
          <NButton size="small" @click="draft = detail.content">放弃</NButton>
          <NButton size="small" type="primary" :loading="saving" @click="save()">
            <template #icon><Save :size="14" /></template>
            保存
          </NButton>
        </div>

      </section>
    </div>

    <NDrawer v-model:show="historyOpen" placement="right" :width="640">
      <NDrawerContent title="版本历史" closable body-content-style="padding: 0">
        <div class="history-layout">
          <div class="version-list">
            <div v-if="!versions.length" class="version-empty">尚无保存记录，当前为内置默认值</div>
            <button
              v-for="(version, index) in versions"
              :key="version.id"
              type="button"
              class="version-item"
              :class="{ 'version-item--active': version.id === compareId }"
              @click="compareId = version.id"
            >
              <span class="version-item__head">
                #{{ version.id }}
                <NTag v-if="index === 0" size="tiny" :bordered="false" type="success">当前</NTag>
              </span>
              <span class="version-item__time">{{ formatTime(version.created_at) }}</span>
              <span v-if="version.note" class="version-item__note">{{ version.note }}</span>
            </button>
          </div>
          <div v-if="compareVersion" class="diff-pane">
            <div class="diff-header">
              <span>#{{ compareVersion.id }} → 编辑区</span>
              <div class="spacer" />
              <NPopconfirm
                v-if="compareVersion.id !== detail?.version?.id"
                @positive-click="rollback(compareVersion)"
              >
                <template #trigger>
                  <NButton size="tiny" type="primary" secondary :disabled="readonly">回滚到此版本</NButton>
                </template>
                以 #{{ compareVersion.id }} 的内容保存为新版本？
              </NPopconfirm>
            </div>
            <div class="diff-body">
              <div v-for="(line, i) in diff" :key="i" class="diff-line" :class="`diff-line--${line.type}`">
                <span class="diff-sign">{{ line.type === 'add' ? '+' : line.type === 'del' ? '-' : ' ' }}</span>
                <span>{{ line.text || ' ' }}</span>
              </div>
            </div>
          </div>
        </div>
      </NDrawerContent>
    </NDrawer>

    <NDrawer v-model:show="createOpen" placement="right" :width="420">
      <NDrawerContent title="新建共享片段" closable>
        <div class="create-form">
          <label>
            <span>Key</span>
            <NInput v-model:value="createForm.key" placeholder="persona.core" />
          </label>
          <label>
            <span>名称</span>
            <NInput v-model:value="createForm.title" placeholder="核心人设" />
          </label>
          <label>
            <span>说明</span>
            <NInput v-model:value="createForm.description" />
          </label>
          <label>
            <span>内容</span>
            <NInput v-model:value="createForm.content" type="textarea" :autosize="{ minRows: 6, maxRows: 16 }" />
          </label>
          <NText depth="3" class="char-count">在其他 prompt 中用 <code>{{ includeToken }}</code> 引入</NText>
          <NButton type="primary" :disabled="!createForm.key" @click="createPrompt">创建</NButton>
        </div>
      </NDrawerContent>
    </NDrawer>
  </div>
</template>

<style scoped>
.prompts-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  height: calc(100vh - 96px);
}

.page-title {
  font-size: 1.5rem;
  font-weight: 600;
}

.prompts-layout {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 240px minmax(0, 1fr);
  gap: 16px;
}

/* ── 列表 ── */
.prompt-list {
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  overflow: auto;
}

.group-label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 10px;
  font-size: 0.72rem;
  letter-spacing: 0.04em;
  opacity: 0.58;
  min-height: 28px;
}

.prompt-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 100%;
  padding: 8px 10px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: background 0.15s ease;
}

.prompt-item:hover {
  background: rgba(127, 127, 148, 0.08);
}

.prompt-item--active,
.prompt-item--active:hover {
  background: rgba(81, 63, 224, 0.12);
}

.prompt-item__title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.9rem;
  font-weight: 500;
}

.prompt-item__icon {
  opacity: 0.5;
}

.prompt-item__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--brand-gradient-start);
}

.prompt-item__key {
  font-family: var(--font-mono);
  font-size: 0.75rem;
  opacity: 0.55;
}

.create-button {
  align-self: flex-start;
}

/* ── 编辑器 ── */
.prompt-editor {
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
  min-height: 0;
}

.editor-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.editor-title {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.editor-title h2 {
  font-size: 1.1rem;
  font-weight: 600;
}

.editor-title code {
  font-family: var(--font-mono);
  font-size: 0.82rem;
  opacity: 0.6;
}

.editor-meta,
.char-count {
  font-size: 0.78rem;
}

.editor-description {
  font-size: 0.88rem;
  opacity: 0.72;
  line-height: 1.5;
}

.lock-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: 8px;
  background: rgba(245, 169, 39, 0.12);
  font-size: 0.85rem;
}

.lock-banner span {
  flex: 1;
}

.variable-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.variable-chip {
  padding: 2px 8px;
  border: 1px solid var(--glass-border);
  border-radius: 6px;
  background: rgba(127, 127, 148, 0.08);
  color: inherit;
  font-family: var(--font-mono);
  font-size: 0.78rem;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
}

.variable-chip:hover:not(:disabled) {
  border-color: var(--brand-gradient-start);
  background: rgba(81, 63, 224, 0.1);
}

.variable-chip:disabled {
  cursor: default;
  opacity: 0.5;
}

.editor-body {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 12px;
}

.editor-body--split {
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
}

.editor-input {
  height: 100%;
}

.editor-input :deep(textarea) {
  font-family: var(--font-mono);
  font-size: 0.86rem;
  line-height: 1.65;
}

.editor-footer,
.save-row,
.preview-header,
.diff-header {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.note-input {
  flex: 1;
  min-width: 200px;
}

.spacer {
  flex: 1;
}

.preview-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
  border-radius: 8px;
  background: rgba(127, 127, 148, 0.06);
  min-height: 0;
}

.preview-header > span {
  font-weight: 600;
  font-size: 0.9rem;
}

.preview-text {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--font-mono);
  font-size: 0.82rem;
  line-height: 1.6;
  flex: 1;
  overflow: auto;
  transition: opacity 0.15s ease;
}

.preview-text--loading {
  opacity: 0.5;
}

/* ── 历史 ── */
.history-layout {
  display: grid;
  grid-template-columns: 180px minmax(0, 1fr);
  height: 100%;
}

.version-list {
  border-right: 1px solid var(--glass-border);
  padding: 8px;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.version-empty {
  padding: 12px;
  font-size: 0.82rem;
  opacity: 0.6;
}

.version-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 10px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.version-item:hover {
  background: rgba(127, 127, 148, 0.08);
}

.version-item--active,
.version-item--active:hover {
  background: rgba(81, 63, 224, 0.12);
}

.version-item__head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-family: var(--font-mono);
  font-size: 0.85rem;
  font-weight: 600;
}

.version-item__time,
.version-item__note {
  font-size: 0.75rem;
  opacity: 0.65;
}

.diff-pane {
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
}

.diff-header {
  padding: 10px 14px;
  border-bottom: 1px solid var(--glass-border);
  font-size: 0.85rem;
  font-weight: 600;
}

.diff-body {
  overflow: auto;
  padding: 8px 0;
  font-family: var(--font-mono);
  font-size: 0.8rem;
  line-height: 1.6;
}

.diff-line {
  display: flex;
  gap: 8px;
  padding: 0 14px;
  white-space: pre-wrap;
  word-break: break-word;
}

.diff-sign {
  flex-shrink: 0;
  width: 10px;
  opacity: 0.6;
}

.diff-line--add {
  background: rgba(130, 237, 48, 0.16);
}

.diff-line--del {
  background: rgba(225, 104, 78, 0.16);
  text-decoration: line-through;
  text-decoration-color: rgba(225, 104, 78, 0.5);
}

/* ── 新建 ── */
.create-form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.create-form label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 0.88rem;
  font-weight: 500;
}

@media (max-width: 900px) {
  .prompts-page {
    height: auto;
  }

  .prompts-layout,
  .editor-body--split {
    grid-template-columns: 1fr;
  }

  .editor-input {
    height: 60vh;
  }
}
</style>
