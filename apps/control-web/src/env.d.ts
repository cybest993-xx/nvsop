/// <reference types="vite/client" />

// Vue's SFC shim: without it TypeScript cannot type a `.vue` import.
declare module '*.vue' {
  import type { DefineComponent } from 'vue'

  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}
