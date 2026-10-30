import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

// Render client components with Vue's in-memory renderer in Node tests.
const plugin = vue()
const hook = plugin.transform!
const transform = typeof hook === 'function' ? hook : hook.handler
plugin.transform = {
  handler(code, id, options) {
    return transform.call(this, code, id, { ...options, ssr: false })
  },
}
export default defineConfig({ plugins: [plugin] })
