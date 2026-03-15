import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const srcDir = new URL('./src/', import.meta.url).pathname

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': srcDir,
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8080',
        changeOrigin: true,
      },
    },
  },
})
