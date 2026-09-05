import prettier from '@vue/eslint-config-prettier'
import { defineConfigWithVueTs, vueTsConfigs } from '@vue/eslint-config-typescript'
import pluginVue from 'eslint-plugin-vue'
import accessibility from 'eslint-plugin-vuejs-accessibility'

export default defineConfigWithVueTs(
  { files: ['**/*.{ts,vue}'] },
  { ignores: ['dist/**', 'node_modules/**', 'src/api/generated/**'] },
  pluginVue.configs['flat/recommended'],
  vueTsConfigs.recommendedTypeChecked,
  // Q32 requires the Web to be keyboard-operable, with labelled inputs and status that is not
  // expressed by colour alone. These rules are what make that a gate rather than a review note:
  // an input without a label and a click handler without a keyboard equivalent both fail here.
  ...accessibility.configs['flat/recommended'],
  prettier,
  {
    rules: {
      // A stub that has to accept arguments in order to type its own call record does not read
      // them. The underscore prefix is the declaration that this is deliberate.
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
      // The session cookie is `HttpOnly`, so nothing in this application can read it and
      // nothing should try. `localStorage` is forbidden for the same reason §六 forbids it: a
      // token kept there survives the tab and is readable by any script on the page.
      'no-restricted-globals': [
        'error',
        { name: 'localStorage', message: '§六: 不在 localStorage 保存令牌' },
      ],
      'no-restricted-properties': [
        'error',
        { object: 'window', property: 'localStorage', message: '§六: 不在 localStorage 保存令牌' },
      ],
    },
  },
)
