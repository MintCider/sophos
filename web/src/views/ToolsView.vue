<script setup lang="ts">
import {
  NButton,
  NCard,
  NInput,
  NSelect,
  NSpace,
  NSwitch,
  NTag,
  NText,
  useMessage,
} from 'naive-ui'
import {
  ChevronDown,
  ChevronRight,
  RotateCcw,
  Save,
} from 'lucide-vue-next'
import { computed, onMounted, reactive, ref } from 'vue'
import api from '@/api'

type ApiType = 'openai' | 'gemini'

interface CustomConfig {
  display_name: string
  provider_alias: string | null
  model_name: string | null
  api_type: ApiType
  send_as: string
}

interface ToolInfo {
  name: string
  description: string
  default_description: string
  has_custom_description: boolean
  category: string
  scope: string
  group: string
  is_builtin: boolean
  is_custom: boolean
  enabled: boolean
  can_disable: boolean
  parameters: Record<string, any>
  custom_config?: CustomConfig
}

interface ToolGroup {
  key: string
  label: string
  tools: ToolInfo[]
}

interface ImageConfig {
  provider_alias: string | null
  model_name: string
  api_type: ApiType
  send_as: string
  description: string
  available_providers: string[]
}

const message = useMessage()

const groups = ref<ToolGroup[]>([])
const loading = ref(false)

// 展开状态：按工具名记录，最多展开一个
const expandedTool = ref('')

// 每个工具的描述草稿（展开即可编辑；未修改时等同于当前描述）
const descDrafts = reactive<Record<string, string>>({})

// 图像生成配置草稿（展开即加载）
const imageConfigLoaded = ref(false)
const imageConfig = reactive<ImageConfig>({
  provider_alias: '',
  model_name: '',
  api_type: 'openai',
  send_as: 'image_url',
  description: '',
  available_providers: [],
})

const apiTypeOptions = [
  { label: 'OpenAI', value: 'openai' },
  { label: 'Gemini', value: 'gemini' },
]

const sendAsOptions = [
  { label: '图片消息', value: 'image_url' },
  { label: '文本链接', value: 'text' },
]

const toolCount = computed(() =>
  groups.value.reduce((sum, g) => sum + g.tools.length, 0),
)

const providerOptions = computed(() =>
  imageConfig.available_providers.map((alias) => ({ label: alias, value: alias })),
)

async function loadTools() {
  loading.value = true
  try {
    const { data } = await api.get('/tools')
    groups.value = data.groups as ToolGroup[]
  } catch (error: any) {
    message.error(error?.response?.data?.reason ?? '加载工具列表失败')
  } finally {
    loading.value = false
  }
}

function toggleExpand(tool: ToolInfo) {
  if (expandedTool.value === tool.name) {
    expandedTool.value = ''
    return
  }
  expandedTool.value = tool.name
  descDrafts[tool.name] = tool.description
  if (tool.name === 'generate_image') {
    void loadImageConfig()
  }
}

async function toggleEnabled(tool: ToolInfo, enabled: boolean) {
  try {
    await api.put(`/tools/${encodeURIComponent(tool.name)}/enabled`, { enabled })
    await loadTools()
  } catch (error: any) {
    message.error(error?.response?.data?.reason ?? '操作失败')
    await loadTools()
  }
}

function descChanged(tool: ToolInfo): boolean {
  const draft = descDrafts[tool.name]
  if (draft == null) return false
  return draft.trim() !== tool.description.trim()
}

async function saveDescription(tool: ToolInfo) {
  const draft = descDrafts[tool.name]?.trim() ?? ''
  if (!draft) {
    message.warning('描述不能为空')
    return
  }
  try {
    await api.put(`/tools/${encodeURIComponent(tool.name)}/description`, {
      description: draft,
    })
    message.success('描述已更新')
    await loadTools()
  } catch (error: any) {
    message.error(error?.response?.data?.reason ?? '更新描述失败')
  }
}

async function resetDescription(tool: ToolInfo) {
  try {
    await api.delete(`/tools/${encodeURIComponent(tool.name)}/description`)
    message.success('描述已恢复默认')
    delete descDrafts[tool.name]
    await loadTools()
  } catch (error: any) {
    message.error(error?.response?.data?.reason ?? '恢复默认失败')
  }
}

async function loadImageConfig() {
  try {
    const { data } = await api.get('/tools/image-generation')
    const cfg = data.config
    imageConfig.provider_alias = cfg.provider_alias ?? ''
    imageConfig.model_name = cfg.model_name ?? ''
    imageConfig.api_type = (cfg.api_type ?? 'openai') as ApiType
    imageConfig.send_as = cfg.send_as ?? 'image_url'
    imageConfig.description = cfg.description ?? ''
    imageConfig.available_providers = cfg.available_providers ?? []
    imageConfigLoaded.value = true
  } catch (error: any) {
    message.error(error?.response?.data?.reason ?? '加载图像生成配置失败')
  }
}

async function saveImageConfig(tool: ToolInfo) {
  try {
    // enabled 跟随当前工具的启用状态（顶部开关），不在此面板里管理
    await api.put('/tools/image-generation', {
      provider_alias: imageConfig.provider_alias || null,
      model_name: imageConfig.model_name || null,
      api_type: imageConfig.api_type,
      send_as: imageConfig.send_as,
      description: imageConfig.description,
      enabled: tool.enabled,
    })
    message.success('图像生成配置已保存')
    await loadTools()
  } catch (error: any) {
    message.error(error?.response?.data?.reason ?? '保存配置失败')
  }
}

onMounted(loadTools)
</script>

<template>
  <div class="tools-page">
    <div class="page-header">
      <div>
        <h1 class="page-title">工具管理</h1>
        <p class="page-subtitle">管理 LLM 可调用的工具及其描述、启用状态</p>
      </div>
      <div class="page-actions glass-panel">
        <NSpace align="center" :size="12">
          <NText depth="3">共 {{ toolCount }} 个工具</NText>
        </NSpace>
      </div>
    </div>

    <div class="tools-groups">
      <NCard
        v-for="group in groups"
        :key="group.key"
        class="tool-group-card glass-panel-heavy"
        :bordered="false"
      >
        <template #header>
          <div class="group-header">
            <span class="group-label">{{ group.label }}</span>
            <span class="group-count">{{ group.tools.length }}</span>
          </div>
        </template>

        <div class="tool-list">
          <div
            v-for="tool in group.tools"
            :key="tool.name"
            class="tool-entry"
          >
            <div
              class="tool-row"
              :class="{ 'tool-row--expanded': expandedTool === tool.name }"
              @click="toggleExpand(tool)"
            >
              <div class="tool-main">
                <div class="tool-title">
                  <code class="tool-name">{{ tool.name }}</code>
                  <NTag
                    v-if="tool.has_custom_description"
                    size="tiny"
                    :bordered="false"
                    type="info"
                  >
                    自定义描述
                  </NTag>
                  <NTag
                    v-if="!tool.is_builtin"
                    size="tiny"
                    :bordered="false"
                    type="warning"
                  >
                    自定义
                  </NTag>
                  <NTag
                    v-if="!tool.can_disable"
                    size="tiny"
                    :bordered="false"
                  >
                    必选
                  </NTag>
                </div>
                <p class="tool-desc">{{ tool.description }}</p>
              </div>

              <div class="tool-actions" @click.stop>
                <NSwitch
                  :value="tool.enabled"
                  :disabled="!tool.can_disable"
                  @update:value="(v: boolean) => toggleEnabled(tool, v)"
                />
                <NButton
                  quaternary
                  circle
                  size="small"
                  @click="toggleExpand(tool)"
                >
                  <template #icon>
                    <component
                      :is="expandedTool === tool.name ? ChevronDown : ChevronRight"
                      :size="18"
                    />
                  </template>
                </NButton>
              </div>
            </div>

            <!-- 展开区：图像生成配置（仅 generate_image）+ 描述编辑（所有工具） -->
            <div v-if="expandedTool === tool.name" class="tool-expanded">
              <!-- 图像生成专属配置 -->
              <div
                v-if="tool.name === 'generate_image'"
                class="panel-section"
              >
                <div class="section-header">
                  <span>图像生成配置</span>
                </div>

                <div v-if="!imageConfigLoaded" class="panel-loading">加载中...</div>

                <template v-else>
                  <div class="config-grid">
                    <label class="config-label">
                      <span>供应商</span>
                      <NSelect
                        v-model:value="imageConfig.provider_alias"
                        :options="providerOptions"
                        placeholder="选择供应商"
                        clearable
                        filterable
                      />
                    </label>

                    <label class="config-label">
                      <span>接口标准</span>
                      <NSelect
                        v-model:value="imageConfig.api_type"
                        :options="apiTypeOptions"
                      />
                    </label>

                    <label class="config-label">
                      <span>模型名称</span>
                      <NInput
                        v-model:value="imageConfig.model_name"
                        placeholder="例如 dall-e-3 / gpt-image-1"
                      />
                    </label>

                    <label class="config-label">
                      <span>发送方式</span>
                      <NSelect
                        v-model:value="imageConfig.send_as"
                        :options="sendAsOptions"
                      />
                    </label>
                  </div>

                  <p class="config-tip">
                    OpenAI 标准调用 <code>/images/generations</code>，Gemini 标准调用
                    <code>generateImages</code>。确保所选供应商已配置对应 Base URL。
                  </p>

                  <div class="action-row">
                    <div class="spacer" />
                    <NButton
                      size="small"
                      type="primary"
                      @click="saveImageConfig(tool)"
                    >
                      <template #icon><Save :size="14" /></template>
                      保存配置
                    </NButton>
                  </div>
                </template>
              </div>

              <!-- 工具描述编辑（通用） -->
              <div class="panel-section">
                <div class="section-header">
                  <span>工具描述</span>
                  <NText v-if="tool.has_custom_description" depth="3" class="section-hint">
                    当前使用自定义描述
                  </NText>
                </div>
                <NInput
                  v-model:value="descDrafts[tool.name]"
                  type="textarea"
                  :autosize="{ minRows: 3, maxRows: 10 }"
                  placeholder="给 LLM 看的工具描述..."
                />
                <div v-if="tool.has_custom_description" class="default-desc">
                  <span class="section-label">默认描述</span>
                  <p>{{ tool.default_description }}</p>
                </div>
                <div class="action-row">
                  <NButton
                    v-if="tool.has_custom_description"
                    size="small"
                    @click="resetDescription(tool)"
                  >
                    <template #icon><RotateCcw :size="14" /></template>
                    恢复默认
                  </NButton>
                  <div class="spacer" />
                  <NButton
                    size="small"
                    type="primary"
                    :disabled="!descChanged(tool)"
                    @click="saveDescription(tool)"
                  >
                    <template #icon><Save :size="14" /></template>
                    保存描述
                  </NButton>
                </div>
              </div>

              <!-- 详情 -->
              <div class="detail-panel">
                <div class="detail-item">
                  <span class="section-label">类别</span>
                  <code>{{ tool.category }}</code>
                </div>
                <div class="detail-item">
                  <span class="section-label">作用域</span>
                  <code>{{ tool.scope }}</code>
                </div>
              </div>
            </div>
          </div>
        </div>
      </NCard>

      <div
        v-if="!loading && groups.length === 0"
        class="tools-empty glass-panel-heavy"
      >
        暂无可用工具
      </div>
    </div>
  </div>
</template>

<style scoped>
.tools-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.page-actions {
  padding: 10px 14px;
  flex-shrink: 0;
}

.page-title {
  font-size: 1.5rem;
  font-weight: 600;
}

.page-subtitle {
  margin-top: 6px;
  opacity: 0.68;
}

.tools-groups {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.tool-group-card {
  padding: 0;
}

.tool-group-card :deep(.n-card__content) {
  padding: 0;
}

.tool-group-card :deep(.n-card-header) {
  padding: 16px 20px 12px;
}

.group-header {
  display: flex;
  align-items: center;
  gap: 10px;
}

.group-label {
  font-weight: 600;
  font-size: 1rem;
}

.group-count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 22px;
  height: 22px;
  padding: 0 6px;
  border-radius: 11px;
  background: rgba(127, 127, 148, 0.12);
  font-size: 0.75rem;
  font-weight: 600;
}

.tool-list {
  display: flex;
  flex-direction: column;
}

.tool-entry {
  border-top: 1px solid var(--glass-border);
}

.tool-entry:first-child {
  border-top: none;
}

.tool-row {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 14px 20px;
  cursor: pointer;
  transition: background 0.15s ease;
}

.tool-row:hover {
  background: rgba(127, 127, 148, 0.05);
}

.tool-row--expanded {
  background: rgba(127, 127, 148, 0.07);
}

.tool-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.tool-title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.tool-name {
  font-family: var(--font-mono);
  font-size: 0.9rem;
  font-weight: 600;
}

.tool-desc {
  font-size: 0.88rem;
  opacity: 0.72;
  line-height: 1.5;
  word-break: break-word;
  margin: 0;
}

.tool-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
  padding-top: 2px;
}

.tool-expanded {
  padding: 4px 20px 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.panel-section {
  padding: 14px;
  border-radius: 12px;
  background: rgba(127, 127, 148, 0.06);
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.panel-loading {
  font-size: 0.88rem;
  opacity: 0.6;
  padding: 6px 0;
}

.section-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  font-weight: 600;
  font-size: 0.9rem;
}

.section-hint {
  font-size: 0.78rem;
  font-weight: 400;
}

.section-label {
  display: block;
  font-size: 0.72rem;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  opacity: 0.58;
  margin-bottom: 4px;
}

.default-desc {
  padding: 10px 12px;
  border-radius: 8px;
  background: rgba(127, 127, 148, 0.08);
}

.default-desc p {
  font-size: 0.85rem;
  line-height: 1.55;
  opacity: 0.78;
  margin: 0;
}

.config-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.config-label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-weight: 500;
  font-size: 0.88rem;
}

.config-tip {
  font-size: 0.82rem;
  opacity: 0.68;
  line-height: 1.55;
  margin: 0;
}

.config-tip code {
  font-family: var(--font-mono);
  font-size: 0.82rem;
  padding: 1px 4px;
  border-radius: 4px;
  background: rgba(127, 127, 148, 0.12);
}

.detail-panel {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px 14px;
  padding: 6px 14px 0;
}

.detail-item code {
  font-family: var(--font-mono);
  font-size: 0.85rem;
}

.action-row {
  display: flex;
  gap: 8px;
  align-items: center;
}

.spacer {
  flex: 1;
}

.tools-empty {
  padding: 48px;
  text-align: center;
  opacity: 0.65;
}

@media (max-width: 767px) {
  .page-header {
    flex-direction: column;
    align-items: stretch;
  }

  .config-grid {
    grid-template-columns: 1fr;
  }
}
</style>
