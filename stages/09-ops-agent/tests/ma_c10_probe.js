// Test-only observation. No dispatchEvent, property override, API task mutation,
// scheduler interception, or injected browser action. Native event trust and
// actual rendered card text accompany the gateway's HTTP timestamps.
(() => {
  const pageID = crypto.randomUUID()
  let last = '', pending = false
  function capture(kind, isTrusted = false) {
    const card = document.querySelector('.ops-task')
    const data = {
      page_id: pageID, kind, is_trusted: isTrusted,
      visibility_state: document.visibilityState, hidden: document.hidden,
      page_time_ms: performance.now(), client_time: new Date().toISOString(),
      status: card?.querySelector('header span')?.textContent || '',
      snapshot: card?.querySelector('.ops-analysis')?.textContent || '',
      error: card?.querySelector('[role="alert"]')?.textContent || '',
      user_agent: navigator.userAgent,
    }
    const fingerprint = JSON.stringify([data.visibility_state, data.status, data.snapshot, data.error])
    if (kind === 'dom' && fingerprint === last) return
    last = fingerprint
    navigator.sendBeacon('/__ma_c10/probe', JSON.stringify(data))
  }
  document.addEventListener('visibilitychange', event => capture('visibilitychange', event.isTrusted))
  window.addEventListener('pageshow', event => capture('pageshow', event.isTrusted))
  new MutationObserver(() => {
    if (pending) return
    pending = true
    queueMicrotask(() => { pending = false; capture('dom') })
  }).observe(document.documentElement, { subtree: true, childList: true, characterData: true })
  capture('initial')
})()
