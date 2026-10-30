// Compile Vue client templates while using the test's in-memory renderer.
export default {
  name: 'vue-renderer',
  viteEnvironment: 'client',
  setup: () => ({ teardown() {} }),
}
