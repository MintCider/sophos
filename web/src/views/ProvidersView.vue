<script setup lang="ts">
import {
  NButton,
  NCard,
  NDrawer,
  NDrawerContent,
  NInput,
  NSpace,
  NTag,
  NText,
  useMessage,
} from 'naive-ui'
import {
  ChevronDown,
  ChevronRight,
  Eye,
  EyeOff,
  RefreshCw,
  Pencil,
  Plus,
  Search,
  Save,
  Trash2,
  X,
} from 'lucide-vue-next'
import { computed, onMounted, reactive, ref } from 'vue'
import api from '@/api'
import { useTheme } from '@/composables/useTheme'
import openaiIcon from '@lobehub/icons-static-svg/icons/openai.svg'
import anthropicIcon from '@lobehub/icons-static-svg/icons/anthropic.svg'
import geminiIcon from '@lobehub/icons-static-svg/icons/gemini.svg'

type ApiType = 'openai' | 'anthropic' | 'gemini'

interface ActiveSlot {
  key: string
  model: string
  api_type: ApiType
}

interface ProviderItem {
  alias: string
  base_urls: Partial<Record<ApiType, string>>
  preferred_api_type: ApiType
  preferred_base_url: string
  api_key_masked: string
  models: string[]
  model_count: number
  active_slots: ActiveSlot[]
  request_policy: Record<string, unknown>
}

const message = useMessage()
const { isDark } = useTheme()

const providers = ref<ProviderItem[]>([])
const loading = ref(false)
const refreshingModels = ref(new Set<string>())

const expandedAlias = ref('')
const revealedKeys = reactive<Record<string, string>>({})
const editingAliases = ref(new Set<string>())
const advancedAddOpen = ref(false)
const addDrawerOpen = ref(false)
const deleteDrawerOpen = ref(false)
const deletingProvider = ref<ProviderItem | null>(null)
const modelSearch = reactive<Record<string, string>>({})

const editDrafts = reactive<Record<string, Partial<Record<ApiType, string>>>>({})
const requestPolicyDrafts = reactive<Record<string, string>>({})

const addForm = reactive<{
  alias: string
  apiKey: string
  openai: string
  anthropic: string
  gemini: string
  requestPolicy: string
}>({
  alias: '',
  apiKey: '',
  openai: 'https://api.openai.com/v1',
  anthropic: '',
  gemini: '',
  requestPolicy: '{}',
})

const apiTypes: ApiType[] = ['openai', 'anthropic', 'gemini']

const providerCountLabel = computed(() => `${providers.value.length} 个模型服务商`)

function providerLabel(apiType: ApiType): string {
  return {
    openai: 'OpenAI',
    anthropic: 'Anthropic',
    gemini: 'Gemini',
  }[apiType]
}

function toggleExpanded(alias: string) {
  if (expandedAlias.value === alias) {
    expandedAlias.value = ''
    return
  }
  expandedAlias.value = alias
}

async function toggleReveal(alias: string) {
  if (alias in revealedKeys) {
    delete revealedKeys[alias]
    return
  }
  try {
    const { data } = await api.get(`/providers/${encodeURIComponent(alias)}/api-key`)
    revealedKeys[alias] = data.api_key
  } catch (error: any) {
    message.error(error?.response?.data?.message ?? '获取 API Key 失败')
  }
}

function isEditing(alias: string) {
  return editingAliases.value.has(alias)
}

function beginEdit(provider: ProviderItem) {
  editingAliases.value.add(provider.alias)
  editDrafts[provider.alias] = {
    openai: provider.base_urls.openai ?? '',
    anthropic: provider.base_urls.anthropic ?? '',
    gemini: provider.base_urls.gemini ?? '',
  }
  requestPolicyDrafts[provider.alias] = JSON.stringify(provider.request_policy ?? {}, null, 2)
  expandedAlias.value = provider.alias
}

function cancelEdit(alias: string) {
  editingAliases.value.delete(alias)
  delete editDrafts[alias]
  delete requestPolicyDrafts[alias]
}

function openAddDrawer() {
  resetAddForm()
  addDrawerOpen.value = true
}

function openDeleteDrawer(provider: ProviderItem) {
  deletingProvider.value = provider
  deleteDrawerOpen.value = true
}

function resetAddForm() {
  addForm.alias = ''
  addForm.apiKey = ''
  addForm.openai = 'https://api.openai.com/v1'
  addForm.anthropic = ''
  addForm.gemini = ''
  addForm.requestPolicy = '{}'
  advancedAddOpen.value = false
}

function cleanBaseUrls(input: Partial<Record<ApiType, string>>) {
  const result: Partial<Record<ApiType, string>> = {}
  for (const type of ['openai', 'anthropic', 'gemini'] as ApiType[]) {
    const value = input[type]?.trim()
    if (value) result[type] = value
  }
  return result
}

function validateBaseUrls(baseUrls: Partial<Record<ApiType, string>>) {
  if (Object.keys(baseUrls).length === 0) {
    return '至少填写一个 Base URL'
  }
  return ''
}

function parseRequestPolicy(raw: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(raw)
    if (parsed === null || Array.isArray(parsed) || typeof parsed !== 'object') {
      message.warning('请求策略必须是 JSON object')
      return null
    }
    return parsed as Record<string, unknown>
  } catch {
    message.warning('请求策略不是合法 JSON')
    return null
  }
}

async function loadProviders() {
  loading.value = true
  try {
    const { data } = await api.get('/providers')
    providers.value = data.providers as ProviderItem[]
  } catch (error: any) {
    message.error(error?.response?.data?.reason ?? '加载模型服务商失败')
  } finally {
    loading.value = false
  }
}

function filteredModels(provider: ProviderItem) {
  const query = (modelSearch[provider.alias] ?? '').trim().toLowerCase()
  if (!query) return provider.models ?? []
  return (provider.models ?? []).filter((model) => model.toLowerCase().includes(query))
}

function iconUrl(apiType: ApiType) {
  return {
    openai: openaiIcon,
    anthropic: anthropicIcon,
    gemini: geminiIcon,
  }[apiType]
}

async function refreshModels(alias: string) {
  if (refreshingModels.value.has(alias)) return
  refreshingModels.value.add(alias)
  try {
    const { data } = await api.post(`/providers/${encodeURIComponent(alias)}/models/refresh`)
    message.success(data.message)
    await loadProviders()
  } catch (error: any) {
    message.error(error?.response?.data?.reason ?? error?.response?.data?.message ?? '刷新可用模型失败')
  } finally {
    refreshingModels.value.delete(alias)
  }
}

async function createProvider() {
  const baseUrls = cleanBaseUrls({
    openai: addForm.openai,
    anthropic: addForm.anthropic,
    gemini: addForm.gemini,
  })
  const validation = validateBaseUrls(baseUrls)
  if (validation) {
    message.warning(validation)
    return
  }
  const requestPolicy = parseRequestPolicy(addForm.requestPolicy)
  if (requestPolicy === null) return

  try {
    const { data } = await api.post('/providers', {
      alias: addForm.alias.trim(),
      api_key: addForm.apiKey.trim(),
      base_urls: baseUrls,
      request_policy: requestPolicy,
    })
    message.success(data.message)
    addDrawerOpen.value = false
    await loadProviders()
  } catch (error: any) {
    message.error(error?.response?.data?.reason ?? error?.response?.data?.message ?? '添加模型服务商失败')
  }
}

async function saveProvider(alias: string) {
  const draft = editDrafts[alias]
  const baseUrls = cleanBaseUrls(draft ?? {})
  const validation = validateBaseUrls(baseUrls)
  if (validation) {
    message.warning(validation)
    return
  }
  const requestPolicy = parseRequestPolicy(requestPolicyDrafts[alias] ?? '{}')
  if (requestPolicy === null) return

  try {
    const { data } = await api.put(`/providers/${encodeURIComponent(alias)}`, {
      base_urls: baseUrls,
      request_policy: requestPolicy,
    })
    message.success(data.message)
    cancelEdit(alias)
    await loadProviders()
  } catch (error: any) {
    message.error(error?.response?.data?.reason ?? error?.response?.data?.message ?? '保存模型服务商失败')
  }
}

async function removeProvider() {
  if (!deletingProvider.value) return
  try {
    const { data } = await api.delete(`/providers/${encodeURIComponent(deletingProvider.value.alias)}`)
    message.success(data.message)
    deleteDrawerOpen.value = false
    deletingProvider.value = null
    await loadProviders()
  } catch (error: any) {
    message.error(error?.response?.data?.reason ?? error?.response?.data?.message ?? '删除模型服务商失败')
  }
}

onMounted(loadProviders)
</script>

<template>
  <div class="providers-page">
    <div class="page-header">
      <div>
        <h1 class="page-title">模型服务商</h1>
        <p class="page-subtitle">管理模型服务商的 Base URL 和 API Key</p>
      </div>
      <div class="page-actions glass-panel">
        <NSpace align="center" :size="12">
          <NText depth="3">{{ providerCountLabel }}</NText>
          <NButton type="primary" @click="openAddDrawer">
            <template #icon><Plus :size="16" /></template>
            添加
          </NButton>
        </NSpace>
      </div>
    </div>

    <div class="providers-grid">
      <NCard
        v-for="provider in providers"
        :key="provider.alias"
        class="provider-card glass-panel-heavy"
        :bordered="false"
      >
        <div class="provider-card__top">
          <div class="provider-title">
            <div
              class="provider-badge"
              :class="[
                `provider-badge--${provider.preferred_api_type}`,
                { 'provider-badge--openai-dark': provider.preferred_api_type === 'openai' && isDark },
              ]"
            >
              <img class="provider-badge__icon" :src="iconUrl(provider.preferred_api_type)" :alt="providerLabel(provider.preferred_api_type)" />
            </div>
            <div>
              <div class="provider-name-row">
                <h2 class="provider-name">{{ provider.alias }}</h2>
                <NTag
                  v-for="slot in provider.active_slots"
                  :key="`${provider.alias}-${slot.key}`"
                  :class="['slot-tag', `slot-tag--${slot.key}`]"
                  size="small"
                  round
                  :bordered="false"
                >
                  {{ slot.key }} · {{ slot.model }}
                </NTag>
              </div>
              <p class="provider-primary-url">
                <span class="provider-primary-label">{{ providerLabel(provider.preferred_api_type) }}</span>
                {{ provider.preferred_base_url || '未设置 Base URL' }}
              </p>
            </div>
          </div>

          <div class="provider-actions">
            <NButton quaternary circle @click="toggleExpanded(provider.alias)">
              <template #icon>
                <component :is="expandedAlias === provider.alias ? ChevronDown : ChevronRight" :size="18" />
              </template>
            </NButton>
            <NButton quaternary circle @click="beginEdit(provider)">
              <template #icon><Pencil :size="18" /></template>
            </NButton>
            <NButton quaternary circle @click="openDeleteDrawer(provider)">
              <template #icon><Trash2 :size="18" /></template>
            </NButton>
          </div>
        </div>

        <div class="provider-meta">
          <div class="provider-meta-item">
            <span class="provider-meta-label">API Key</span>
            <button class="provider-key-button" type="button" @click="toggleReveal(provider.alias)">
              <span>{{ provider.alias in revealedKeys ? revealedKeys[provider.alias] : provider.api_key_masked }}</span>
              <component :is="provider.alias in revealedKeys ? EyeOff : Eye" :size="16" />
            </button>
          </div>
          <div class="provider-meta-item">
            <span class="provider-meta-label">模型缓存</span>
            <button class="provider-models-toggle" type="button" @click="toggleExpanded(provider.alias)">
              <span>{{ provider.model_count }} 个</span>
              <component :is="expandedAlias === provider.alias ? ChevronDown : ChevronRight" :size="16" />
            </button>
          </div>
        </div>

        <div v-if="expandedAlias === provider.alias" class="provider-expanded">
          <div class="provider-expanded-header">
            <span>全部 Base URL</span>
            <div class="provider-expanded-tools">
              <NButton size="small" quaternary :loading="refreshingModels.has(provider.alias)" @click="refreshModels(provider.alias)">
                <template #icon><RefreshCw :size="15" /></template>
                刷新模型
              </NButton>
              <span v-if="isEditing(provider.alias)" class="editing-indicator">编辑中</span>
            </div>
          </div>

          <div
            v-for="apiType in apiTypes"
            :key="apiType"
            class="provider-url-row"
          >
            <div class="provider-url-type">
              <div
                class="provider-badge provider-badge--mini"
                :class="[
                  `provider-badge--${apiType}`,
                  { 'provider-badge--openai-dark': apiType === 'openai' && isDark },
                ]"
              >
                <img class="provider-badge__icon provider-badge__icon--mini" :src="iconUrl(apiType)" :alt="providerLabel(apiType)" />
              </div>
              <span>{{ providerLabel(apiType) }}</span>
            </div>
            <template v-if="isEditing(provider.alias)">
              <NInput
                v-model:value="editDrafts[provider.alias][apiType]"
                :placeholder="`${providerLabel(apiType)} Base URL`"
              />
            </template>
            <template v-else>
              <code class="provider-url-value">
                {{ provider.base_urls[apiType] || '未设置' }}
              </code>
            </template>
          </div>

          <div class="provider-policy-panel">
            <div class="provider-expanded-header">
              <span>请求策略</span>
            </div>
            <NInput
              v-if="isEditing(provider.alias)"
              v-model:value="requestPolicyDrafts[provider.alias]"
              type="textarea"
              :autosize="{ minRows: 5, maxRows: 16 }"
              placeholder="{}"
            />
            <pre v-else class="provider-policy-value">{{ JSON.stringify(provider.request_policy ?? {}, null, 2) }}</pre>
          </div>

          <div v-if="isEditing(provider.alias)" class="provider-edit-actions">
            <NButton @click="cancelEdit(provider.alias)">
              <template #icon><X :size="16" /></template>
              取消
            </NButton>
            <NButton type="primary" @click="saveProvider(provider.alias)">
              <template #icon><Save :size="16" /></template>
              保存
            </NButton>
          </div>

          <div class="provider-models-panel">
            <div class="provider-expanded-header">
              <span>可用模型</span>
            </div>
            <div class="provider-model-search">
              <Search :size="16" />
              <NInput
                v-model:value="modelSearch[provider.alias]"
                placeholder="搜索模型..."
                clearable
              />
            </div>
            <div v-if="filteredModels(provider).length > 0" class="provider-model-list">
              <code v-for="model in filteredModels(provider)" :key="model" class="provider-model-item">
                {{ model }}
              </code>
            </div>
            <div v-else class="provider-model-empty">
              暂无可用模型，或搜索无结果
            </div>
          </div>
        </div>
      </NCard>

      <div v-if="!loading && providers.length === 0" class="providers-empty glass-panel-heavy">
        暂无模型服务商
      </div>
    </div>

    <NDrawer v-model:show="addDrawerOpen" placement="right" :width="440">
      <NDrawerContent title="添加模型服务商" closable>
        <div class="drawer-form">
          <label class="field-label">
            <span>服务商名称</span>
            <NInput v-model:value="addForm.alias" placeholder="例如 openrouter" />
          </label>

          <label class="field-label">
            <span>API Key</span>
            <NInput v-model:value="addForm.apiKey" type="password" show-password-on="click" placeholder="sk-..." />
          </label>

          <label class="field-label">
            <span>OpenAI Base URL</span>
            <NInput v-model:value="addForm.openai" placeholder="https://api.openai.com/v1" />
          </label>

          <button class="advanced-toggle" type="button" @click="advancedAddOpen = !advancedAddOpen">
            <component :is="advancedAddOpen ? ChevronDown : ChevronRight" :size="16" />
            高级选项
          </button>

          <div v-if="advancedAddOpen" class="advanced-fields">
            <label class="field-label">
              <span>Anthropic Base URL</span>
              <NInput v-model:value="addForm.anthropic" placeholder="https://api.anthropic.com" />
            </label>

            <label class="field-label">
              <span>Gemini Base URL</span>
              <NInput v-model:value="addForm.gemini" placeholder="https://generativelanguage.googleapis.com" />
            </label>

            <label class="field-label">
              <span>请求策略（JSON）</span>
              <NInput
                v-model:value="addForm.requestPolicy"
                type="textarea"
                :autosize="{ minRows: 5, maxRows: 14 }"
                placeholder="{}"
              />
            </label>

            <p class="drawer-tip">
              填了 Anthropic 或 Gemini URL 后，OpenAI URL 可以留空。请求策略用于声明兼容接口允许的请求字段和响应历史保留规则。
            </p>
          </div>
        </div>

        <template #footer>
          <div class="drawer-footer">
            <NButton @click="addDrawerOpen = false">取消</NButton>
            <NButton type="primary" @click="createProvider">创建</NButton>
          </div>
        </template>
      </NDrawerContent>
    </NDrawer>

    <NDrawer v-model:show="deleteDrawerOpen" placement="right" :width="380">
      <NDrawerContent title="确认删除" closable>
        <div v-if="deletingProvider" class="delete-panel">
          <p class="delete-title">删除模型服务商 `{{ deletingProvider.alias }}`？</p>
          <p class="delete-text">
            这会移除该模型服务商的全部 Base URL 和 API Key。若它仍被某个 slot 使用，后端会拒绝删除。
          </p>
          <div class="delete-summary">
            <span>默认展示 URL</span>
            <code>{{ deletingProvider.preferred_base_url || '未设置' }}</code>
          </div>
        </div>

        <template #footer>
          <div class="drawer-footer">
            <NButton @click="deleteDrawerOpen = false">取消</NButton>
            <NButton type="error" @click="removeProvider">确认删除</NButton>
          </div>
        </template>
      </NDrawerContent>
    </NDrawer>
  </div>
</template>

<style scoped>
.providers-page {
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

.providers-grid {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.provider-card {
  padding: 18px;
}

.provider-card :deep(.n-card__content) {
  padding: 0;
}

.provider-card__top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.provider-title {
  display: flex;
  gap: 12px;
  min-width: 0;
}

.provider-badge {
  width: 42px;
  height: 42px;
  border-radius: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  flex-shrink: 0;
  color: #fff;
  overflow: hidden;
}

.provider-badge__icon {
  width: 22px;
  height: 22px;
  filter: brightness(0) invert(1);
}

.provider-badge__icon--mini {
  width: 14px;
  height: 14px;
}

.provider-badge--openai .provider-badge__icon {
  filter: brightness(0) saturate(100%);
}

.provider-badge--mini {
  width: 26px;
  height: 26px;
  border-radius: 9px;
  font-size: 0.66rem;
}

.provider-badge--openai {
  background: #ffffff;
}

.provider-badge--openai-dark {
  background: linear-gradient(135deg, #2a3131, #3a4444);
}

.provider-badge--openai-dark .provider-badge__icon {
  filter: brightness(0) invert(1);
}

.provider-badge--anthropic {
  background: linear-gradient(135deg, #8b5a2b, #d18d44);
}

.provider-badge--gemini {
  background: linear-gradient(135deg, #3d5afe, #00bcd4);
}

.provider-name-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.provider-name {
  font-size: 1.08rem;
  font-weight: 600;
}

.provider-primary-url {
  margin-top: 8px;
  line-height: 1.5;
  word-break: break-all;
  opacity: 0.8;
}

.provider-primary-label {
  display: inline-block;
  margin-right: 6px;
  font-weight: 600;
}

.provider-actions {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
}

.slot-tag {
  color: #fff;
}

.slot-tag:deep(.n-tag__content) {
  color: inherit;
}

.slot-tag--default,
.slot-tag--collector {
  background: linear-gradient(135deg, #513fe0, #7c6bf0);
}

.slot-tag--vision {
  background: linear-gradient(135deg, #20c7fd, #3d8bfd);
}

.slot-tag--trigger {
  background: linear-gradient(135deg, #f5a927, #d97706);
}

.slot-tag--embedding {
  background: linear-gradient(135deg, #10b981, #059669);
}

.provider-meta {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 12px;
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px solid var(--glass-border);
}

.provider-meta-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.provider-meta-label {
  font-size: 0.78rem;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  opacity: 0.58;
}

.provider-key-button {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
  font-family: var(--font-mono);
  text-align: left;
  word-break: break-all;
}

.provider-expanded {
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px dashed var(--glass-border);
}

.provider-expanded-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
  font-weight: 600;
}

.provider-expanded-tools {
  display: flex;
  align-items: center;
  gap: 8px;
}

.editing-indicator {
  font-size: 0.8rem;
  color: #6c63ff;
}

.provider-url-row {
  display: grid;
  grid-template-columns: 120px 1fr;
  gap: 10px;
  align-items: center;
  margin-top: 10px;
}

.provider-url-type {
  display: flex;
  align-items: center;
  gap: 8px;
}

.provider-url-value {
  display: block;
  padding: 10px 12px;
  border-radius: 10px;
  background: rgba(127, 127, 148, 0.08);
  font-family: var(--font-mono);
  font-size: 0.88rem;
  word-break: break-all;
}

.provider-models-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
}

.provider-edit-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 16px;
}

.provider-policy-panel {
  margin-top: 18px;
  padding-top: 16px;
  border-top: 1px dashed var(--glass-border);
}

.provider-policy-value {
  max-height: 300px;
  margin: 0;
  padding: 12px;
  overflow: auto;
  border-radius: 10px;
  background: rgba(127, 127, 148, 0.08);
  font-family: var(--font-mono);
  font-size: 0.84rem;
  white-space: pre-wrap;
  word-break: break-word;
}

.provider-models-panel {
  margin-top: 18px;
  padding-top: 16px;
  border-top: 1px dashed var(--glass-border);
}

.provider-model-search {
  display: grid;
  grid-template-columns: 16px 1fr;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.provider-model-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 240px;
  overflow: auto;
}

.provider-model-item {
  display: block;
  padding: 10px 12px;
  border-radius: 10px;
  background: rgba(127, 127, 148, 0.08);
  font-family: var(--font-mono);
  word-break: break-all;
}

.provider-model-empty {
  opacity: 0.6;
  padding: 12px 0 4px;
}

.providers-empty {
  grid-column: 1 / -1;
  padding: 48px;
  text-align: center;
  opacity: 0.65;
}

.drawer-form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.field-label {
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-weight: 500;
}

.advanced-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  width: fit-content;
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
  font-weight: 600;
}

.advanced-fields {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 14px;
  border-radius: 12px;
  background: rgba(127, 127, 148, 0.08);
}

.drawer-tip {
  font-size: 0.88rem;
  opacity: 0.7;
}

.drawer-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  width: 100%;
}

.delete-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.delete-title {
  font-size: 1rem;
  font-weight: 600;
}

.delete-text {
  line-height: 1.6;
  opacity: 0.76;
}

.delete-summary {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border-radius: 12px;
  background: rgba(127, 127, 148, 0.08);
}

.delete-summary code {
  font-family: var(--font-mono);
  word-break: break-all;
}

@media (max-width: 767px) {
  .page-header {
    flex-direction: column;
    align-items: stretch;
  }

  .providers-grid {
    display: flex;
  }

  .provider-url-row {
    grid-template-columns: 1fr;
  }
}
</style>
