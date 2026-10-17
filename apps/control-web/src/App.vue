<script setup lang="ts">
/**
 * The root: the router decides everything, including which layout is in use.
 *
 * `NConfigProvider` is what puts the component library into Simplified Chinese (Q32), and it is
 * where the library's global configuration (`locale`, `dateLocale`, `themeOverrides`) goes when
 * something needs it — one place rather than two. `abstract` keeps it from adding a wrapper
 * element of its own, so it configures the tree without becoming part of it.
 *
 * `NMessageProvider` is what `useMessage()` reads. Naive UI has no imperative global message
 * object the way Element Plus did, so a component that shows a toast has to render inside this
 * provider; a test mounting such a component needs one above it too.
 */
import { NConfigProvider, NMessageProvider } from 'naive-ui'

import { appDateLocale, appLocale } from '@/naive-config'
import { themeOverrides } from '@/styles/naive-theme'
</script>

<template>
  <NConfigProvider
    abstract
    :locale="appLocale"
    :date-locale="appDateLocale"
    :theme-overrides="themeOverrides"
  >
    <NMessageProvider>
      <RouterView />
    </NMessageProvider>
  </NConfigProvider>
</template>
