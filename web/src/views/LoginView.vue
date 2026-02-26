<script setup lang="ts">
import { NInput, NButton, NIcon } from 'naive-ui'
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useAuth } from '@/composables/useAuth'
import { useTheme } from '@/composables/useTheme'
import SophosLogo from '@/components/SophosLogo.vue'
import { Sun, Moon, SunMoon } from 'lucide-vue-next'

const router = useRouter()
const { login } = useAuth()
const { mode, toggleMode } = useTheme()

const password = ref('')
const error = ref('')
const loading = ref(false)

const themeIcon = computed(() => {
  const icons: Record<string, typeof SunMoon> = { auto: SunMoon, light: Sun, dark: Moon }
  return icons[mode.value] ?? SunMoon
})

async function handleLogin() {
  if (!password.value) return
  loading.value = true
  error.value = ''
  const result = await login(password.value)
  loading.value = false
  if (result.ok) {
    router.replace({ name: 'dashboard' })
  } else {
    error.value = result.error ?? '密码错误'
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card glass-panel">
      <SophosLogo variant="vertical" size="lg" />
      <form class="login-form" style="margin-top: 28px" @submit.prevent="handleLogin">
        <NInput
          v-model:value="password"
          type="password"
          show-password-on="click"
          placeholder="密码"
          size="large"
          :status="error ? 'error' : undefined"
          @keyup.enter="handleLogin"
        />
        <p v-if="error" class="login-error">{{ error }}</p>
        <NButton
          type="primary"
          size="large"
          block
          :loading="loading"
          :disabled="!password"
          @click="handleLogin"
        >
          登录
        </NButton>
      </form>
    </div>
    <button class="theme-toggle" @click="toggleMode">
      <NIcon size="18"><component :is="themeIcon" /></NIcon>
    </button>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.login-card {
  width: 100%;
  max-width: 360px;
  padding: 40px 32px;
}

.login-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.login-form :deep(.n-input) {
  border-radius: 8px;
}

.login-form :deep(.n-button) {
  border-radius: 8px;
}

.login-error {
  margin: -8px 0 0;
  font-size: 13px;
  color: var(--error-color, #e1684e);
}

.theme-toggle {
  position: fixed;
  bottom: 20px;
  right: 20px;
  width: 40px;
  height: 40px;
  border-radius: 50%;
  border: 1px solid var(--glass-border);
  background: var(--glass-bg);
  backdrop-filter: blur(var(--glass-blur));
  -webkit-backdrop-filter: blur(var(--glass-blur));
  cursor: pointer;
  font-size: 18px;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: opacity 0.2s;
}

.theme-toggle:hover {
  opacity: 0.8;
}
</style>
