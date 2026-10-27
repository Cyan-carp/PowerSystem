const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const net = require('node:net');
const { once } = require('node:events');
const { createStartupProxy } = require('../scripts/startup-proxy.cjs');

function requestTunnel(port, authority) {
  return new Promise((resolve, reject) => {
    const request = http.request({ hostname: '127.0.0.1', port, method: 'CONNECT', path: authority });
    request.on('error', reject); request.on('connect', (response, socket) => resolve({ response, socket })); request.end();
  });
}
test('owner proxy rejects until enabled, restricts host, then relays bytes without TLS interception', async () => {
  const echo = net.createServer(socket => socket.pipe(socket)); echo.listen(0, '127.0.0.1'); await once(echo, 'listening');
  let connections = 0;
  const proxy = createStartupProxy({ connect: () => { connections++; return net.connect(echo.address().port, '127.0.0.1'); } });
  proxy.server.listen(0, '127.0.0.1'); await once(proxy.server, 'listening');
  try {
    const rejected = await requestTunnel(proxy.server.address().port, 'www.cyancarp.cn:443');
    assert.equal(rejected.response.statusCode, 502); rejected.socket.destroy(); assert.equal(connections, 0);
    proxy.enable();
    const wrong = await requestTunnel(proxy.server.address().port, 'example.com:443');
    assert.equal(wrong.response.statusCode, 502); wrong.socket.destroy(); assert.equal(connections, 0);
    const valid = await requestTunnel(proxy.server.address().port, 'www.cyancarp.cn:443');
    assert.equal(valid.response.statusCode, 200); const received = once(valid.socket, 'data');
    valid.socket.write('synthetic-encrypted-bytes'); assert.equal((await received)[0].toString(), 'synthetic-encrypted-bytes');
    valid.socket.destroy(); assert.equal(connections, 1);
  } finally { proxy.close(); echo.close(); }
});
