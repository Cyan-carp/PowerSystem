// Run the first-version public core routes in installed Chrome/Edge 154.
// The ephemeral login token stays in process memory and is never logged.
import { execFileSync } from 'node:child_process'
import { createRequire } from 'node:module'
import path from 'node:path'

const require = createRequire(path.resolve('artifacts/stage4/browser-harness/index.js'))
const { chromium } = require('playwright-core')
const base = 'https://8.138.10.222'
const password = execFileSync('ssh', [
  'root@192.168.111.2', 'cat /etc/powersystem/p0-admin-password',
], { encoding: 'utf8', timeout: 15000 }).trim()
const loginResponse = await fetch(base + '/api/v1/auth/login', {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ username: 'p0-admin', password }),
})
const login = (await loginResponse.json()).data
if (!login.token || !login.user) throw new Error('test login unavailable')

for (const [name, channel] of [['Chrome 154', 'chrome'], ['Edge 154', 'msedge']]) {
  const browser = await chromium.launch({ channel, headless: true, args: ['--no-sandbox'] })
  try {
    const context = await browser.newContext({ ignoreHTTPSErrors: false })
    await context.addInitScript(({ token, user }) => {
      if (location.origin === 'https://8.138.10.222') {
        sessionStorage.setItem('powersystem_token', token)
        sessionStorage.setItem('powersystem_user', JSON.stringify(user))
      }
    }, { token: login.token, user: login.user })
    const page = await context.newPage()
    const results = []
    for (const route of ['/dashboard', '/devices', '/alarms', '/predictions']) {
      await page.goto(base + route, { waitUntil: 'domcontentloaded', timeout: 30000 })
      await page.waitForFunction(() => document.body.innerText.length > 150, null, { timeout: 15000 })
      const result = await page.evaluate(() => ({
        path: location.pathname,
        hasLogin: document.body.innerText.includes('登录平台'),
        hasError: document.body.innerText.includes('加载失败'),
        chars: document.body.innerText.length,
      }))
      const passed = result.path === route && !result.hasLogin && !result.hasError
      results.push({ route, passed, text_chars: result.chars })
      if (!passed) throw new Error(`${name}: ${route} did not render authenticated view`)
    }
    console.log(JSON.stringify({ browser: name, routes: results }))
    await context.close()
  } finally {
    await browser.close()
  }
}
