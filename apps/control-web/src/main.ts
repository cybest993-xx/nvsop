/**
 * The browser entrypoint. Composition only: Pinia, the router, mount.
 *
 * Everything it installs is testable without it — the store, the guard and each component are
 * exercised at their own seam, and this file has no behavior of its own to prove.
 */

import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from '@/App.vue'
import { createAppRouter } from '@/router'
import '@/styles/tokens.css'
import '@/styles/base.css'

const app = createApp(App)
app.use(createPinia())
app.use(createAppRouter())
// Naive UI is imported per component where it is used rather than installed globally, so a
// component that is never rendered never reaches the bundle. Its global configuration — locale,
// date locale, theme overrides — is set by `NConfigProvider` in `App.vue`, so there is one place
// it lives rather than two.
app.mount('#app')
