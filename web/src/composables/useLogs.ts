import { computed, ref } from 'vue'
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
    } else {
      result.push(parsed)
    }
  }
  return result
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

  // SSE URL
  const streamUrl = computed(() => '/api/logs/stream')

  const { connected, connect, disconnect } = useLogStream(streamUrl, {
    onMessage(data: string) {
      try {
        const msg = JSON.parse(data) as { line: string; level: string }
        const parsed = parseLine(msg.line)
        // 续行合并
        if (parsed.level === '') {
          const target = isFollowing.value ? lines : buffer
          if (target.value.length > 0) {
            target.value[target.value.length - 1].continuation.push(msg.line)
            return
          }
        }

        if (isFollowing.value) {
          lines.value.push(parsed)
          // FIFO 裁剪
          if (lines.value.length > MAX_ENTRIES) {
            lines.value.splice(0, lines.value.length - MAX_ENTRIES)
          }
        } else {
          buffer.value.push(parsed)
        }
      } catch {
        // 忽略解析错误
      }
    },
    reconnectDelay: 3000,
  })

  // 过滤后的日志行
  const filteredLines = computed(() => {
    const minPriority = LEVEL_PRIORITY[minLevel.value] ?? 20
    const query = searchQuery.value.toLowerCase()

    return lines.value.filter((line) => {
      // 等级过滤：有等级的行按优先级过滤，续行（无等级）跟随上一条
      if (line.level) {
        const p = LEVEL_PRIORITY[line.level] ?? 20
        if (p < minPriority) return false
      }
      // 搜索过滤
      if (query) {
        const text = line.raw + line.continuation.join('\n')
        if (!text.toLowerCase().includes(query)) return false
      }
      return true
    })
  })

  const bufferedCount = computed(() => buffer.value.length)

  /** 加载初始日志 */
  async function loadInitial() {
    loading.value = true
    try {
      const { data } = await api.get(`/logs/${selectedFile.value}`, {
        params: { tail: INITIAL_ENTRIES },
      })
      lines.value = parseLines(data.lines as string[])
    } catch (e) {
      console.error('Failed to load logs:', e)
    } finally {
      loading.value = false
    }
  }

  /** 恢复跟踪：合并缓冲区 */
  function resumeFollowing() {
    lines.value.push(...buffer.value)
    buffer.value = []
    if (lines.value.length > MAX_ENTRIES) {
      lines.value.splice(0, lines.value.length - MAX_ENTRIES)
    }
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
    await loadInitial()
    if (filename === 'sophos.log') {
      connect()
    }
  }

  /** 启动：加载初始数据 + 连接 SSE */
  async function start() {
    await loadInitial()
    connect()
  }

  /** 停止：断开 SSE */
  function stop() {
    disconnect()
  }

  return {
    lines,
    filteredLines,
    buffer,
    bufferedCount,
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
