/**
 * WebUI 鉴权 composable。
 *
 * 使用 httpOnly cookie（后端设置），前端只需调用 API。
 */
import { ref } from 'vue'
import { useRouter } from 'vue-router'

const isAuthenticated = ref(false)
const authRequired = ref(true)

export function useAuth() {
  const router = useRouter()

  async function checkAuth(): Promise<{ authenticated: boolean; required: boolean }> {
    try {
      const res = await fetch('/api/auth/check')
      const data = await res.json()
      isAuthenticated.value = data.authenticated
      authRequired.value = data.required
      return data
    } catch {
      isAuthenticated.value = false
      authRequired.value = true
      return { authenticated: false, required: true }
    }
  }

  async function login(password: string): Promise<{ ok: boolean; error?: string }> {
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      })
      if (res.ok) {
        isAuthenticated.value = true
        return { ok: true }
      }
      const data = await res.json()
      return { ok: false, error: data.error || '密码错误' }
    } catch {
      return { ok: false, error: '网络错误' }
    }
  }

  async function logout(): Promise<void> {
    await fetch('/api/auth/logout', { method: 'POST' }).catch(() => {})
    isAuthenticated.value = false
    router.push({ name: 'login' })
  }

  return {
    isAuthenticated,
    authRequired,
    checkAuth,
    login,
    logout,
  }
}
