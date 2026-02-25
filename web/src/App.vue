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
    bodyColor: isDark.value ? '#101014' : '#f5f5f7',
    cardColor: isDark.value
      ? 'rgba(36, 36, 42, 0.7)'
      : 'rgba(255, 255, 255, 0.7)',
    modalColor: isDark.value
      ? 'rgba(36, 36, 42, 0.85)'
      : 'rgba(255, 255, 255, 0.85)',
    popoverColor: isDark.value
      ? 'rgba(44, 44, 50, 0.9)'
      : 'rgba(255, 255, 255, 0.9)',
    primaryColor: '#7c6ef0',
    primaryColorHover: '#9b8cf8',
    primaryColorPressed: '#6355d8',
    primaryColorSuppl: '#9b8cf8',
  },
  Card: {
    borderRadius: '12px',
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
