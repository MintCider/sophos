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

const router = useRouter()
const route = useRoute()
const { mode, toggleMode } = useTheme()

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
        <span v-show="!collapsed" class="logo-text">Sophos</span>
        <span v-show="collapsed" class="logo-text">S</span>
      </div>
      <NMenu
        :collapsed="collapsed"
        :collapsed-width="64"
        :options="menuOptions"
        :value="activeMenu"
        @update:value="handleMenuUpdate"
      />
      <div class="sider-footer">
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
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-bottom: 1px solid var(--glass-border);
}

.logo-text {
  font-size: 1.25rem;
  font-weight: 700;
  letter-spacing: 0.05em;
}

.sider-footer {
  position: absolute;
  bottom: 12px;
  width: 100%;
  display: flex;
  justify-content: center;
}

.main-content {
  padding: 24px;
  background: transparent;
}
</style>
