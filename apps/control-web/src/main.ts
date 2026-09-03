/**
 * The browser entrypoint. Composition only: Element Plus, mount.
 *
 * Everything it installs is testable without it — each component is exercised at its own
 * seam, and this file has no behavior of its own to prove. Pinia and the router are installed
 * by the stage that delivers the state and the pages they carry; this stage delivers the
 * workspace the gate runs against, and the gate must not need more than that to hold.
 */

import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import { createApp } from 'vue'

import App from '@/App.vue'

const app = createApp(App)
// Installed without options: the locale, and any later global configuration, is set by
// `ElConfigProvider` in `App.vue` so there is one place it lives rather than two.
app.use(ElementPlus)
app.mount('#app')
