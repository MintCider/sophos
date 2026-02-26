<script setup lang="ts">
import {
  NInput,
  NSelect,
  NSpace,
} from 'naive-ui'
import { h, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useLogs, LOG_LEVELS, type LogLevel } from '@/composables/useLogs'
import { useTheme } from '@/composables/useTheme'
import api from '@/api'

const { isDark } = useTheme()

const {
  filteredLines,
  bufferedCount,
  filteredBufferedCount,
  isFollowing,
  selectedFile,
  minLevel,
  searchQuery,
  loading,
  start,
  stop,
  resumeFollowing,
  pauseFollowing,
  switchFile,
} = useLogs()

// ── 日志文件列表 ──────────────────────────────────────

const fileOptions = ref<{ label: string; value: string }[]>([])

async function loadFileList() {
  try {
    const { data } = await api.get('/logs')
    fileOptions.value = (data.files as { name: string; size: number }[]).map(
      (f) => ({
        label: `${f.name} (${(f.size / 1024).toFixed(0)} KB)`,
        value: f.name,
      }),
    )
  } catch {
    fileOptions.value = [{ label: 'sophos.log', value: 'sophos.log' }]
  }
}

// ── 等级过滤选项（带颜色） ────────────────────────────────

const LEVEL_COLORS: Record<string, { light: string; dark: string }> = {
  CRITICAL: { light: '#d04a32', dark: '#e8877a' },
  ERROR: { light: '#d04a32', dark: '#e8877a' },
  WARNING: { light: '#c48510', dark: '#f7bc55' },
  INFO: { light: '#3d2cc0', dark: '#8578ed' },
  DEBUG: { light: '#6b6b78', dark: '#a0a0b0' },
  TRACE: { light: '#8b8b9a', dark: '#808090' },
}

function levelColor(level: string): string {
  const c = LEVEL_COLORS[level]
  if (!c) return 'inherit'
  return isDark.value ? c.dark : c.light
}

const levelOptions = LOG_LEVELS.map((l) => ({
  label: l,
  value: l,
}))

function renderLevelLabel(option: { label: string; value: string }) {
  return h('span', {
    style: { color: levelColor(option.value), fontWeight: 500 },
  }, option.label)
}

// ── 滚动与跟踪 ────────────────────────────────────────

const logContainer = ref<HTMLElement | null>(null)

function scrollToBottom() {
  const el = logContainer.value
  if (el) {
    el.scrollTop = el.scrollHeight
  }
}

function onScroll() {
  const el = logContainer.value
  if (!el) return
  // 距底部 50px 以内视为"在底部"
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50
  if (atBottom && !isFollowing.value) {
    resumeFollowing()
  } else if (!atBottom && isFollowing.value) {
    pauseFollowing()
  }
}

// 跟踪模式下自动滚到底部
watch(
  () => filteredLines.value.length,
  () => {
    if (isFollowing.value) {
      nextTick(scrollToBottom)
    }
  },
)

function handleResumeClick() {
  resumeFollowing()
  nextTick(scrollToBottom)
}

// ── 折叠状态（基于视觉行数） ──────────────────────────────

const expandedIds = ref(new Set<number>())
/** 记录哪些日志条目内容溢出了（需要折叠） */
const overflowIds = ref(new Set<number>())

function toggleExpand(id: number) {
  const isCollapsing = expandedIds.value.has(id)
  if (isCollapsing) {
    expandedIds.value.delete(id)
    // 收起后滚回到该条目位置
    nextTick(() => {
      const container = logContainer.value
      if (!container) return
      const el = container.querySelector<HTMLElement>(`[data-id="${id}"]`)
      if (el) {
        const lineEl = el.closest('.log-line') as HTMLElement | null
        if (lineEl) {
          lineEl.scrollIntoView({ block: 'nearest' })
        }
      }
    })
  } else {
    expandedIds.value.add(id)
  }
}

function formatJson(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    return text
  }
}

/** 渲染后检测哪些日志条目内容溢出了 3 行高度 */
function detectOverflow() {
  const container = logContainer.value
  if (!container) return
  const items = container.querySelectorAll<HTMLElement>('.log-line-body')
  const newSet = new Set<number>()
  items.forEach((el) => {
    const id = Number(el.dataset.id)
    if (el.scrollHeight > el.clientHeight + 1) {
      newSet.add(id)
    }
  })
  // 保留已展开条目的溢出标记
  for (const id of expandedIds.value) {
    if (overflowIds.value.has(id)) {
      newSet.add(id)
    }
  }
  overflowIds.value = newSet
}

// 日志变化后重新检测溢出
watch(
  () => filteredLines.value.length,
  () => {
    nextTick(detectOverflow)
  },
)

// ── 生命周期 ──────────────────────────────────────────

onMounted(async () => {
  await loadFileList()
  await start()
  nextTick(() => {
    scrollToBottom()
    detectOverflow()
  })
})

onUnmounted(() => {
  stop()
})
</script>

<template>
  <div class="logs-page">
    <!-- 工具栏 -->
    <div class="logs-toolbar glass-panel">
      <NSpace align="center" :size="12">
        <NSelect
          v-model:value="selectedFile"
          :options="fileOptions"
          size="small"
          style="width: 220px"
          @update:value="switchFile"
        />
        <NSelect
          v-model:value="minLevel"
          :options="levelOptions"
          :render-label="renderLevelLabel"
          size="small"
          style="width: 130px"
        />
        <NInput
          v-model:value="searchQuery"
          placeholder="搜索..."
          size="small"
          clearable
          style="width: 200px"
        />
      </NSpace>
    </div>

    <!-- 日志区域 -->
    <div class="logs-container glass-panel-heavy">
      <div class="logs-scroll" ref="logContainer" @scroll="onScroll">
        <div v-if="loading" class="logs-loading">加载中...</div>
        <div v-else-if="filteredLines.length === 0" class="logs-empty">
          暂无日志
        </div>
        <template v-else>
          <div
            v-for="line in filteredLines"
            :key="line.id"
            :class="['log-line', `log-line--${line.level.toLowerCase() || 'unknown'}`]"
        >
          <!-- 折叠指示器（绝对定位在左 margin 区域） -->
          <span
            v-if="overflowIds.has(line.id)"
            class="log-fold-indicator"
            @click="toggleExpand(line.id)"
          >{{ expandedIds.has(line.id) ? '▼' : '▶' }}</span>
          <!-- 首行：时间 + 等级 + 组件 -->
          <div class="log-line-header">
            <span class="log-ts">{{ line.ts }}</span>
            <span :class="['log-level', `log-level--${line.level.toLowerCase() || 'unknown'}`]">
              {{ line.level.padEnd(8) }}
            </span>
            <span class="log-logger">{{ line.logger }}</span>
          </div>
          <!-- 内容区：message + continuation，用 max-height 控制折叠 -->
          <div
            class="log-line-body"
            :class="{ 'log-line-body--collapsed': !expandedIds.has(line.id) }"
            :data-id="line.id"
          >
            <span class="log-message">{{ line.message }}</span>
            <template v-if="line.continuation.length > 0">
              <div class="log-continuation">{{ expandedIds.has(line.id)
                ? formatJson(line.continuation.join('\n'))
                : line.continuation.join('\n') }}</div>
            </template>
          </div>
          <!-- 折叠时的展开提示 -->
          <span
            v-if="!expandedIds.has(line.id) && overflowIds.has(line.id)"
            class="log-fold-bar"
            @click="toggleExpand(line.id)"
          >··· 展开 ···</span>
          <!-- 展开时的底部收起 -->
          <span
            v-if="expandedIds.has(line.id) && overflowIds.has(line.id)"
            class="log-fold-bar"
            @click="toggleExpand(line.id)"
          >··· 收起 ···</span>
        </div>
      </template>
      </div>
    </div>

    <!-- 浮动提示：有新日志 -->
    <Transition name="fade">
      <div
        v-if="filteredBufferedCount > 0 && !isFollowing"
        class="logs-new-indicator"
        @click="handleResumeClick"
      >
        {{ filteredBufferedCount }} 条新日志 ▼
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.logs-page {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 48px - 48px);
  position: relative;
}

.logs-toolbar {
  flex-shrink: 0;
  margin-bottom: 12px;
  padding: 8px 12px;
}

/* ── 工具栏控件圆角 ──────────────────────────────────── */

.logs-toolbar :deep(.n-input),
.logs-toolbar :deep(.n-base-selection) {
  border-radius: 8px;
}

.logs-container {
  flex: 1;
  overflow: hidden;
  padding: 12px 16px;
  font-family: var(--font-mono);
  font-size: 13px;
  line-height: 1.6;
}

.logs-scroll {
  height: 100%;
  overflow-y: auto;
  overflow-x: hidden;
  border-radius: 8px;
}

.logs-loading,
.logs-empty {
  text-align: center;
  padding: 40px;
  opacity: 0.5;
}

/* ── 日志行 ──────────────────────────────────────────── */

.log-line {
  padding: 2px 24px;
  border-radius: 4px;
  white-space: pre-wrap;
  word-break: break-all;
  position: relative;
}

.log-line-header {
  display: flex;
  align-items: baseline;
}

.log-ts {
  opacity: 0.6;
  margin-right: 1ch;
  flex-shrink: 0;
}

.log-level {
  display: inline-block;
  width: 9ch;
  margin-right: 1ch;
  flex-shrink: 0;
}

.log-logger {
  opacity: 0.5;
  margin-right: 1ch;
  flex-shrink: 0;
}

/* ── 内容区折叠 ──────────────────────────────────────── */

.log-line-body {
  overflow: hidden;
}

.log-line-body--collapsed {
  max-height: calc(3 * 1.6em);
}

.log-continuation {
  margin-left: 2ch;
  opacity: 0.8;
  white-space: pre-wrap;
}

/* ── 折叠指示器（disclosure triangle） ────────────────── */

.log-fold-indicator {
  position: absolute;
  left: 6px;
  top: 4px;
  width: 14px;
  cursor: pointer;
  opacity: 0.4;
  font-size: 10px;
  user-select: none;
  text-align: center;
}

.log-fold-indicator:hover {
  opacity: 0.8;
}

/* ── 折叠/展开操作条 ──────────────────────────────────── */

.log-fold-bar {
  display: block;
  text-align: center;
  cursor: pointer;
  opacity: 0.35;
  font-size: 11px;
  user-select: none;
  letter-spacing: 1px;
  padding: 1px 0;
}

.log-fold-bar:hover {
  opacity: 0.7;
}

/* ── 行背景色 ──────────────────────────────────────────── */

.log-line--critical,
.log-line--error { background: rgba(225, 104, 78, 0.14); }
.log-line--warning { background: rgba(245, 169, 39, 0.14); }
.log-line--info { background: rgba(81, 63, 224, 0.10); }
.log-line--debug { background: rgba(139, 139, 154, 0.08); }
.log-line--trace { background: rgba(107, 107, 120, 0.06); }

/* ── level 标签文字色（浅色模式用暗色调，深色模式用亮色调） */

.log-level--critical,
.log-level--error { color: #d04a32; }
.log-level--warning { color: #c48510; }
.log-level--info { color: #3d2cc0; }
.log-level--debug { color: #6b6b78; }
.log-level--trace { color: #8b8b9a; }

/* ── 深色模式微调 ──────────────────────────────────────── */

:global(html.dark) .log-line--critical,
:global(html.dark) .log-line--error { background: rgba(225, 104, 78, 0.16); }
:global(html.dark) .log-line--warning { background: rgba(245, 169, 39, 0.14); }
:global(html.dark) .log-line--info { background: rgba(81, 63, 224, 0.14); }
:global(html.dark) .log-level--critical,
:global(html.dark) .log-level--error { color: #e8877a; }
:global(html.dark) .log-level--warning { color: #f7bc55; }
:global(html.dark) .log-level--info { color: #8578ed; }
:global(html.dark) .log-level--debug { color: #a0a0b0; }
:global(html.dark) .log-level--trace { color: #808090; }

/* ── 浮动提示 ──────────────────────────────────────────── */

.logs-new-indicator {
  position: absolute;
  bottom: 24px;
  left: 50%;
  transform: translateX(-50%);
  padding: 8px 20px;
  border-radius: 20px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 500;
  background: var(--glass-bg-heavy);
  backdrop-filter: blur(var(--glass-blur));
  -webkit-backdrop-filter: blur(var(--glass-blur));
  border: 1.5px solid var(--primary-color, #513fe0);
  box-shadow: 0 0 0 3px rgba(81, 63, 224, 0.15), var(--glass-shadow);
  transition: opacity 0.2s;
}

.logs-new-indicator:hover {
  opacity: 0.85;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.3s;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

@media (max-width: 767px) {
  .logs-page {
    height: calc(100vh - 48px - 32px);
  }
}
</style>
