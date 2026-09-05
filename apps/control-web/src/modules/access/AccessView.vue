<script setup lang="ts">
/**
 * 用户与权限: the orchestrator. The accounts and the roles live in their own panels; this file
 * holds what is shared between them — the data they both read, the visibility of the two tabs,
 * and the one refresh that runs after any successful change.
 *
 * §5.4 is explicit: 无权查看的模块不显示该导航项. A tab the caller cannot use is therefore
 * absent, not disabled and not replaced by an explanation. The router guard sends a caller who
 * holds neither permission to 概览, so this page never renders with both tabs missing. None of
 * this is the security boundary — the use case checks regardless — but an operator who fills in
 * a form and is then refused has been misled.
 */
import { ElTabPane, ElTabs } from 'element-plus'
import { computed, onMounted, ref } from 'vue'

import {
  ControlPlaneError,
  readPermissionCatalogue,
  readRoles,
  readUsers,
  type RoleView,
  type UserView,
} from '@/api/controlPlane'
import { useSessionStore } from '@/session/store'

import AccountsPanel from './AccountsPanel.vue'
import RolesPanel from './RolesPanel.vue'

const session = useSessionStore()

const users = ref<UserView[]>([])
const roles = ref<RoleView[]>([])
const catalogue = ref<string[]>([])
const loading = ref(true)
/** Why the listings could not be loaded, shown as text rather than only as a colour (Q32). */
const failure = ref('')

const mayViewUsers = computed(() => session.may('auth.user.view'))
const mayViewRoles = computed(() => session.may('auth.role.view'))

async function refresh(): Promise<void> {
  loading.value = true
  failure.value = ''
  try {
    if (mayViewUsers.value) {
      users.value = (await readUsers()).items
    }
    if (mayViewRoles.value) {
      roles.value = (await readRoles()).items
      catalogue.value = (await readPermissionCatalogue()).items
    }
  } catch (error) {
    if (!(error instanceof ControlPlaneError)) {
      throw error
    }
    failure.value = error.detail ?? error.message
  } finally {
    // Re-read who this caller now is, too: a change in one panel can change what the caller —
    // possibly themselves — may do, and the panels render their buttons from that set.
    await session.refreshIdentity()
    loading.value = false
  }
}

onMounted(refresh)
</script>

<template>
  <section aria-labelledby="access-heading">
    <h1 id="access-heading" class="access__heading">用户与权限</h1>

    <!-- `role="alert"` so a refusal is announced rather than only appearing; the text carries the
         state, not the colour (Q32). -->
    <p v-if="failure" class="access__failure" role="alert">{{ failure }}</p>

    <ElTabs>
      <ElTabPane v-if="mayViewUsers" label="账户" name="users">
        <AccountsPanel :users="users" :roles="roles" :loading="loading" @changed="refresh" />
      </ElTabPane>

      <ElTabPane v-if="mayViewRoles" label="角色" name="roles">
        <RolesPanel :roles="roles" :catalogue="catalogue" :loading="loading" @changed="refresh" />
      </ElTabPane>
    </ElTabs>
  </section>
</template>

<style scoped>
.access__heading {
  margin: 0 0 1.25rem;
  font-size: 1.25rem;
}

.access__failure {
  margin: 0 0 1rem;
  padding: 0.6rem 0.9rem;
  border-left: 3px solid var(--el-color-danger);
  background: var(--el-color-danger-light-9);
  color: var(--el-color-danger);
}
</style>
