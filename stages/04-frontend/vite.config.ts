import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
 const env = loadEnv(mode, '.', 'VITE_')
 const target = env.VITE_API_TARGET || 'http://127.0.0.1:8080'
 return {
  plugins: [vue()],
  build: {
    rolldownOptions: {
      preserveEntrySignatures: 'allow-extension',
      output: {
        // Keep the existing authentication, API and realtime asset boundaries when
        // the optional desktop adapter changes the shared import graph.
        strictExecutionOrder: true,
        codeSplitting: {
          includeDependenciesRecursively: false,
          groups: ['auth', 'api', 'realtime'].map(name => ({
            name,
            test: (id: string) => id.replaceAll('\\', '/').endsWith(`/src/lib/${name}.ts`),
          })),
        },
      },
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target, changeOrigin: true },
      '/ws': { target: target.replace(/^http/, 'ws'), changeOrigin: false, ws: true },
    },
  },
 }
})
