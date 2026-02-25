import { ref, type Ref } from 'vue'

export interface SSEOptions {
  /** 收到一条 SSE data 消息时的回调 */
  onMessage: (data: string) => void
  /** 连接断开后自动重连延迟（ms），0 表示不重连 */
  reconnectDelay?: number
}

/** 心跳超时（ms）。后端每 5s 发一次心跳，15s 无响应视为断开。 */
const HEARTBEAT_TIMEOUT = 15_000

export function useLogStream(url: Ref<string>, options: SSEOptions) {
  const connected = ref(false)
  let source: EventSource | null = null
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let watchdogTimer: ReturnType<typeof setInterval> | null = null
  let lastActivity = 0

  function resetActivity() {
    lastActivity = Date.now()
  }

  function startWatchdog() {
    stopWatchdog()
    watchdogTimer = setInterval(() => {
      if (lastActivity > 0 && Date.now() - lastActivity > HEARTBEAT_TIMEOUT) {
        // 心跳超时，强制断开并重连
        connected.value = false
        source?.close()
        source = null
        stopWatchdog()
        const delay = options.reconnectDelay ?? 3000
        if (delay > 0) {
          reconnectTimer = setTimeout(connect, delay)
        }
      }
    }, 3000)
  }

  function stopWatchdog() {
    if (watchdogTimer) {
      clearInterval(watchdogTimer)
      watchdogTimer = null
    }
  }

  function connect() {
    disconnect()
    source = new EventSource(url.value)

    source.onopen = () => {
      connected.value = true
      resetActivity()
      startWatchdog()
    }

    source.onmessage = (event) => {
      resetActivity()
      options.onMessage(event.data)
    }

    // 监听心跳事件（不触发 onmessage，需要单独监听）
    source.addEventListener('heartbeat', () => {
      resetActivity()
    })

    source.onerror = () => {
      connected.value = false
      source?.close()
      source = null
      stopWatchdog()
      // 自动重连
      const delay = options.reconnectDelay ?? 3000
      if (delay > 0) {
        reconnectTimer = setTimeout(connect, delay)
      }
    }
  }

  function disconnect() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    stopWatchdog()
    if (source) {
      source.close()
      source = null
    }
    connected.value = false
    lastActivity = 0
  }

  return { connected, connect, disconnect }
}
