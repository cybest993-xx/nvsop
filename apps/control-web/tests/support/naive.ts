import { NConfigProvider, NMessageProvider } from 'naive-ui'
import { mount } from '@vue/test-utils'
import type { ComponentMountingOptions, VueWrapper } from '@vue/test-utils'
import { defineComponent, h, type Component } from 'vue'

import { appDateLocale, appLocale } from '@/naive-config'
import { themeOverrides } from '@/styles/naive-theme'

/**
 * `NModal` 的内容经 vueuc 的 `LazyTeleport` 传送到 body（Vue 内建 Teleport 的测试桩拦不住
 * 它），测试的 `wrapper.find` 就够不到弹窗内容。这里原位渲染它的默认插槽（Element Plus 的
 * 对话框默认就是原位渲染），内容是否可见仍由 `NModal` 自己的 `display-directive` 决定。
 */
// eslint-disable-next-line vue/one-component-per-file -- 测试装配的替身组件，不是页面组件
const InlineLazyTeleport = defineComponent({
  name: 'LazyTeleport',
  props: {
    to: { type: [String, Object], default: undefined },
    disabled: Boolean,
    show: { type: Boolean, required: true },
  },
  setup(props, { slots }) {
    return () => (props.show ? slots.default?.() : null)
  },
})

// jsdom 没有 matchMedia，而 naive-ui 的下拉菜单在挂载虚拟列表时读它；不打桩时在 jsdom 里
// 打开任何 NSelect 都会抛错。只补这个缺口，不模拟媒体查询行为。
if (typeof window !== 'undefined' && typeof window.matchMedia !== 'function') {
  window.matchMedia = (query: string): MediaQueryList => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })
}

/**
 * 在真实 Naive UI 提供者内挂载组件。
 *
 * 生产树里的两个提供者（`App.vue`）在这里必须以真实组件出现，不能注入替身：
 * `naive-ui` 不导出 message 的注入键，而 `useMessage()` 在提供者之外直接抛错；
 * 多个套件断言真实 toast 文本（经 Teleport 落到 `document.body`），只有真实
 * `NMessageProvider` 能产生。提供者的配置与 `App.vue` 同源：locale 来自
 * `@/naive-config`，主题覆盖来自 `@/styles/naive-theme`——快照断言的是生产里会出现的文本。
 *
 * `NConfigProvider` 用 `abstract`，不引入包裹元素，挂载结果的 DOM 就是组件自身，
 * 既有 `wrapper.find(...)` / `wrapper.html()` 断言不受影响；需要组件实例的断言
 * 用 `wrapper.findComponent(Component)`，例如 `setProps`。
 */
export function mountWithNaive<C extends Component>(
  component: C,
  options: ComponentMountingOptions<C> = {},
): VueWrapper {
  const { global, props, slots, attachTo } = options
  // Teleport 打桩后消息容器留在组件树内，toast 断言用 `wrapper.text()` 而不是
  // `document.body.textContent`；`LazyTeleport` 用原位渲染的替身，弹窗内容同样留在树内。
  const mergedGlobal = {
    ...global,
    stubs: { teleport: true, LazyTeleport: InlineLazyTeleport, ...global?.stubs },
  }
  // eslint-disable-next-line vue/one-component-per-file -- 测试宿主，随每次挂载创建
  const host = defineComponent({
    name: 'NaiveTestHost',
    render: () =>
      h(
        NConfigProvider,
        { abstract: true, locale: appLocale, dateLocale: appDateLocale, themeOverrides },
        {
          default: () =>
            h(NMessageProvider, null, {
              default: () => h(component, props, slots),
            }),
        },
      ),
  })
  return mount(host, { global: mergedGlobal, attachTo })
}
