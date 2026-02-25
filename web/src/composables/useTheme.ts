import { useColorMode, usePreferredDark } from '@vueuse/core'
import { computed, ref } from 'vue'

type Mode = 'auto' | 'light' | 'dark'

// Shared preference across all useTheme() calls
const preference = ref<Mode>(
  (localStorage.getItem('sophos-color-mode') as Mode) || 'auto'
)

export function useTheme() {
  const systemDark = usePreferredDark()

  const isDark = computed(() => {
    if (preference.value === 'auto') return systemDark.value
    return preference.value === 'dark'
  })

  function toggleMode() {
    const order: Mode[] = ['auto', 'light', 'dark']
    const idx = order.indexOf(preference.value)
    preference.value = order[(idx + 1) % order.length]
    localStorage.setItem('sophos-color-mode', preference.value)

    // Sync html class for CSS variables
    document.documentElement.classList.toggle('dark', isDark.value)
  }

  // Init html class
  document.documentElement.classList.toggle('dark', isDark.value)

  return {
    mode: preference,
    isDark,
    toggleMode,
  }
}
