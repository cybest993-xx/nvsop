<script setup lang="ts">
/**
 * The protected layout: everything a signed-in operator sees sits inside it.
 *
 * Navigation is §5.4's five items. Four of them have no page in this slice, and are rendered as
 * unavailable rather than as links to an empty view — the shell says what exists.
 */
import { ElButton } from 'element-plus'
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'

import { ControlPlaneError } from '@/api/controlPlane'
import { LOGIN_ROUTE, OVERVIEW_ROUTE } from '@/router'
import { useSessionStore } from '@/session/store'

const session = useSessionStore()
const router = useRouter()

interface NavigationItem {
  label: string
  route?: string
  /** Why the section cannot be opened yet, shown as its title. Absent once it has a page. */
  pending?: string
}

// §5.4's navigation, in its fixed order.
const navigation: NavigationItem[] = [
  { label: '概览', route: OVERVIEW_ROUTE },
  { label: '工位与设备', pending: '该功能尚未上线' },
  { label: 'SOP 模板', pending: '该功能尚未上线' },
  { label: '训练数据集', pending: '该功能尚未上线' },
  { label: '用户与权限', pending: '该功能尚未上线' },
]

const displayName = computed(() => session.current?.display_name ?? '')
const logoutFailure = ref<string | null>(null)

async function logOut(): Promise<void> {
  logoutFailure.value = null
  try {
    await session.logOut()
  } catch (error) {
    logoutFailure.value =
      error instanceof ControlPlaneError ? error.message : '请求未能完成，请稍后重试'
    return
  }
  await router.replace({ name: LOGIN_ROUTE })
}
</script>

<template>
  <div class="shell">
    <header class="shell__header">
      <span class="shell__product">SOP 合规检测系统</span>
      <div class="shell__account">
        <span class="shell__user">{{ displayName }}</span>
        <ElButton link type="primary" @click="logOut">退出</ElButton>
      </div>
    </header>

    <div class="shell__body">
      <!-- A real `nav` with a list, so a screen reader can enumerate the sections and a keyboard
           can tab through only the ones that lead somewhere. -->
      <nav class="shell__nav" aria-label="主导航">
        <ul class="shell__nav-list">
          <li v-for="item in navigation" :key="item.label">
            <RouterLink v-if="item.route" :to="{ name: item.route }" class="shell__link">
              {{ item.label }}
            </RouterLink>
            <!-- `aria-disabled` and the trailing note rather than grey text alone: colour is not
                 the only expression of state (Q32). -->
            <span
              v-else
              class="shell__link shell__link--pending"
              aria-disabled="true"
              :title="item.pending"
            >
              {{ item.label }}
              <small class="shell__pending-note">（未上线）</small>
            </span>
          </li>
        </ul>
      </nav>

      <main class="shell__main">
        <p v-if="logoutFailure" class="shell__error" role="alert">{{ logoutFailure }}</p>
        <RouterView />
      </main>
    </div>
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}

.shell__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 1.5rem;
  height: 3.5rem;
  border-bottom: 1px solid var(--el-border-color);
}

.shell__product {
  font-weight: 600;
}

.shell__account {
  display: flex;
  align-items: center;
  gap: 1rem;
}

.shell__user {
  color: var(--el-text-color-regular);
}

.shell__body {
  display: flex;
  flex: 1;
}

.shell__nav {
  /* Fixed width so the content column is stable between 1366 and 1920 px (Q32). */
  width: 13rem;
  padding: 1rem 0;
  border-right: 1px solid var(--el-border-color);
}

.shell__nav-list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.shell__link {
  display: block;
  padding: 0.6rem 1.5rem;
  color: var(--el-text-color-primary);
  text-decoration: none;
}

.shell__link:hover {
  background: var(--el-fill-color-light);
}

.shell__link--pending {
  color: var(--el-text-color-disabled);
  cursor: not-allowed;
}

.shell__pending-note {
  font-size: 0.75rem;
}

.shell__link.router-link-active {
  /* A left rule as well as the colour, so the current section is identifiable without it. */
  border-left: 3px solid var(--el-color-primary);
  padding-left: calc(1.5rem - 3px);
  color: var(--el-color-primary);
  font-weight: 600;
}

.shell__main {
  flex: 1;
  padding: 1.5rem;
}

.shell__error {
  margin: 0 0 1rem;
  color: var(--el-color-danger);
}

.shell :focus-visible {
  outline: 2px solid var(--el-color-primary);
  outline-offset: 2px;
}
</style>
