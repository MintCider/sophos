<script setup lang="ts">
import {
  NLayout,
  NLayoutSider,
  NLayoutContent,
  NMenu,
  NButton,
  NTooltip,
  NDrawer,
  NDrawerContent,
  NIcon,
} from 'naive-ui'
import { ref, computed, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useMediaQuery } from '@vueuse/core'
import { useTheme } from '@/composables/useTheme'
import { useAuth } from '@/composables/useAuth'
import SophosLogo from '@/components/SophosLogo.vue'
import { renderIcon } from '@/utils/renderIcon'
import {
  Gauge,
  ScrollText,
  Orbit,
  Wrench,
  LogOut,
  Sun,
  Moon,
  SunMoon,
  PanelLeftClose,
  PanelLeftOpen,
} from 'lucide-vue-next'

const router = useRouter()
const route = useRoute()
const { mode, toggleMode } = useTheme()
const { authRequired, logout } = useAuth()

// ── 响应式 ──
const isMobile = useMediaQuery('(max-width: 767px)')

// ── 侧边栏 / 抽屉状态 ──
const sidebarOpen = ref(true)
const drawerVisible = ref(false)
const collapsed = computed(() => !sidebarOpen.value)

function toggleSidebar() {
  if (isMobile.value) {
    drawerVisible.value = !drawerVisible.value
  } else {
    sidebarOpen.value = !sidebarOpen.value
  }
}

// 切到桌面时关闭抽屉
watch(isMobile, (mobile) => {
  if (!mobile) drawerVisible.value = false
})

// ── 菜单 ──
const activeMenu = computed(() => (route.name as string) ?? 'dashboard')

const menuOptions = [
  { label: '仪表盘', key: 'dashboard', icon: renderIcon(Gauge) },
  { label: '日志', key: 'logs', icon: renderIcon(ScrollText) },
  { label: '模型服务商', key: 'providers', icon: renderIcon(Orbit) },
  { label: '工具管理', key: 'tools', icon: renderIcon(Wrench) },
]

function handleMenuUpdate(key: string) {
  router.push({ name: key })
  if (isMobile.value) drawerVisible.value = false
}

// ── 主题图标 ──
const themeIcon = computed(() => {
  const icons: Record<string, typeof SunMoon> = { auto: SunMoon, light: Sun, dark: Moon }
  return icons[mode.value] ?? SunMoon
})

const modeLabel = computed(() => {
  const labels: Record<string, string> = { auto: '跟随系统', light: '浅色', dark: '深色' }
  return labels[mode.value] ?? '跟随系统'
})
</script>

<template>
  <!-- 顶栏 -->
  <div class="app-topbar">
    <div class="topbar-left">
      <NButton quaternary circle @click="toggleSidebar">
        <template #icon>
          <NIcon size="20">
            <PanelLeftOpen v-if="isMobile ? !drawerVisible : collapsed" />
            <PanelLeftClose v-else />
          </NIcon>
        </template>
      </NButton>
      <SophosLogo variant="horizontal" size="sm" />
    </div>
    <div class="topbar-right">
      <NTooltip placement="bottom">
        <template #trigger>
          <NButton quaternary circle @click="toggleMode">
            <template #icon>
              <NIcon size="18"><component :is="themeIcon" /></NIcon>
            </template>
          </NButton>
        </template>
        {{ modeLabel }}
      </NTooltip>
      <NTooltip v-if="authRequired" placement="bottom">
        <template #trigger>
          <NButton quaternary circle @click="logout">
            <template #icon>
              <NIcon size="18"><LogOut /></NIcon>
            </template>
          </NButton>
        </template>
        退出登录
      </NTooltip>
    </div>
  </div>

  <!-- 主布局 -->
  <NLayout has-sider class="layout-root">
    <!-- 桌面侧边栏 -->
    <NLayoutSider
      v-if="!isMobile"
      bordered
      collapse-mode="width"
      :collapsed-width="64"
      :width="220"
      :collapsed="collapsed"
      class="glass-sider"
    >
      <NMenu
        :collapsed="collapsed"
        :collapsed-width="64"
        :options="menuOptions"
        :value="activeMenu"
        @update:value="handleMenuUpdate"
      />
    </NLayoutSider>

    <!-- 移动端抽屉 -->
    <NDrawer
      v-if="isMobile"
      v-model:show="drawerVisible"
      placement="left"
      :width="260"
    >
      <NDrawerContent body-content-style="padding: 0">
        <NMenu
          :options="menuOptions"
          :value="activeMenu"
          @update:value="handleMenuUpdate"
        />
      </NDrawerContent>
    </NDrawer>

    <NLayoutContent class="main-content">
      <RouterView />
    </NLayoutContent>
  </NLayout>
</template>

<style scoped>
/* ── 顶栏 ── */
.app-topbar {
  height: 48px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 12px;
  border-bottom: 1px solid var(--glass-border);
  backdrop-filter: blur(var(--glass-blur, 20px));
  -webkit-backdrop-filter: blur(var(--glass-blur, 20px));
  background: var(--glass-bg);
  position: sticky;
  top: 0;
  z-index: 100;
}

.topbar-left,
.topbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* ── 布局 ── */
.layout-root {
  height: calc(100vh - 48px);
  background: transparent !important;
}

.glass-sider {
  backdrop-filter: blur(var(--glass-blur, 20px));
  background: var(--glass-bg) !important;
}

.main-content {
  padding: 24px;
  background: transparent;
}

@media (max-width: 767px) {
  .main-content {
    padding: 16px;
  }
}
</style>
