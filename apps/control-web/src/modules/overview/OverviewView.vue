<script setup lang="ts">
/**
 * 概览 in this slice: the session the caller actually holds, and nothing invented.
 *
 * §5.4 is explicit that the overview shows only real configuration summary — C7 composes that
 * from each module's `summary()` once those modules exist. Until then this page reports the one
 * fact the backend can already answer for, rather than standing in for it with a mock.
 */
import { computed } from 'vue'

import { useSessionStore } from '@/session/store'

const session = useSessionStore()

// §5.15: instants are UTC on the wire and rendered in Asia/Shanghai.
const formatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  dateStyle: 'medium',
  timeStyle: 'short',
})

const expiresAt = computed(() => {
  const raw = session.current?.expires_at
  return raw ? formatter.format(new Date(raw)) : ''
})
</script>

<template>
  <section aria-labelledby="overview-heading">
    <h1 id="overview-heading" class="overview__heading">概览</h1>

    <dl v-if="session.current" class="overview__facts">
      <dt>登录名</dt>
      <dd>{{ session.current.login_name }}</dd>
      <dt>姓名</dt>
      <dd>{{ session.current.display_name }}</dd>
      <dt>会话到期</dt>
      <dd>{{ expiresAt }}</dd>
    </dl>

    <p class="overview__pending">配置摘要随各模块上线后显示。</p>
  </section>
</template>

<style scoped>
.overview__heading {
  margin: 0 0 1.25rem;
  font-size: 1.25rem;
}

.overview__facts {
  display: grid;
  grid-template-columns: 7rem 1fr;
  gap: 0.5rem 1rem;
  margin: 0 0 1.5rem;
  max-width: 32rem;
}

.overview__facts dt {
  color: var(--el-text-color-secondary);
}

.overview__facts dd {
  margin: 0;
}

.overview__pending {
  color: var(--el-text-color-secondary);
}
</style>
