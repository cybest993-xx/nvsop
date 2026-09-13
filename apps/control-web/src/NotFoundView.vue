<script setup lang="ts">
/**
 * The page for a path the application never served.
 *
 * It sits at the source root rather than in a slice because it belongs to no slice: any URL the
 * router's records do not name lands here, anonymous included — a stale bookmark outlives the
 * session that made it, and finding that out is not worth signing in for. The overview is the
 * application's real answer to "where do I go instead", so that is where the way back leads.
 */
import { OVERVIEW_ROUTE } from '@/router'
</script>

<template>
  <main class="not-found">
    <h1 class="not-found__title">页面不存在</h1>
    <p class="not-found__note">您访问的地址不存在或已被移动。</p>
    <RouterLink :to="{ name: OVERVIEW_ROUTE }" class="not-found__back">返回概览</RouterLink>
  </main>
</template>

<style scoped>
.not-found {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.75rem;
  min-height: 100vh;
}

.not-found__title {
  margin: 0;
  font-size: 1.5rem;
  font-weight: 600;
}

.not-found__note {
  margin: 0;
  color: var(--el-text-color-regular);
}

.not-found__back:focus-visible {
  outline: 2px solid var(--el-color-primary);
  outline-offset: 2px;
}
</style>
