import { h, type Component } from 'vue'
import { NIcon } from 'naive-ui'

/** NMenu 图标渲染辅助：将 Lucide 组件包装为 NIcon render 函数 */
export function renderIcon(icon: Component) {
  return () => h(NIcon, null, { default: () => h(icon) })
}
