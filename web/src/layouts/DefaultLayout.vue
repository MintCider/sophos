<script setup lang="ts">
import {
  NLayout,
  NLayoutSider,
  NLayoutContent,
  NMenu,
  NButton,
  NTooltip,
} from 'naive-ui'
import { ref, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useTheme } from '@/composables/useTheme'
import { useAuth } from '@/composables/useAuth'
import SophosLogo from '@/components/SophosLogo.vue'

const router = useRouter()
const route = useRoute()
const { mode, toggleMode } = useTheme()
const { authRequired, logout } = useAuth()

const collapsed = ref(false)
const activeMenu = computed(() => (route.name as string) ?? 'dashboard')

const modeLabel = computed(() => {
  const labels: Record<string, string> = {
    auto: '跟随系统',
    light: '浅色',
    dark: '深色',
  }
  return labels[mode.value] ?? '跟随系统'
})

const modeIcon = computed(() => {
  const icons: Record<string, string> = {
    auto: '◐',
    light: '☀',
    dark: '☾',
  }
  return icons[mode.value] ?? '◐'
})

const menuOptions = [
  { label: '仪表盘', key: 'dashboard' },
  { label: '日志', key: 'logs' },
]

function handleMenuUpdate(key: string) {
  router.push({ name: key })
}
</script>

<template>
  <NLayout has-sider class="layout-root">
    <NLayoutSider
      bordered
      collapse-mode="width"
      :collapsed-width="64"
      :width="220"
      :collapsed="collapsed"
      show-trigger
      class="glass-sider"
      @collapse="collapsed = true"
      @expand="collapsed = false"
    >
      <div class="sider-header">
        <SophosLogo v-if="!collapsed" variant="horizontal" size="sm" />
        <SophosLogo v-else variant="icon" size="sm" />
      </div>
      <NMenu
        :collapsed="collapsed"
        :collapsed-width="64"
        :options="menuOptions"
        :value="activeMenu"
        @update:value="handleMenuUpdate"
      />
      <div class="sider-footer">
        <NTooltip v-if="authRequired" placement="right">
          <template #trigger>
            <NButton quaternary circle @click="logout">
              ⏻
            </NButton>
          </template>
          退出登录
        </NTooltip>
        <NTooltip placement="right">
          <template #trigger>
            <NButton quaternary circle @click="toggleMode">
              {{ modeIcon }}
            </NButton>
          </template>
          {{ modeLabel }}
        </NTooltip>
      </div>
    </NLayoutSider>
    <NLayoutContent class="main-content">
      <RouterView />
    </NLayoutContent>
  </NLayout>
</template>

<style scoped>
.layout-root {
  height: 100vh;
  background: transparent !important;
}

.glass-sider {
  backdrop-filter: blur(var(--glass-blur, 20px));
  background: var(--glass-bg) !important;
}

.sider-header {
  height: 72px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-bottom: 1px solid var(--glass-border);
}

.sider-footer {
  position: absolute;
  bottom: 12px;
  width: 100%;
  display: flex;
  justify-content: center;
  gap: 4px;
}

.main-content {
  padding: 24px;
  background: transparent;
}
</style>
