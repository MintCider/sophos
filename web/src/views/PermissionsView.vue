<script setup lang="ts">
import {
  NButton,
  NDrawer,
  NDrawerContent,
  NInput,
  NInputNumber,
  NPopconfirm,
  NSelect,
  NSwitch,
  NTabPane,
  NTabs,
  NTag,
  NText,
  useMessage,
} from 'naive-ui'
import {
  ChevronDown,
  ChevronRight,
  Plus,
  Trash2,
} from 'lucide-vue-next'
import { computed, onMounted, reactive, ref } from 'vue'
import api from '@/api'

// ── 类型 ──

interface ScopeItem {
  scope_type: string
  scope_id: number
  enabled: boolean
  updated_at: string | null
  tools: string[] | null
}

interface GrantItem {
  user_id: number
  scope_type: string
  scope_id: number
  permission: string
  granted_by: number
  created_at: string | null
}

interface PolicyItem {
  user_id: number
  scope_type: string
  scope_id: number
  suppress_llm_trigger: boolean
  rate_multiplier: number
  suppress_refresh: boolean
  note: string | null
}

// ── 常量 ──

const ALL_PERMS = [
  'bot', 'bot.group', 'bot.private',
  'cmd.tools', 'cmd.config', 'cmd.llm',
  'cmd.trigger', 'cmd.memory', 'cmd.prompt',
  'delegate',
].map(p => ({ label: p, value: p }))

const SCOPE_TYPES = [
  { label: '群聊', value: 'group' },
  { label: '私聊', value: 'private' },
  { label: '全局', value: 'global' },
]

const SESSION_SCOPE_TYPES = SCOPE_TYPES.filter(option => option.value !== 'global')

const message = useMessage()

// ── 数据 ──

const scopes = ref<ScopeItem[]>([])
const grants = ref<GrantItem[]>([])
const policies = ref<PolicyItem[]>([])
const availableTools = ref<string[]>([])

const expandedScope = ref('')

// ── 抽屉控制 ──

const addScopeOpen = ref(false)
const addGrantOpen = ref(false)
const addPolicyOpen = ref(false)
const deleteScopeOpen = ref(false)
const deleteScopeTarget = ref<ScopeItem | null>(null)
const deletePolicyOpen = ref(false)
const deletePolicyTarget = ref<PolicyItem | null>(null)

// ── 表单 ──

const scopeForm = reactive({ scope_type: 'group', scope_id: '', enabled: true })
const grantForm = reactive({ user_id: '', scope_type: 'global', scope_id: '0', permission: '' })
const policyForm = reactive({
  user_id: '', scope_type: 'global', scope_id: '0',
  suppress_llm_trigger: true, rate_multiplier: 0, suppress_refresh: true, note: '',
})

const toolInput = reactive<Record<string, string>>({})

// ── 辅助 ──

function scopeKey(s: { scope_type: string; scope_id: number }) {
  return `${s.scope_type}:${s.scope_id}`
}

function scopeLabel(t: string) {
  return ({ group: '群聊', private: '私聊', global: '全局' } as Record<string, string>)[t] ?? t
}

function scopeTagType(t: string) {
  return ({ group: 'info', private: 'success', global: 'warning' } as Record<string, string>)[t] as
    'info' | 'success' | 'warning' | undefined
}

function errorText(error: any, fallback: string) {
  return error?.response?.data?.error
    ?? error?.response?.data?.reason
    ?? error?.response?.data?.message
    ?? fallback
}

const availableToolOptions = computed(() => (
  availableTools.value.map(name => ({ label: name, value: name }))
))

function toolOptionsFor(scope: ScopeItem) {
  const selected = new Set(scope.tools ?? [])
  return availableToolOptions.value.filter(option => !selected.has(option.value))
}

function toggleExpanded(scope: ScopeItem) {
  const key = scopeKey(scope)
  expandedScope.value = expandedScope.value === key ? '' : key
}

function resetScopeForm() {
  scopeForm.scope_type = 'group'
  scopeForm.scope_id = ''
  scopeForm.enabled = true
}

function resetGrantForm() {
  grantForm.user_id = ''
  grantForm.scope_type = 'global'
  grantForm.scope_id = '0'
  grantForm.permission = ''
}

function resetPolicyForm() {
  policyForm.user_id = ''
  policyForm.scope_type = 'global'
  policyForm.scope_id = '0'
  policyForm.suppress_llm_trigger = true
  policyForm.rate_multiplier = 0
  policyForm.suppress_refresh = true
  policyForm.note = ''
}

// ── API: 会话权限 ──

async function loadScopes() {
  try {
    const { data } = await api.get('/permissions/scopes')
    scopes.value = data.scopes
  } catch (e: any) {
    message.error(errorText(e, '加载会话权限失败'))
  }
}

async function loadAvailableTools() {
  try {
    const { data } = await api.get('/tools')
    availableTools.value = (data.groups ?? [])
      .flatMap((group: { tools?: Array<{ name: string }> }) => group.tools ?? [])
      .map((tool: { name: string }) => tool.name)
      .sort((a: string, b: string) => a.localeCompare(b))
  } catch (e: any) {
    message.error(errorText(e, '加载工具列表失败'))
  }
}

async function createScope() {
  if (!scopeForm.scope_id || Number(scopeForm.scope_id) <= 0) {
    message.warning('请输入有效的会话 ID')
    return
  }
  try {
    const { data } = await api.post('/permissions/scopes', {
      scope_type: scopeForm.scope_type,
      scope_id: scopeForm.scope_type === 'global' ? 0 : Number(scopeForm.scope_id),
      enabled: scopeForm.enabled,
    })
    message.success(data.message)
    addScopeOpen.value = false
    resetScopeForm()
    await loadScopes()
  } catch (e: any) {
    message.error(errorText(e, '创建会话失败'))
  }
}

async function toggleScope(scope: ScopeItem, enabled: boolean) {
  try {
    await api.put(`/permissions/scopes/${scope.scope_type}/${scope.scope_id}`, { enabled })
    scope.enabled = enabled
  } catch (e: any) {
    message.error(errorText(e, '切换会话状态失败'))
    await loadScopes()
  }
}

async function deleteScope() {
  if (!deleteScopeTarget.value) return
  const s = deleteScopeTarget.value
  try {
    const { data } = await api.delete(`/permissions/scopes/${s.scope_type}/${s.scope_id}`)
    message.success(data.message)
    deleteScopeOpen.value = false
    deleteScopeTarget.value = null
    await loadScopes()
  } catch (e: any) {
    message.error(errorText(e, '删除会话失败'))
  }
}

async function addTool(scope: ScopeItem) {
  const key = scopeKey(scope)
  const name = (toolInput[key] ?? '').trim()
  if (!name) return
  const current = scope.tools ?? []
  if (current.includes(name)) {
    message.warning('工具已存在')
    return
  }
  try {
    await api.put(`/permissions/scopes/${scope.scope_type}/${scope.scope_id}/tools`, {
      tools: [...current, name],
    })
    if (scope.tools === null) {
      message.success(`已启用白名单，当前仅允许 ${name}`)
    }
    toolInput[key] = ''
    await loadScopes()
  } catch (e: any) {
    message.error(errorText(e, '添加工具失败'))
  }
}

async function removeTool(scope: ScopeItem, tool: string) {
  const current = scope.tools ?? []
  if (current.length <= 1) {
    message.warning('白名单至少保留一个工具；如需解除限制，请点击“重置为全部可用”')
    return
  }
  const remaining = current.filter(t => t !== tool)
  try {
    await api.put(`/permissions/scopes/${scope.scope_type}/${scope.scope_id}/tools`, {
      tools: remaining,
    })
    await loadScopes()
  } catch (e: any) {
    message.error(errorText(e, '移除工具失败'))
  }
}

async function resetTools(scope: ScopeItem) {
  try {
    await api.delete(`/permissions/scopes/${scope.scope_type}/${scope.scope_id}/tools`)
    message.success('已重置工具白名单')
    await loadScopes()
  } catch (e: any) {
    message.error(errorText(e, '重置工具白名单失败'))
  }
}

// ── API: 用户授权 ──

async function loadGrants() {
  try {
    const { data } = await api.get('/permissions/grants')
    grants.value = data.grants
  } catch (e: any) {
    message.error(errorText(e, '加载用户授权失败'))
  }
}

async function createGrant() {
  if (!grantForm.user_id || Number(grantForm.user_id) <= 0 || !grantForm.permission) {
    message.warning('请填写有效的用户 QQ 和权限')
    return
  }
  if (grantForm.scope_type !== 'global' && Number(grantForm.scope_id) <= 0) {
    message.warning('请输入有效的作用域 ID')
    return
  }
  try {
    const { data } = await api.post('/permissions/grants', {
      user_id: Number(grantForm.user_id),
      scope_type: grantForm.scope_type,
      scope_id: grantForm.scope_type === 'global' ? 0 : Number(grantForm.scope_id),
      permission: grantForm.permission,
    })
    message.success(data.message)
    addGrantOpen.value = false
    resetGrantForm()
    await loadGrants()
  } catch (e: any) {
    message.error(errorText(e, '授予权限失败'))
  }
}

async function revokeGrant(g: GrantItem) {
  try {
    const { data } = await api.delete('/permissions/grants', {
      data: {
        user_id: g.user_id,
        scope_type: g.scope_type,
        scope_id: g.scope_id,
        permission: g.permission,
      },
    })
    message.success(data.message)
    await loadGrants()
  } catch (e: any) {
    message.error(errorText(e, '撤销权限失败'))
  }
}

// ── API: 用户策略 ──

async function loadPolicies() {
  try {
    const { data } = await api.get('/permissions/policies')
    policies.value = data.policies
  } catch (e: any) {
    message.error(errorText(e, '加载用户策略失败'))
  }
}

async function createPolicy() {
  if (!policyForm.user_id || Number(policyForm.user_id) <= 0) {
    message.warning('请输入有效的用户 QQ')
    return
  }
  if (policyForm.scope_type !== 'global' && Number(policyForm.scope_id) <= 0) {
    message.warning('请输入有效的作用域 ID')
    return
  }
  try {
    const { data } = await api.post('/permissions/policies', {
      user_id: Number(policyForm.user_id),
      scope_type: policyForm.scope_type,
      scope_id: policyForm.scope_type === 'global' ? 0 : Number(policyForm.scope_id),
      suppress_llm_trigger: policyForm.suppress_llm_trigger,
      rate_multiplier: policyForm.rate_multiplier,
      suppress_refresh: policyForm.suppress_refresh,
      note: policyForm.note || null,
    })
    message.success(data.message)
    addPolicyOpen.value = false
    resetPolicyForm()
    await loadPolicies()
  } catch (e: any) {
    message.error(errorText(e, '创建策略失败'))
  }
}

async function updatePolicy(p: PolicyItem) {
  try {
    await api.post('/permissions/policies', {
      user_id: p.user_id,
      scope_type: p.scope_type,
      scope_id: p.scope_id,
      suppress_llm_trigger: p.suppress_llm_trigger,
      rate_multiplier: p.rate_multiplier,
      suppress_refresh: p.suppress_refresh,
      note: p.note,
    })
  } catch (e: any) {
    message.error(errorText(e, '更新策略失败'))
    await loadPolicies()
  }
}

async function deletePolicy() {
  if (!deletePolicyTarget.value) return
  const p = deletePolicyTarget.value
  try {
    const { data } = await api.delete(
      `/permissions/policies/${p.user_id}/${p.scope_type}/${p.scope_id}`,
    )
    message.success(data.message)
    deletePolicyOpen.value = false
    deletePolicyTarget.value = null
    await loadPolicies()
  } catch (e: any) {
    message.error(errorText(e, '删除策略失败'))
  }
}

// ── 初始化 ──

onMounted(() => {
  loadScopes()
  loadGrants()
  loadPolicies()
  loadAvailableTools()
})
</script>

<template>
  <div class="permissions-page">
    <div class="page-header">
      <div>
        <h1 class="page-title">权限管理</h1>
        <p class="page-subtitle">管理会话权限、用户授权与触发策略</p>
      </div>
    </div>

    <NTabs type="line" animated class="perm-tabs">
      <!-- ── Tab 1: 会话权限 ── -->
      <NTabPane name="scopes" tab="会话权限">
        <div class="tab-header">
          <NText depth="3">{{ scopes.length }} 个会话</NText>
          <NButton type="primary" size="small" @click="resetScopeForm(); addScopeOpen = true">
            <template #icon><Plus :size="16" /></template>
            添加
          </NButton>
        </div>

        <div class="card-list">
          <div
            v-for="scope in scopes"
            :key="scopeKey(scope)"
            class="perm-card glass-panel-heavy"
          >
            <div class="perm-card__top">
              <div class="perm-card__info">
                <NTag :type="scopeTagType(scope.scope_type)" size="small" round :bordered="false">
                  {{ scopeLabel(scope.scope_type) }}
                </NTag>
                <span class="scope-id">{{ scope.scope_id }}</span>
              </div>
              <div class="perm-card__actions">
                <NSwitch
                  :value="scope.enabled"
                  size="small"
                  @update:value="(v: boolean) => toggleScope(scope, v)"
                />
                <NButton quaternary circle size="small" @click="toggleExpanded(scope)">
                  <template #icon>
                    <component
                      :is="expandedScope === scopeKey(scope) ? ChevronDown : ChevronRight"
                      :size="18"
                    />
                  </template>
                </NButton>
                <NButton
                  quaternary circle size="small"
                  @click="deleteScopeTarget = scope; deleteScopeOpen = true"
                >
                  <template #icon><Trash2 :size="16" /></template>
                </NButton>
              </div>
            </div>

            <!-- 展开：工具白名单 -->
            <div v-if="expandedScope === scopeKey(scope)" class="perm-card__expanded">
              <div class="tool-section-header">
                <span class="tool-section-title">工具白名单</span>
                <NPopconfirm
                  v-if="scope.tools && scope.tools.length > 0"
                  @positive-click="resetTools(scope)"
                >
                  <template #trigger>
                    <NButton size="tiny" quaternary>重置为全部可用</NButton>
                  </template>
                  重置后将解除工具限制，确认继续？
                </NPopconfirm>
              </div>

              <div v-if="!scope.tools" class="tool-unrestricted">
                全部可用（未限制）
              </div>
              <div v-else class="tool-tags">
                <NTag
                  v-for="tool in scope.tools"
                  :key="tool"
                  closable size="small" round
                  @close="removeTool(scope, tool)"
                >
                  {{ tool }}
                </NTag>
              </div>

              <div class="tool-add">
                <NSelect
                  :value="toolInput[scopeKey(scope)] ?? ''"
                  :options="toolOptionsFor(scope)"
                  size="small"
                  filterable
                  clearable
                  placeholder="选择工具"
                  @update:value="(v: string) => toolInput[scopeKey(scope)] = v"
                />
                <NButton size="small" @click="addTool(scope)">
                  <template #icon><Plus :size="14" /></template>
                </NButton>
              </div>
            </div>
          </div>

          <div v-if="scopes.length === 0" class="empty-state glass-panel-heavy">
            暂无会话权限
          </div>
        </div>
      </NTabPane>

      <!-- ── Tab 2: 用户授权 ── -->
      <NTabPane name="grants" tab="用户授权">
        <div class="tab-header">
          <NText depth="3">{{ grants.length }} 条授权</NText>
          <NButton type="primary" size="small" @click="resetGrantForm(); addGrantOpen = true">
            <template #icon><Plus :size="16" /></template>
            添加
          </NButton>
        </div>

        <div class="card-list">
          <div
            v-for="(g, i) in grants"
            :key="i"
            class="grant-row glass-panel-heavy"
          >
            <div class="grant-info">
              <span class="grant-user">{{ g.user_id }}</span>
              <NTag
                size="small" round :bordered="false"
                :type="scopeTagType(g.scope_type)"
              >
                {{ scopeLabel(g.scope_type) }}{{ g.scope_type !== 'global' ? `:${g.scope_id}` : '' }}
              </NTag>
              <NTag size="small" round>{{ g.permission }}</NTag>
            </div>
            <NPopconfirm @positive-click="revokeGrant(g)">
              <template #trigger>
                <NButton quaternary circle size="small">
                  <template #icon><Trash2 :size="16" /></template>
                </NButton>
              </template>
              确认撤销用户 {{ g.user_id }} 的 {{ g.permission }} 权限？
            </NPopconfirm>
          </div>

          <div v-if="grants.length === 0" class="empty-state glass-panel-heavy">
            暂无用户授权
          </div>
        </div>
      </NTabPane>

      <!-- ── Tab 3: 用户策略 ── -->
      <NTabPane name="policies" tab="用户策略">
        <div class="tab-header">
          <NText depth="3">{{ policies.length }} 条策略</NText>
          <NButton type="primary" size="small" @click="resetPolicyForm(); addPolicyOpen = true">
            <template #icon><Plus :size="16" /></template>
            添加
          </NButton>
        </div>

        <div class="card-list">
          <div
            v-for="p in policies"
            :key="`${p.user_id}:${p.scope_type}:${p.scope_id}`"
            class="perm-card glass-panel-heavy"
          >
            <div class="perm-card__top">
              <div class="perm-card__info">
                <span class="policy-user">{{ p.user_id }}</span>
                <NTag
                  size="small" round :bordered="false"
                  :type="scopeTagType(p.scope_type)"
                >
                  {{ scopeLabel(p.scope_type) }}{{ p.scope_type !== 'global' ? `:${p.scope_id}` : '' }}
                </NTag>
              </div>
              <NButton
                quaternary circle size="small"
                @click="deletePolicyTarget = p; deletePolicyOpen = true"
              >
                <template #icon><Trash2 :size="16" /></template>
              </NButton>
            </div>

            <div class="policy-fields">
              <div class="policy-field">
                <span class="policy-field-label">抑制 LLM 触发</span>
                <NSwitch
                  :value="p.suppress_llm_trigger" size="small"
                  @update:value="(v: boolean) => { p.suppress_llm_trigger = v; updatePolicy(p) }"
                />
              </div>
              <div class="policy-field">
                <span class="policy-field-label">概率倍率</span>
                <NInputNumber
                  :value="p.rate_multiplier" size="small"
                  :min="0" :max="10" :step="0.1"
                  style="width: 110px"
                  @update:value="(v: number | null) => { p.rate_multiplier = v ?? 0; updatePolicy(p) }"
                />
              </div>
              <div class="policy-field">
                <span class="policy-field-label">抑制刷新注入</span>
                <NSwitch
                  :value="p.suppress_refresh" size="small"
                  @update:value="(v: boolean) => { p.suppress_refresh = v; updatePolicy(p) }"
                />
              </div>
              <div class="policy-field policy-field--wide">
                <span class="policy-field-label">备注</span>
                <NInput
                  :value="p.note ?? ''" size="small"
                  placeholder="无"
                  @update:value="(v: string) => { p.note = v || null }"
                  @blur="updatePolicy(p)"
                  @keyup.enter="($event.target as HTMLInputElement)?.blur()"
                />
              </div>
            </div>
          </div>

          <div v-if="policies.length === 0" class="empty-state glass-panel-heavy">
            暂无用户策略
          </div>
        </div>
      </NTabPane>
    </NTabs>

    <!-- ── 添加会话抽屉 ── -->
    <NDrawer v-model:show="addScopeOpen" placement="right" :width="380">
      <NDrawerContent title="添加会话" closable>
        <div class="drawer-form">
          <label class="field-label">
            <span>会话类型</span>
            <NSelect v-model:value="scopeForm.scope_type" :options="SESSION_SCOPE_TYPES" />
          </label>
          <label class="field-label">
            <span>{{ scopeForm.scope_type === 'group' ? '群号' : '用户 QQ' }}</span>
            <NInput v-model:value="scopeForm.scope_id" placeholder="输入 ID" />
          </label>
          <label class="field-label">
            <span>启用</span>
            <NSwitch v-model:value="scopeForm.enabled" />
          </label>
        </div>
        <template #footer>
          <div class="drawer-footer">
            <NButton @click="addScopeOpen = false">取消</NButton>
            <NButton type="primary" @click="createScope">创建</NButton>
          </div>
        </template>
      </NDrawerContent>
    </NDrawer>

    <!-- ── 添加授权抽屉 ── -->
    <NDrawer v-model:show="addGrantOpen" placement="right" :width="380">
      <NDrawerContent title="添加授权" closable>
        <div class="drawer-form">
          <label class="field-label">
            <span>用户 QQ</span>
            <NInput v-model:value="grantForm.user_id" placeholder="QQ 号" />
          </label>
          <label class="field-label">
            <span>作用域</span>
            <NSelect v-model:value="grantForm.scope_type" :options="SCOPE_TYPES" />
          </label>
          <label v-if="grantForm.scope_type !== 'global'" class="field-label">
            <span>{{ grantForm.scope_type === 'group' ? '群号' : '用户 QQ' }}</span>
            <NInput v-model:value="grantForm.scope_id" placeholder="输入 ID" />
          </label>
          <label class="field-label">
            <span>权限</span>
            <NSelect v-model:value="grantForm.permission" :options="ALL_PERMS" />
          </label>
        </div>
        <template #footer>
          <div class="drawer-footer">
            <NButton @click="addGrantOpen = false">取消</NButton>
            <NButton type="primary" @click="createGrant">授予</NButton>
          </div>
        </template>
      </NDrawerContent>
    </NDrawer>

    <!-- ── 添加策略抽屉 ── -->
    <NDrawer v-model:show="addPolicyOpen" placement="right" :width="420">
      <NDrawerContent title="添加触发策略" closable>
        <div class="drawer-form">
          <label class="field-label">
            <span>用户 QQ</span>
            <NInput v-model:value="policyForm.user_id" placeholder="QQ 号" />
          </label>
          <label class="field-label">
            <span>作用域</span>
            <NSelect v-model:value="policyForm.scope_type" :options="SCOPE_TYPES" />
          </label>
          <label v-if="policyForm.scope_type !== 'global'" class="field-label">
            <span>{{ policyForm.scope_type === 'group' ? '群号' : '用户 QQ' }}</span>
            <NInput v-model:value="policyForm.scope_id" placeholder="输入 ID" />
          </label>
          <label class="field-label">
            <span>抑制 LLM 触发</span>
            <NSwitch v-model:value="policyForm.suppress_llm_trigger" />
          </label>
          <label class="field-label">
            <span>概率倍率</span>
            <NInputNumber v-model:value="policyForm.rate_multiplier" :min="0" :max="10" :step="0.1" />
          </label>
          <label class="field-label">
            <span>抑制刷新注入</span>
            <NSwitch v-model:value="policyForm.suppress_refresh" />
          </label>
          <label class="field-label">
            <span>备注</span>
            <NInput v-model:value="policyForm.note" placeholder="可选" />
          </label>
        </div>
        <template #footer>
          <div class="drawer-footer">
            <NButton @click="addPolicyOpen = false">取消</NButton>
            <NButton type="primary" @click="createPolicy">创建</NButton>
          </div>
        </template>
      </NDrawerContent>
    </NDrawer>

    <!-- ── 删除会话确认 ── -->
    <NDrawer v-model:show="deleteScopeOpen" placement="right" :width="380">
      <NDrawerContent title="确认删除" closable>
        <div v-if="deleteScopeTarget" class="delete-panel">
          <p class="delete-title">
            删除会话 {{ scopeLabel(deleteScopeTarget.scope_type) }}:{{ deleteScopeTarget.scope_id }}？
          </p>
          <p class="delete-text">这会同时移除该会话的工具白名单设置。</p>
        </div>
        <template #footer>
          <div class="drawer-footer">
            <NButton @click="deleteScopeOpen = false">取消</NButton>
            <NButton type="error" @click="deleteScope">确认删除</NButton>
          </div>
        </template>
      </NDrawerContent>
    </NDrawer>

    <!-- ── 删除策略确认 ── -->
    <NDrawer v-model:show="deletePolicyOpen" placement="right" :width="380">
      <NDrawerContent title="确认删除" closable>
        <div v-if="deletePolicyTarget" class="delete-panel">
          <p class="delete-title">删除用户 {{ deletePolicyTarget.user_id }} 的触发策略？</p>
          <p class="delete-text">
            作用域：{{ scopeLabel(deletePolicyTarget.scope_type) }}{{ deletePolicyTarget.scope_type !== 'global' ? `:${deletePolicyTarget.scope_id}` : '' }}
          </p>
        </div>
        <template #footer>
          <div class="drawer-footer">
            <NButton @click="deletePolicyOpen = false">取消</NButton>
            <NButton type="error" @click="deletePolicy">确认删除</NButton>
          </div>
        </template>
      </NDrawerContent>
    </NDrawer>
  </div>
</template>

<style scoped>
.permissions-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* ── 页头 ── */

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.page-title {
  font-size: 1.5rem;
  font-weight: 600;
}

.page-subtitle {
  margin-top: 6px;
  opacity: 0.68;
}

/* ── Tabs ── */

.perm-tabs :deep(.n-tabs-nav) {
  padding: 0 4px;
}

.perm-tabs :deep(.n-tab-pane) {
  padding: 14px 0 0;
}

/* ── Tab 内顶栏 ── */

.tab-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}

/* ── 卡片列表 ── */

.card-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* ── 通用卡片 ── */

.perm-card {
  padding: 16px;
}

.perm-card__top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.perm-card__info {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.perm-card__actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

.scope-id {
  font-family: var(--font-mono);
  font-size: 0.95rem;
}

/* ── 展开区域：工具白名单 ── */

.perm-card__expanded {
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px dashed var(--glass-border);
}

.tool-section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.tool-section-title {
  font-weight: 600;
  font-size: 0.88rem;
}

.tool-unrestricted {
  opacity: 0.6;
  font-size: 0.88rem;
  margin-bottom: 10px;
}

.tool-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
}

.tool-add {
  display: flex;
  gap: 8px;
  align-items: center;
}

/* ── 用户授权行 ── */

.grant-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
}

.grant-info {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.grant-user {
  font-family: var(--font-mono);
  font-weight: 600;
  font-size: 0.95rem;
}

/* ── 用户策略字段 ── */

.policy-user {
  font-family: var(--font-mono);
  font-weight: 600;
  font-size: 0.95rem;
}

.policy-fields {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px solid var(--glass-border);
}

.policy-field {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.policy-field--wide {
  grid-column: 1 / -1;
}

.policy-field-label {
  font-size: 0.85rem;
  opacity: 0.7;
  white-space: nowrap;
}

/* ── 抽屉 ── */

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

.drawer-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  width: 100%;
}

/* ── 删除确认 ── */

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

/* ── 空状态 ── */

.empty-state {
  padding: 48px;
  text-align: center;
  opacity: 0.65;
}

/* ── 响应式 ── */

@media (max-width: 767px) {
  .page-header {
    flex-direction: column;
    align-items: stretch;
  }

  .policy-fields {
    grid-template-columns: 1fr;
  }

  .grant-info {
    flex-wrap: wrap;
  }
}
</style>
