import { defineConfig } from '@hey-api/openapi-ts'

export default defineConfig({
  input: '../../packages/contracts/openapi.json',
  output: {
    path: 'src/api/generated',
    postProcess: ['prettier'],
  },
  plugins: ['@hey-api/typescript', '@hey-api/client-fetch', '@hey-api/sdk'],
})
