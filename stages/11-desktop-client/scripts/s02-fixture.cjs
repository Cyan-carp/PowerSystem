// Isolated synthetic backend + real stage-four compiled UI. No upstream proxy.
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const artifactRoot = path.resolve(__dirname, '../../../artifacts/v5-桌面客户端');
const runArg = process.argv.indexOf('--run-directory');
const run = path.resolve(runArg >= 0 ? process.argv[runArg + 1] : path.join(artifactRoot, 's02-20261026-r1'));
if (!run.startsWith(artifactRoot + path.sep)) throw new Error('Run directory must stay inside this project v5-desktop artifacts.');
fs.mkdirSync(run, { recursive: true });
const root = path.join(run, 'frontend');
const csv = Buffer.from('\ufeffdevice,value\r\nS02-SYNTHETIC,42\r\n');
const artifactID = '11111111-1111-4111-8111-111111111111';
const runID = 's02-synthetic-run';
const digest = crypto.createHash('sha256').update(csv).digest('hex');
const summary = { device_total: 1, online: 1, offline: 0, fault: 0, current_power_kw: 42, active_alarms: 0, operational_health_percent: 100, today_energy_kwh: null, retained_energy_kwh: null, health_score_percent: null, health_valid_count: 0, health_unknown_count: 1, health_provisional: true };
const device = { id: 1, device_code: 'S02-SYNTHETIC', name: 'S02 合成测试设备', dev_type: 'inverter', vendor: 'fixture', is_simulated: true, reference_vendor: 'fixture', reference_model: 'fixture', station_code: 'S02', group_name: '隔离测试', created_at: new Date().toISOString(), updated_at: new Date().toISOString() };
const alarms = new Map();
let offline = false, unauthorized = false, nextAlarm = 1000, ticks = 0;
const counts = {};
function evidence(code) { fs.appendFileSync(path.join(run, 'fixture-events.jsonl'), JSON.stringify({ at: new Date().toISOString(), code }) + '\n'); }
function page(list) { return { list, total: list.length, page: 1, page_size: 100 }; }
function alarm(id) { return { id, device_id: 1, rule_id: null, metric: 'temperature', level: 'major', value: 42, threshold: 40, status: 'unhandled', acked_by: null, acked_at: null, recovered_at: null, triggered_at: new Date().toISOString(), archived: false, origin: 'normal' }; }
function artifact() { return { artifact_id: artifactID, run_id: runID, status: 'ready', mime_type: 'text/csv', size: csv.length, sha256: digest, name: 's02-artifact.csv', expires_at: new Date(Date.now() + 3600000).toISOString() }; }
function send(res, data, status = 200, raw = false) { res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(JSON.stringify(raw ? data : { code: status === 200 ? 0 : status, message: status === 200 ? '' : 'S02 fixture error', data })); }
function controlHtml() { return `<!doctype html><meta charset="utf-8"><title>S02 隔离控制台</title><style>body{font:18px system-ui;max-width:900px;margin:40px auto}button{font:inherit;margin:8px;padding:12px}pre{white-space:pre-wrap}</style><h1>S02 隔离控制台</h1><p>仅 127.0.0.1:4175 合成测试，不连接生产。客户端账号 A 或 B，密码任意非空测试文本。勿输入真实凭据。</p><p>先切回客户端或隐藏窗口，再用“延迟告警”留出切换时间。</p>${['alarm','repeat','delayed-alarm','offline','online','401','clear401'].map(key => `<button onclick="act('${key}')">${{alarm:'立即新告警',repeat:'重复同一告警','delayed-alarm':'5秒后新告警',offline:'仅测试服务断线',online:'恢复测试服务','401':'下一请求返回401',clear401:'清除401'}[key]}</button>`).join('')}<p><a href="/reports" target="_blank">浏览器报表入口</a></p><pre id="result"></pre><script>async function act(action){document.getElementById('result').textContent=await(await fetch('/__control/'+action,{method:'POST'})).text()}setInterval(async()=>{document.getElementById('result').textContent=await(await fetch('/__state')).text()},3000)</script>`; }
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1:4175'), p = url.pathname;
  if (p === '/__control') { res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); res.end(controlHtml()); return; }
  if (p === '/__state') return send(res, { batch: path.basename(run), offline, unauthorized, lastAlarm: nextAlarm, syntheticServerTicks: ticks, counts, artifact: { id: artifactID, runID, size: csv.length, sha256: digest } }, 200, true);
  if (p.startsWith('/__control/') && req.method === 'POST') {
    const action = p.split('/').pop();
    function broadcast(id) { const data = alarm(id); alarms.set(id, data); for (const ws of sockets.clients) ws.send(JSON.stringify({ type: 'alarm_created', data })); evidence('synthetic-alarm'); }
    if (action === 'offline') { offline = true; for (const ws of sockets.clients) ws.terminate(); }
    if (action === 'online') offline = false;
    if (action === '401') { unauthorized = true; for (const ws of sockets.clients) ws.terminate(); }
    if (action === 'clear401') unauthorized = false;
    if (action === 'alarm') broadcast(++nextAlarm);
    if (action === 'repeat') broadcast(nextAlarm);
    if (action === 'delayed-alarm') setTimeout(() => broadcast(++nextAlarm), 5000);
    evidence('control-' + action); return send(res, { accepted: true });
  }
  if (p.startsWith('/api/')) {
    // Test-only timing evidence: no headers, bodies, credentials or query strings.
    fs.appendFileSync(path.join(run, 'fixture-requests.jsonl'), JSON.stringify({ at: new Date().toISOString(), method: req.method, path: p }) + '\n');
    counts[req.method + ' ' + p] = (counts[req.method + ' ' + p] || 0) + 1;
    if (offline) { req.socket.destroy(); return; }
    if (p === '/api/v1/auth/login') {
      let data = ''; for await (const chunk of req) data += chunk;
      const name = JSON.parse(data).username === 'B' ? 'B' : 'A'; unauthorized = false;
      return send(res, { token: 'S02-SYNTHETIC-' + name, token_type: 'Bearer', expires_in: 3600, user: { id: name === 'A' ? 1 : 2, username: name, real_name: '隔离账号' + name, role: 'viewer' } });
    }
    if (unauthorized || !['Bearer S02-SYNTHETIC-A', 'Bearer S02-SYNTHETIC-B'].includes(req.headers.authorization)) return send(res, null, 401);
    if (p === '/api/v1/ws-ticket') return send(res, { ticket: crypto.randomUUID(), expires_in: 60 });
    // No write endpoint is implemented beyond login and one-time connection ticket.
    if (req.method !== 'GET') { evidence('business-write-rejected'); return send(res, null, 405); }
    if (p.includes('/export') || url.searchParams.get('format') === 'csv' || p.endsWith('/download')) { res.writeHead(200, { 'Content-Type': 'text/csv', 'Content-Length': csv.length }); res.end(csv); return; }
    if (p.startsWith('/api/v1/agent/artifacts/')) return send(res, artifact(), 200, true);
    if (p === '/api/v1/dashboard/summary') return send(res, { ...summary, active_alarms: alarms.size });
    if (p === '/api/v1/dashboard/overview') return send(res, { scope: url.searchParams.get('scope') || 'business', station_code: '', computed_at: new Date().toISOString(), partial: false, expected_device_count: 1, registered_device_count: 1, observed_device_count: 1, health_rule_version: 's02-fixture', summary, stations: [] });
    if (p === '/api/v1/alarms') return send(res, page([...alarms.values()].reverse()));
    if (/^\/api\/v1\/alarms\/\d+$/.test(p)) return send(res, alarms.get(Number(p.split('/').pop())) || alarm(Number(p.split('/').pop())));
    if (p === '/api/v1/devices') return send(res, page([device]));
    if (p === '/api/v1/devices/1') return send(res, device);
    if (p.endsWith('/telemetry/latest')) return send(res, { device_id: 1, ...summary, ts_ms: Date.now(), temperature: 42, power: 42, voltage: 220, current: 1, status: 1 });
    if (p.endsWith('/telemetry')) return send(res, { device_id: 1, metric: 'power', points: [[Date.now(), 42]] });
    if (p === '/api/v1/stations') return send(res, { scope: url.searchParams.get('scope'), list: [], total: 0 });
    if (p === '/api/v1/health/devices') return send(res, { devices: [], stations: [], complete: true, computed_at: new Date().toISOString(), rule_version: 's02-fixture' });
    if (p === '/api/v1/reports/availability') return send(res, { rule_version: 's02-fixture', computed_at: new Date().toISOString(), start: url.searchParams.get('start'), end: url.searchParams.get('end'), devices: [], stations: [] });
    if (p === '/api/v1/reports/alarm-timeliness') { const stat = { count: 0, mean_ms: null, median_ms: null, p90_ms: null }; return send(res, { rule_version: 's02-fixture', computed_at: new Date().toISOString(), result: { scope: 'fixture', ack_timeliness: stat, recover_timeliness: stat, telemetry_recover_timeliness: stat, prediction_recover_timeliness: stat, recovery_reasons: {} } }); }
    if (p === '/api/v1/agent/schedules') return send(res, { list: [], runs: [{ run_id: runID, turn_id: 1, session_id: 's02-synthetic-session', status: 'succeeded', capability: 'assistant.answer' }] }, 200, true);
    if (p === '/api/v1/agent/runs/' + runID) return send(res, { protocol: 'assistant/v4-draft1', run_id: runID, turn_id: 1, session_id: 's02-synthetic-session', status: 'succeeded', phase: 'finished', cancel_requested: false, requires_manual_reconciliation: false, final_answer: 'S02 合成历史产物，仅供另存为测试；未创建服务端任务。', answer_version: 1, result_refs: [], actions: [], artifacts: [artifact()], terminal_event: null, last_seq: 1, error: null }, 200, true);
    return send(res, { list: [], total: 0, offset: 0, page_size: 100 });
  }
  const local = path.resolve(root, '.' + decodeURIComponent(p));
  if (!local.startsWith(root + path.sep) && local !== root) { res.writeHead(403); res.end(); return; }
  const file = fs.existsSync(local) && fs.statSync(local).isFile() ? local : path.join(root, 'index.html');
  const types = { '.html': 'text/html; charset=utf-8', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' };
  res.writeHead(200, { 'Content-Type': types[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' }); fs.createReadStream(file).pipe(res);
});
// Minimal server-to-client fixture frames; no npm runtime dependency.
const sockets = { clients: new Set() };
server.on('upgrade', (req, socket, head) => {
  if (offline || unauthorized || !req.url.startsWith('/ws/realtime?ticket=')) return socket.destroy();
  const accept = crypto.createHash('sha1').update(req.headers['sec-websocket-key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
  socket.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + '\r\n\r\n');
  const ws = { terminate: () => socket.destroy(), send: text => { const body = Buffer.from(text); const header = body.length < 126 ? Buffer.from([0x81, body.length]) : Buffer.from([0x81, 126, body.length >> 8, body.length & 255]); socket.write(Buffer.concat([header, body])); } };
  sockets.clients.add(ws); socket.on('close', () => sockets.clients.delete(ws)); socket.on('error', () => sockets.clients.delete(ws));
  socket.on('data', bytes => { if ((bytes[0] & 15) === 8) socket.end(Buffer.from([0x88, 0])); });
  evidence('websocket-connected');
});
const timer = setInterval(() => { ticks++; }, 1000);
server.listen(4175, '127.0.0.1', () => { fs.writeFileSync(path.join(run, 'fixture-manifest.json'), JSON.stringify({ batch: path.basename(run), processId: process.pid, origin: 'http://127.0.0.1:4175', productionNetwork: false, csvSize: csv.length, csvSHA256: digest, artifactID, runID }, null, 2)); console.log('S02 fixture ready: http://127.0.0.1:4175/__control'); });
function close() { clearInterval(timer); for (const ws of sockets.clients) ws.terminate(); server.close(() => process.exit()); }
process.on('SIGINT', close); process.on('SIGTERM', close);
