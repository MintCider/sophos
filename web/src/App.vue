<script setup lang="ts">
import {
  NConfigProvider,
  NMessageProvider,
  darkTheme,
  type GlobalThemeOverrides,
} from 'naive-ui'
import { computed, watchEffect } from 'vue'
import { useTheme } from '@/composables/useTheme'
import DefaultLayout from '@/layouts/DefaultLayout.vue'

const { isDark } = useTheme()

const theme = computed(() => (isDark.value ? darkTheme : null))

// Keep html class in sync for CSS variables
watchEffect(() => {
  document.documentElement.classList.toggle('dark', isDark.value)
})

const themeOverrides = computed<GlobalThemeOverrides>(() => ({
  common: {
    bodyColor: isDark.value ? '#161228' : '#eee8ff',
    cardColor: isDark.value
      ? 'rgba(36, 36, 42, 0.7)'
      : 'rgba(255, 255, 255, 0.7)',
    modalColor: isDark.value
      ? 'rgba(36, 36, 42, 0.85)'
      : 'rgba(255, 255, 255, 0.85)',
    popoverColor: isDark.value
      ? 'rgba(44, 44, 50, 0.9)'
      : 'rgba(255, 255, 255, 0.9)',
    primaryColor: '#513fe0',
    primaryColorHover: '#7265e8',
    primaryColorPressed: '#3d2cc0',
    primaryColorSuppl: '#8578ed',
    successColor: '#82ed30',
    successColorHover: '#9af25c',
    successColorPressed: '#6ad818',
    successColorSuppl: '#a8f470',
    warningColor: '#f5a927',
    warningColorHover: '#f7bc55',
    warningColorPressed: '#e09510',
    warningColorSuppl: '#f8c56a',
    errorColor: '#e1684e',
    errorColorHover: '#e8877a',
    errorColorPressed: '#d04a32',
    errorColorSuppl: '#ec9d8e',
    infoColor: '#20c7fd',
    infoColorHover: '#50d4fd',
    infoColorPressed: '#08b4ec',
    infoColorSuppl: '#6cdbfe',
    fontFamily: "'Inter', 'PingFang SC', 'Microsoft YaHei', 'Noto Sans SC', sans-serif",
  },
  Card: {
    borderRadius: '12px',
  },
  Layout: {
    color: 'transparent',
    siderColor: 'transparent',
  },
}))
</script>

<template>
  <NConfigProvider :theme="theme" :theme-overrides="themeOverrides">
    <NMessageProvider>
      <DefaultLayout />
    </NMessageProvider>
  </NConfigProvider>
</template>
