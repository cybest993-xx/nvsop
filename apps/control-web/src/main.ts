/**
 * The browser entrypoint. Composition only: Pinia, the router, Element Plus, mount.
 *
 * Everything it installs is testable without it — the store, the guard and each component are
 * exercised at their own seam, and this file has no behavior of its own to prove.
 */

import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from '@/App.vue'
import { createAppRouter } from '@/router'

const app = createApp(App)
app.use(createPinia())
app.use(createAppRouter())
// Installed without options: the locale, and any later global configuration, is set by
// `ElConfigProvider` in `App.vue` so there is one place it lives rather than two.
app.use(ElementPlus)
app.mount('#app')
