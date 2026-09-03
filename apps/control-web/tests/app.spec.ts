/**
 * The unit gate has something real to run before the first page lands.
 *
 * The unit target is a gate, and a gate with no test directory to walk would either fail on
 * emptiness or have to be taught to excuse it — the second weakness spreads. Until the view
 * suites arrive with the pages they test, this proves the application the gate mounts is the
 * one these sources describe. The stage that delivers the pages removes it.
 */
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import App from '@/App.vue'

describe('the application root', () => {
  it('mounts with the component library in Simplified Chinese', () => {
    const wrapper = mount(App)

    const provider = wrapper.findComponent({ name: 'ElConfigProvider' })
    expect(provider.exists()).toBe(true)
    // The locale object is Element Plus's own; `name` is the independent literal that says
    // the provider received zh-cn rather than the library default (English).
    expect(provider.props('locale')?.name).toBe('zh-cn')
  })
})
