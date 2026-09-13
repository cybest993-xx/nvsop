<script setup lang="ts">
/**
 * The roles panel: the listing, and creating, redefining and deleting roles.
 *
 * The form's checkboxes come from the backend's catalogue (`readPermissionCatalogue`, passed
 * down by the page), never from a list kept here — that is what makes "不能引入未注册权限" true
 * at the screen as well as at the seam: a permission the backend does not register cannot be
 * offered, and one it adds appears here with no change to this file.
 *
 * A refusal is shown as text where it happened, carrying the backend's `detail` — the sentence
 * that names the permission which would leave the system unadministrable, when the
 * last-administration guard refuses.
 */
import {
  ElButton,
  ElCheckbox,
  ElCheckboxGroup,
  ElDialog,
  ElForm,
  ElFormItem,
  ElInput,
  ElMessage,
} from 'element-plus'
import { computed, ref } from 'vue'

import {
  ControlPlaneError,
  createRole,
  deleteRole,
  editRole,
  type FieldError,
  type RoleView,
} from '@/api/controlPlane'
import { useSessionStore } from '@/session/store'

defineProps<{ roles: RoleView[]; catalogue: string[]; loading: boolean }>()
const emit = defineEmits<{ changed: [] }>()

const session = useSessionStore()

const mayEditRoles = computed(() => session.may('auth.role.edit'))
const mayDeleteRoles = computed(() => session.may('auth.role.delete'))

const failure = ref('')
const fieldErrors = ref<FieldError[]>([])

function fieldError(name: string): string {
  return fieldErrors.value.find((item) => item.field === name)?.message ?? ''
}

function resetFailure(): void {
  failure.value = ''
  fieldErrors.value = []
}

/** Run an operation, reporting a refusal as text instead of letting it reject unhandled. */
async function attempt(operation: () => Promise<unknown>): Promise<boolean> {
  resetFailure()
  try {
    await operation()
    return true
  } catch (error) {
    if (!(error instanceof ControlPlaneError)) {
      throw error
    }
    failure.value = error.detail ?? error.message
    fieldErrors.value = error.fieldErrors
    return false
  }
}

const roleDialog = ref(false)
const editingRole = ref<RoleView | null>(null)
const roleDraft = ref({ code: '', name: '', permissions: [] as string[] })

function openRoleDialog(role: RoleView | null): void {
  editingRole.value = role
  roleDraft.value = role
    ? { code: role.code, name: role.name, permissions: [...role.permissions] }
    : { code: '', name: '', permissions: [] }
  resetFailure()
  roleDialog.value = true
}

async function submitRole(): Promise<void> {
  const existing = editingRole.value
  const ok = await attempt(() =>
    existing
      ? editRole(existing.id, {
          name: roleDraft.value.name,
          permissions: roleDraft.value.permissions,
        })
      : createRole({ ...roleDraft.value }),
  )
  if (ok) {
    roleDialog.value = false
    emit('changed')
  }
}

async function removeRole(role: RoleView): Promise<void> {
  if (await attempt(() => deleteRole(role.id))) {
    ElMessage.success('角色已删除')
    emit('changed')
  }
}
</script>

<template>
  <div class="panel">
    <div class="panel__actions">
      <ElButton v-if="mayEditRoles" type="primary" @click="openRoleDialog(null)">
        新建角色
      </ElButton>
    </div>

    <p v-if="failure" class="panel__failure" role="alert">{{ failure }}</p>

    <p v-if="loading" class="panel__loading">正在加载…</p>
    <table v-else class="panel__table">
      <caption class="panel__caption">
        角色及其权限
      </caption>
      <thead>
        <tr>
          <th scope="col">编码</th>
          <th scope="col">名称</th>
          <th scope="col">权限</th>
          <th scope="col">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="role in roles" :key="role.id">
          <td>{{ role.code }}</td>
          <td>{{ role.name }}</td>
          <td>
            <span v-if="role.permissions.length === 0" class="panel__none">无</span>
            <span v-else>{{ role.permissions.join('、') }}</span>
          </td>
          <td class="panel__row-actions">
            <ElButton v-if="mayEditRoles" link type="primary" @click="openRoleDialog(role)">
              编辑
            </ElButton>
            <ElButton v-if="mayDeleteRoles" link type="danger" @click="removeRole(role)">
              删除
            </ElButton>
          </td>
        </tr>
      </tbody>
    </table>

    <ElDialog v-model="roleDialog" :title="editingRole ? '编辑角色' : '新建角色'" width="34rem">
      <ElForm label-width="5rem" @submit.prevent="submitRole">
        <ElFormItem label="编码">
          <!-- The code is the natural key and is fixed once created, like a login name. -->
          <ElInput
            v-model="roleDraft.code"
            name="code"
            :disabled="editingRole !== null"
            autocomplete="off"
          />
          <p v-if="fieldError('code')" class="panel__field-error" role="alert">
            {{ fieldError('code') }}
          </p>
        </ElFormItem>
        <ElFormItem label="名称">
          <ElInput v-model="roleDraft.name" name="name" autocomplete="off" />
          <p v-if="fieldError('name')" class="panel__field-error" role="alert">
            {{ fieldError('name') }}
          </p>
        </ElFormItem>
        <ElFormItem label="权限">
          <!-- Rendered from the backend's catalogue, never from a list kept here. -->
          <ElCheckboxGroup v-model="roleDraft.permissions">
            <ElCheckbox v-for="permission in catalogue" :key="permission" :value="permission">
              {{ permission }}
            </ElCheckbox>
          </ElCheckboxGroup>
        </ElFormItem>
      </ElForm>
      <template #footer>
        <ElButton @click="roleDialog = false">取消</ElButton>
        <ElButton type="primary" @click="submitRole">保存</ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.panel__actions {
  margin: 0 0 1rem;
}

.panel__failure {
  margin: 0 0 1rem;
  padding: 0.6rem 0.9rem;
  border-left: 3px solid var(--el-color-danger);
  background: var(--el-color-danger-light-9);
  color: var(--el-color-danger);
}

.panel__field-error {
  margin: 0.25rem 0 0;
  width: 100%;
  color: var(--el-color-danger);
}

.panel__table {
  width: 100%;
  border-collapse: collapse;
}

.panel__caption {
  margin-bottom: 0.5rem;
  color: var(--el-text-color-secondary);
  text-align: left;
}

.panel__table th,
.panel__table td {
  padding: 0.55rem 0.75rem;
  border-bottom: 1px solid var(--el-border-color-lighter);
  text-align: left;
  vertical-align: middle;
}

.panel__row-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.panel__none,
.panel__loading {
  color: var(--el-text-color-secondary);
}

.panel :focus-visible {
  outline: 2px solid var(--el-color-primary);
  outline-offset: 2px;
}
</style>
