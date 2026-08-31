<script setup lang="ts">
/**
 * The login page. Q32's baseline in one form: labelled inputs, keyboard operation, an error that
 * is announced rather than only coloured.
 */
import { ElButton, ElForm, ElFormItem, ElInput } from 'element-plus'
import { computed, onMounted, ref, useTemplateRef } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ControlPlaneError } from '@/api/controlPlane'
import { OVERVIEW_ROUTE } from '@/router'
import { useSessionStore } from '@/session/store'

const session = useSessionStore()
const router = useRouter()
const route = useRoute()

const loginName = ref('')
const password = ref('')
const submitting = ref(false)
const refusal = ref<string | null>(null)
const fieldMessages = ref<Record<string, string>>({})

// Focus is taken on mount rather than declared with `autofocus`. The visible effect is the
// same — the caret starts in 登录名, where a keyboard operator expects it (Q32) — but the
// attribute is only honoured while the document is being parsed, so on a route change from a
// lapsed session it does nothing. This works either way, and it is the form
// `vuejs-accessibility/no-autofocus` asks for. Safe here specifically because the form is the
// whole page: there is no reading position to move focus away from.
const nameField = useTemplateRef<InstanceType<typeof ElInput>>('nameField')
onMounted(() => nameField.value?.focus())

// Element Plus reads this prop as `val ? "error" : ""`, so an empty string is exactly its "no
// error" case. Stated rather than left as the `undefined` an absent key gives, so the value the
// prop receives is the same type whether or not the field was rejected.
const loginNameError = computed(() => fieldMessages.value.login_name ?? '')
const passwordError = computed(() => fieldMessages.value.password ?? '')

const canSubmit = computed(() => loginName.value.length > 0 && password.value.length > 0)

async function submit(): Promise<void> {
  if (!canSubmit.value || submitting.value) {
    return
  }
  submitting.value = true
  refusal.value = null
  fieldMessages.value = {}
  try {
    await session.logIn({ login_name: loginName.value, password: password.value })
    // `next` is a path this application produced when the guard redirected here. Only a relative
    // one is followed: an absolute URL in a query parameter is how a login page becomes an open
    // redirect.
    const next = route.query.next
    const target =
      typeof next === 'string' && next.startsWith('/') && !next.startsWith('//') ? next : null
    await router.replace(target ?? { name: OVERVIEW_ROUTE })
  } catch (error) {
    if (!(error instanceof ControlPlaneError)) {
      throw error
    }
    // `message` is the backend's own Simplified Chinese `title`, so an `error_code` this page has
    // never seen still produces something the operator can act on (§5.15).
    refusal.value = error.message
    fieldMessages.value = Object.fromEntries(
      error.fieldErrors.map((item) => [item.field, item.message]),
    )
    password.value = ''
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <main class="login">
    <section class="login__panel" aria-labelledby="login-heading">
      <h1 id="login-heading" class="login__heading">SOP 合规检测系统</h1>
      <p class="login__hint">请使用本地账户登录</p>

      <!-- `alert` so a screen reader announces the refusal without the operator having to go
           looking for it, and the leading ✕ means the failure is not carried by colour alone
           (Q32). -->
      <p v-if="refusal" class="login__refusal" role="alert">
        <span aria-hidden="true">✕</span>
        {{ refusal }}
      </p>

      <ElForm label-position="top" @submit.prevent="submit">
        <ElFormItem label="登录名" :error="loginNameError">
          <!-- `id`/`for` is what `ElFormItem`'s label binds through. Focus is taken by the
               `onMounted` above rather than by `autofocus`. -->
          <ElInput
            id="login-name"
            ref="nameField"
            v-model="loginName"
            name="login_name"
            autocomplete="username"
            :disabled="submitting"
          />
        </ElFormItem>
        <ElFormItem label="密码" :error="passwordError">
          <ElInput
            id="password"
            v-model="password"
            name="password"
            type="password"
            autocomplete="current-password"
            show-password
            :disabled="submitting"
            @keyup.enter="submit"
          />
        </ElFormItem>
        <ElFormItem>
          <ElButton
            type="primary"
            native-type="submit"
            class="login__submit"
            :loading="submitting"
            :disabled="!canSubmit || submitting"
          >
            {{ submitting ? '正在登录…' : '登录' }}
          </ElButton>
        </ElFormItem>
      </ElForm>
    </section>
  </main>
</template>

<style scoped>
.login {
  display: flex;
  align-items: center;
  justify-content: center;
  /* 1366×768 is the smallest supported desktop (Q32), so the panel is centred rather than
     vertically stretched. */
  min-height: 100vh;
  background: var(--el-bg-color-page);
}

.login__panel {
  width: 26rem;
  padding: 2rem;
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
  background: var(--el-bg-color);
}

.login__heading {
  margin: 0 0 0.25rem;
  font-size: 1.35rem;
}

.login__hint {
  margin: 0 0 1.5rem;
  color: var(--el-text-color-secondary);
}

.login__refusal {
  display: flex;
  gap: 0.5rem;
  margin: 0 0 1rem;
  padding: 0.625rem 0.75rem;
  border: 1px solid var(--el-color-danger);
  border-radius: 4px;
  background: var(--el-color-danger-light-9);
  color: var(--el-color-danger);
}

.login__submit {
  width: 100%;
}

/* Q32 requires focus to be visible. Element Plus's own outline is subtle on a light panel, so
   the ring is stated here rather than left to the theme. */
.login__panel :focus-visible {
  outline: 2px solid var(--el-color-primary);
  outline-offset: 2px;
}
</style>
