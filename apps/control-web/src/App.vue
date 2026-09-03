<script setup lang="ts">
/**
 * The root. The router decides everything, including which layout is in use — it arrives
 * with the first pages, in the stage this workspace exists to carry.
 *
 * `ElConfigProvider` is what puts the component library into Simplified Chinese (Q32). It is the
 * form Element Plus documents, and it is where the rest of the library's global configuration
 * (`size`, `namespace`) goes when a slice needs it — one place rather than two. Passing `locale`
 * to `app.use(ElementPlus, …)` would work equally well; it is not the alternative that was
 * rejected for a technical reason.
 */
import { ElConfigProvider } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
</script>

<template>
  <ElConfigProvider :locale="zhCn" />
</template>
