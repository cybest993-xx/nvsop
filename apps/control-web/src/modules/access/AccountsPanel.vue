<script setup lang="ts">
/**
 * The accounts panel: the listing, and every operation that changes an account — create, rename,
 * reset password, assign roles, 停用/恢复, delete.
 *
 * Its one rule for what it offers: only what the caller's permissions include, and no ceremony
 * about it. §5.4 rules out showing a module the caller may not use, so a button the backend
 * would refuse does not exist here. The backend checks anyway — this is presentation, not
 * enforcement.
 *
 * A refusal is shown where it happened: the banner carries the backend's `detail` (the specific
 * reason — which login name is taken), and the create form repeats a field error beside the
 * field it names. The dialogs are this panel's private surface; the page orchestrates refresh.
 */
import {
  ElButton,
  ElDialog,
  ElForm,
  ElFormItem,
  ElInput,
  ElMessage,
  ElOption,
  ElSelect,
  ElTag,
} from 'element-plus'
import { computed, ref } from 'vue'

import {
  ControlPlaneError,
  createUser,
  deleteUser,
  editUser,
  resetUserPassword,
  setUserRoles,
  setUserStatus,
  type FieldError,
  type RoleView,
  type UserView,
} from '@/api/controlPlane'
import { useSessionStore } from '@/session/store'

// Kept in step with the backend's `MINIMUM_PASSWORD_LENGTH`. Checked here so the form can refuse
// before a round trip; the backend refuses too, and its answer is the authority.
const MINIMUM_PASSWORD_LENGTH = 12

const props = defineProps<{ users: UserView[]; roles: RoleView[]; loading: boolean }>()
const emit = defineEmits<{ changed: [] }>()

const session = useSessionStore()

const mayEditUsers = computed(() => session.may('auth.user.edit'))
const mayDeleteUsers = computed(() => session.may('auth.user.delete'))
// The assignment operation is authorized by USER_EDIT, but its form needs the role catalogue.
// Do not offer a button that opens an empty selector to a caller who lacks ROLE_VIEW; the API
// remains the authority for callers that already know role IDs.
const mayAssignRoles = computed(() => mayEditUsers.value && session.may('auth.role.view'))

const roleNames = computed(() => new Map(props.roles.map((role) => [role.id, role.name])))

/** The last refusal on this panel, as the banner text and the per-field messages. `detail` is
 * the backend's specific reason and wins over the generic title; field errors are repeated
 * beside their fields, because a form with four inputs deserves the answer at the input. */
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

// ——— create ———

const userDialog = ref(false)
const userDraft = ref({ login_name: '', display_name: '', password: '' })

function openUserDialog(): void {
  userDraft.value = { login_name: '', display_name: '', password: '' }
  resetFailure()
  userDialog.value = true
}

async function submitUser(): Promise<void> {
  if (userDraft.value.password.length < MINIMUM_PASSWORD_LENGTH) {
    failure.value = `密码至少需要 ${MINIMUM_PASSWORD_LENGTH} 个字符`
    return
  }
  if (await attempt(() => createUser({ ...userDraft.value }))) {
    userDialog.value = false
    ElMessage.success('账户已创建')
    emit('changed')
  }
}

// ——— rename ———

const renameDialog = ref(false)
const renaming = ref<UserView | null>(null)
const newDisplayName = ref('')

function openRename(user: UserView): void {
  renaming.value = user
  newDisplayName.value = user.display_name
  resetFailure()
  renameDialog.value = true
}

async function submitRename(): Promise<void> {
  const target = renaming.value
  if (!target) {
    return
  }
  if (await attempt(() => editUser(target.id, { display_name: newDisplayName.value }))) {
    renameDialog.value = false
    emit('changed')
  }
}

// ——— reset password ———

const passwordDialog = ref(false)
const resetting = ref<UserView | null>(null)
const newPassword = ref('')

function openReset(user: UserView): void {
  resetting.value = user
  newPassword.value = ''
  resetFailure()
  passwordDialog.value = true
}

async function submitReset(): Promise<void> {
  const target = resetting.value
  if (!target) {
    return
  }
  if (newPassword.value.length < MINIMUM_PASSWORD_LENGTH) {
    failure.value = `密码至少需要 ${MINIMUM_PASSWORD_LENGTH} 个字符`
    return
  }
  if (await attempt(() => resetUserPassword(target.id, newPassword.value))) {
    passwordDialog.value = false
    ElMessage.success('密码已重置')
  }
}

// ——— status: 停用 and 恢复 ———

async function toggleStatus(user: UserView): Promise<void> {
  const next = user.status === 'active' ? 'deactivated' : 'active'
  let revoked = 0
  const ok = await attempt(async () => {
    const outcome = await setUserStatus(user.id, next)
    revoked = outcome.revoked_sessions
  })
  if (ok) {
    // Saying how many sessions were closed is the point: an administrator deactivating an account
    // needs to know the operator standing at a terminal has been signed out, not just that a field
    // changed.
    ElMessage.success(
      next === 'deactivated' ? `已停用，同时下线 ${revoked} 个会话` : '已恢复该账户',
    )
    emit('changed')
  }
}

// ——— roles ———

const rolesDialog = ref(false)
const assigning = ref<UserView | null>(null)
const assignedRoleIds = ref<string[]>([])

function openRoles(user: UserView): void {
  assigning.value = user
  assignedRoleIds.value = [...user.role_ids]
  resetFailure()
  rolesDialog.value = true
}

async function submitRoles(): Promise<void> {
  const target = assigning.value
  if (!target) {
    return
  }
  if (await attempt(() => setUserRoles(target.id, assignedRoleIds.value))) {
    rolesDialog.value = false
    emit('changed')
  }
}

async function removeUser(user: UserView): Promise<void> {
  if (await attempt(() => deleteUser(user.id))) {
    ElMessage.success('账户已删除')
    emit('changed')
  }
}
</script>

<template>
  <div class="panel">
    <div class="panel__actions">
      <ElButton v-if="mayEditUsers" type="primary" @click="openUserDialog">新建账户</ElButton>
    </div>

    <p v-if="failure" class="panel__failure" role="alert">{{ failure }}</p>

    <p v-if="loading" class="panel__loading">正在加载…</p>
    <table v-else class="panel__table">
      <caption class="panel__caption">
        全部账户，含已停用
      </caption>
      <thead>
        <tr>
          <th scope="col">登录名</th>
          <th scope="col">姓名</th>
          <th scope="col">状态</th>
          <th scope="col">角色</th>
          <th scope="col">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="user in users" :key="user.id">
          <td>{{ user.login_name }}</td>
          <td>{{ user.display_name }}</td>
          <td>
            <!-- The word as well as the colour: 停用 is legible without seeing the tag's hue. -->
            <ElTag :type="user.status === 'active' ? 'success' : 'info'" disable-transitions>
              {{ user.status === 'active' ? '在用' : '已停用' }}
            </ElTag>
          </td>
          <td>
            <span v-if="user.role_ids.length === 0" class="panel__none">无</span>
            <span v-else>
              {{ user.role_ids.map((id) => roleNames.get(id) ?? id).join('、') }}
            </span>
          </td>
          <td class="panel__row-actions">
            <template v-if="mayEditUsers">
              <ElButton link type="primary" @click="openRename(user)">改名</ElButton>
              <ElButton link type="primary" @click="openReset(user)">重置密码</ElButton>
              <ElButton v-if="mayAssignRoles" link type="primary" @click="openRoles(user)">
                分配角色
              </ElButton>
              <ElButton link type="primary" @click="toggleStatus(user)">
                {{ user.status === 'active' ? '停用' : '恢复' }}
              </ElButton>
            </template>
            <ElButton v-if="mayDeleteUsers" link type="danger" @click="removeUser(user)">
              删除
            </ElButton>
          </td>
        </tr>
      </tbody>
    </table>

    <ElDialog v-model="userDialog" title="新建账户" width="30rem">
      <ElForm label-width="6rem" @submit.prevent="submitUser">
        <ElFormItem label="登录名">
          <ElInput v-model="userDraft.login_name" name="login_name" autocomplete="off" />
          <p v-if="fieldError('login_name')" class="panel__field-error" role="alert">
            {{ fieldError('login_name') }}
          </p>
        </ElFormItem>
        <ElFormItem label="姓名">
          <ElInput v-model="userDraft.display_name" name="display_name" autocomplete="off" />
          <p v-if="fieldError('display_name')" class="panel__field-error" role="alert">
            {{ fieldError('display_name') }}
          </p>
        </ElFormItem>
        <ElFormItem label="初始密码">
          <ElInput
            v-model="userDraft.password"
            name="password"
            type="password"
            autocomplete="new-password"
            show-password
          />
          <p v-if="fieldError('password')" class="panel__field-error" role="alert">
            {{ fieldError('password') }}
          </p>
        </ElFormItem>
      </ElForm>
      <template #footer>
        <ElButton @click="userDialog = false">取消</ElButton>
        <ElButton type="primary" @click="submitUser">创建</ElButton>
      </template>
    </ElDialog>

    <ElDialog v-model="renameDialog" title="修改姓名" width="26rem">
      <ElForm label-width="5rem" @submit.prevent="submitRename">
        <ElFormItem label="姓名">
          <ElInput v-model="newDisplayName" name="display_name" autocomplete="off" />
        </ElFormItem>
      </ElForm>
      <template #footer>
        <ElButton @click="renameDialog = false">取消</ElButton>
        <ElButton type="primary" @click="submitRename">保存</ElButton>
      </template>
    </ElDialog>

    <ElDialog v-model="passwordDialog" title="重置密码" width="26rem">
      <ElForm label-width="5rem" @submit.prevent="submitReset">
        <ElFormItem label="新密码">
          <ElInput
            v-model="newPassword"
            name="new_password"
            type="password"
            autocomplete="new-password"
            show-password
          />
        </ElFormItem>
      </ElForm>
      <template #footer>
        <ElButton @click="passwordDialog = false">取消</ElButton>
        <ElButton type="primary" @click="submitReset">重置</ElButton>
      </template>
    </ElDialog>

    <ElDialog v-model="rolesDialog" title="分配角色" width="28rem">
      <ElSelect v-model="assignedRoleIds" multiple placeholder="选择角色" class="panel__select">
        <ElOption v-for="role in roles" :key="role.id" :label="role.name" :value="role.id" />
      </ElSelect>
      <template #footer>
        <ElButton @click="rolesDialog = false">取消</ElButton>
        <ElButton type="primary" @click="submitRoles">保存</ElButton>
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

.panel__select {
  width: 100%;
}

.panel :focus-visible {
  outline: 2px solid var(--el-color-primary);
  outline-offset: 2px;
}
</style>
