<script setup lang="ts">
import { computed } from 'vue'
import logoRaw from '@/assets/logo.svg?raw'

const props = defineProps<{
  variant?: 'horizontal' | 'vertical' | 'icon'
  size?: 'sm' | 'md' | 'lg'
}>()

const gradientDef = `<defs><linearGradient id="brand-grad" x1="0%" y1="0%" x2="100%" y2="100%">
  <stop offset="0%" stop-color="var(--brand-gradient-start)" />
  <stop offset="100%" stop-color="var(--brand-gradient-end)" />
</linearGradient></defs>`

const svgHtml = computed(() =>
  logoRaw
    .replace('fill="currentColor"', 'fill="url(#brand-grad)"')
    .replace(/<svg([^>]*)>/, `<svg$1>${gradientDef}`)
)

const showText = computed(() => props.variant !== 'icon')
</script>

<template>
  <div class="sophos-logo" :class="[`variant-${variant ?? 'horizontal'}`, `size-${size ?? 'md'}`]">
    <div class="logo-icon" v-html="svgHtml" />
    <span v-if="showText" class="logo-text">Sophos</span>
  </div>
</template>

<style scoped>
.sophos-logo {
  display: flex;
  align-items: center;
  gap: 8px;
  user-select: none;
}

.variant-vertical {
  flex-direction: column;
  gap: 12px;
}

.logo-icon {
  display: flex;
  flex-shrink: 0;
}

.logo-icon :deep(svg) {
  width: auto;
  display: block;
}

/* Size: sm (sidebar) */
.size-sm .logo-icon :deep(svg) { height: 42px; }
.size-sm .logo-text { font-size: 2.4rem; }

/* Size: md (default) */
.size-md .logo-icon :deep(svg) { height: 32px; }
.size-md .logo-text { font-size: 1.4rem; }

/* Size: lg (login page) */
.size-lg .logo-icon :deep(svg) { height: 64px; }
.size-lg .logo-text { font-size: 2.2rem; }

.logo-text {
  font-family: 'Allura', cursive;
  font-weight: bold;
  background: linear-gradient(
    135deg,
    var(--brand-gradient-start),
    var(--brand-gradient-end)
  );
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  line-height: 1;
}

/* Horizontal: text sinks slightly for visual balance */
.variant-horizontal .logo-text {
  transform: translateY(3px);
}
</style>
