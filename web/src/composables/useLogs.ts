import { computed, ref, watch } from 'vue'
import api from '@/api'
import { useLogStream } from './useLogStream'

// ── 类型 ──────────────────────────────────────────────

export interface LogLine {
  id: number
  raw: string
  level: string
  ts: string
  logger: string
  message: string
  contentLength: number
  /** 多行消息的续行 */
  continuation: string[]
}

export const LOG_LEVELS = [
  'TRACE',
  'DEBUG',
  'INFO',
  'WARNING',
  'ERROR',
  'CRITICAL',
] as const

export type LogLevel = (typeof LOG_LEVELS)[number]

const LEVEL_PRIORITY: Record<string, number> = {
  TRACE: 5,
  DEBUG: 10,
  INFO: 20,
  WARNING: 30,
  ERROR: 40,
  CRITICAL: 50,
}

const MAX_ENTRIES = 5000
const INITIAL_ENTRIES = 500

// 解析日志行
const LOG_RE = /^(\S+ \S+) \| (\w+)\s+\| (\S+) \| (.*)$/

let nextId = 0

function parseLine(raw: string): LogLine {
  const m = LOG_RE.exec(raw)
  if (m) {
    return {
      id: nextId++,
      raw,
      ts: m[1],
      level: m[2],
      logger: m[3],
      message: m[4],
      contentLength: m[4].length,
      continuation: [],
    }
  }
  // 无法解析的行（续行或格式异常）
  return {
    id: nextId++,
    raw,
    ts: '',
    level: '',
    logger: '',
    message: raw,
    contentLength: raw.length,
    continuation: [],
  }
}

/**
 * 将原始行数组解析为 LogLine[]，合并续行到上一条日志。
 */
function parseLines(rawLines: string[]): LogLine[] {
  const result: LogLine[] = []
  for (const raw of rawLines) {
    const parsed = parseLine(raw)
    if (parsed.level === '' && result.length > 0) {
      // 续行：合并到上一条
      result[result.length - 1].continuation.push(raw)
      result[result.length - 1].contentLength += raw.length + 1
    } else {
      result.push(parsed)
    }
  }
  return result
}

/** 过滤谓词：等级 + 搜索词 */
function matchesFilter(line: LogLine, minPriority: number, query: string): boolean {
  if (line.level) {
    const p = LEVEL_PRIORITY[line.level] ?? 20
    if (p < minPriority) return false
  }
  if (query) {
    if (
      !line.raw.toLowerCase().includes(query)
      && !line.continuation.some((part) => part.toLowerCase().includes(query))
    ) return false
  }
  return true
}

// ── Composable ────────────────────────────────────────

export function useLogs() {
  const lines = ref<LogLine[]>([])
  const buffer = ref<LogLine[]>([])
  const isFollowing = ref(true)
  const selectedFile = ref('sophos.log')
  const minLevel = ref<LogLevel>('INFO')
  const searchQuery = ref('')
  const loading = ref(false)
  let started = false
  let loadSequence = 0

  // SSE URL
  const streamUrl = computed(() => `/api/logs/stream?level=${minLevel.value}`)

  function trim(target: LogLine[]) {
    if (target.length > MAX_ENTRIES) {
      target.splice(0, target.length - MAX_ENTRIES)
    }
  }

  function appendRawLines(target: LogLine[], rawLines: string[]) {
    for (const raw of rawLines) {
      const parsed = parseLine(raw)
      if (parsed.level === '' && target.length > 0) {
        target[target.length - 1].continuation.push(raw)
        target[target.length - 1].contentLength += raw.length + 1
      } else {
        target.push(parsed)
      }
    }
    trim(target)
  }

  const { connected, connect, disconnect } = useLogStream(streamUrl, {
    onMessage(data: string) {
      try {
        const msg = JSON.parse(data) as { lines: string[] }
        const target = isFollowing.value ? lines.value : buffer.value
        appendRawLines(target, msg.lines)
      } catch {
        // 忽略解析错误
      }
    },
    onReset() {
      void reloadCurrent()
    },
    reconnectDelay: 3000,
  })

  // 过滤后的日志行
  const filteredLines = computed(() => {
    const minPriority = LEVEL_PRIORITY[minLevel.value] ?? 20
    const query = searchQuery.value.toLowerCase()
    return lines.value.filter((l) => matchesFilter(l, minPriority, query))
  })

  const bufferedCount = computed(() => buffer.value.length)

  /** buffer 中匹配当前过滤条件的条目数 */
  const filteredBufferedCount = computed(() => {
    const minPriority = LEVEL_PRIORITY[minLevel.value] ?? 20
    const query = searchQuery.value.toLowerCase()
    return buffer.value.filter((l) => matchesFilter(l, minPriority, query)).length
  })

  /** 加载初始日志 */
  async function loadInitial(): Promise<string | null> {
    const sequence = ++loadSequence
    const filename = selectedFile.value
    const level = minLevel.value
    loading.value = true
    try {
      const { data } = await api.get(`/logs/${filename}`, {
        params: { tail: INITIAL_ENTRIES, level },
      })
      if (sequence !== loadSequence) return null
      lines.value = parseLines(data.lines as string[])
      return data.cursor as string
    } catch (e) {
      if (sequence === loadSequence) {
        console.error('Failed to load logs:', e)
      }
      return null
    } finally {
      if (sequence === loadSequence) {
        loading.value = false
      }
    }
  }

  /** 重新加载当前选择，并用快照游标无缝续接实时流。 */
  async function reloadCurrent() {
    disconnect()
    buffer.value = []
    const cursor = await loadInitial()
    if (
      cursor !== null
      && started
      && selectedFile.value === 'sophos.log'
    ) {
      connect(cursor)
    }
  }

  /** 恢复跟踪：合并缓冲区 */
  function resumeFollowing() {
    lines.value.push(...buffer.value)
    buffer.value = []
    trim(lines.value)
    isFollowing.value = true
  }

  /** 暂停跟踪 */
  function pauseFollowing() {
    isFollowing.value = false
  }

  /** 切换日志文件（查看轮转文件时断开 SSE） */
  async function switchFile(filename: string) {
    selectedFile.value = filename
    disconnect()
    buffer.value = []
    isFollowing.value = filename === 'sophos.log'
    const cursor = await loadInitial()
    if (cursor !== null && started && filename === 'sophos.log') {
      connect(cursor)
    }
  }

  /** 启动：加载初始数据 + 连接 SSE */
  async function start() {
    started = true
    const cursor = await loadInitial()
    if (cursor !== null) {
      connect(cursor)
    }
  }

  /** 停止：断开 SSE */
  function stop() {
    started = false
    loadSequence += 1
    disconnect()
  }

  watch(minLevel, () => {
    if (started) {
      void reloadCurrent()
    }
  })

  return {
    lines,
    filteredLines,
    buffer,
    bufferedCount,
    filteredBufferedCount,
    isFollowing,
    selectedFile,
    minLevel,
    searchQuery,
    loading,
    connected,
    start,
    stop,
    resumeFollowing,
    pauseFollowing,
    switchFile,
  }
}
