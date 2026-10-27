// Owner acceptance fixture only; never included in desktop packages.
// CONNECT forwards encrypted bytes to the single fixed site; no TLS interception or payload logs.
const http = require('node:http');
const net = require('node:net');

function createStartupProxy({ connect = () => net.connect(443, 'www.cyancarp.cn') } = {}) {
  let enabled = false;
  const sockets = new Set();
  const server = http.createServer((_request, response) => { response.writeHead(403); response.end(); });
  server.on('connection', socket => { sockets.add(socket); socket.on('error', () => {}); socket.on('close', () => sockets.delete(socket)); });
  server.on('connect', (request, client, head) => {
    if (!enabled || request.url !== 'www.cyancarp.cn:443' || sockets.size > 64) {
      client.end('HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n'); return;
    }
    const upstream = connect();
    sockets.add(upstream); upstream.once('close', () => sockets.delete(upstream));
    upstream.setTimeout(30000, () => upstream.destroy());
    const stop = () => { upstream.destroy(); client.destroy(); };
    upstream.on('error', stop); client.on('error', stop);
    client.once('close', () => upstream.destroy()); upstream.once('close', () => client.destroy());
    upstream.once('connect', () => {
      client.write('HTTP/1.1 200 Connection Established\r\n\r\n');
      if (head.length) upstream.write(head);
      client.pipe(upstream); upstream.pipe(client);
    });
  });
  return {
    server,
    enable: () => { enabled = true; },
    close: () => { for (const socket of sockets) socket.destroy(); server.close(); }
  };
}
module.exports = { createStartupProxy };
