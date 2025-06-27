// Минимальный рабочий Cloudflare Worker для проксирования ElevenLabs API
// Основан на рекомендациях технического советника

export default {
  async fetch(request, env) {
    // Храним оригинальный URL
    const url = new URL(request.url);

    // --- HTTP прокси ---
    if (request.headers.get('Upgrade') !== 'websocket') {
      // убираем префикс /api, если он есть
      const upstreamHttp = `https://api.elevenlabs.io${url.pathname.replace(/^\/api/, '')}${url.search}`;
      
      // Клонируем заголовки и добавляем API ключ
      const headers = new Headers(request.headers);
      headers.set('xi-api-key', env.ELEVEN_KEY);
      headers.delete('host');
      
      const response = await fetch(upstreamHttp, {
        method: request.method,
        headers: headers,
        body: ['GET', 'HEAD'].includes(request.method) ? undefined : request.body
      });
      
      // Добавляем CORS заголовки
      const responseHeaders = new Headers(response.headers);
      responseHeaders.set('Access-Control-Allow-Origin', '*');
      responseHeaders.set('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
      responseHeaders.set('Access-Control-Allow-Headers', 'Content-Type, Authorization, xi-api-key, Upgrade, Connection, Sec-WebSocket-Key, Sec-WebSocket-Version, Sec-WebSocket-Protocol');
      
      return new Response(response.body, {
        status: response.status,
        headers: responseHeaders
      });
    }

    // --- WebSocket прокси ---
    console.log('WebSocket request to:', request.url);
    
    const upstreamWsURL = `wss://api.elevenlabs.io${url.pathname.replace(/^\/api/, '')}${url.search}`;
    console.log('Proxying to:', upstreamWsURL);

    // пара сокетов client/server
    const { 0: client, 1: server } = new WebSocketPair();
    server.accept();

    try {
      // коннект к ElevenLabs
      const upstreamResp = await fetch(upstreamWsURL, {
        headers: { 
          'Upgrade': 'websocket', 
          'xi-api-key': env.ELEVEN_KEY 
        }
      });
      
      console.log('Upstream response status:', upstreamResp.status);
      
      const upstream = upstreamResp.webSocket;
      if (!upstream) {
        console.error('No webSocket in upstream response');
        return new Response('Upstream rejected WS', { status: 502 });
      }
      
      upstream.accept();
      console.log('WebSocket connection established');

      // трубопровод
      server.addEventListener('message', e => {
        try {
          upstream.send(e.data);
        } catch (error) {
          console.error('Error sending to upstream:', error);
        }
      });
      
      upstream.addEventListener('message', e => {
        try {
          server.send(e.data);
        } catch (error) {
          console.error('Error sending to client:', error);
        }
      });
      
      server.addEventListener('close', e => {
        try {
          upstream.close(e.code, e.reason);
        } catch (error) {
          console.error('Error closing upstream:', error);
        }
      });
      
      upstream.addEventListener('close', e => {
        try {
          server.close(e.code, e.reason);
        } catch (error) {
          console.error('Error closing client:', error);
        }
      });

      return new Response(null, { 
        status: 101, 
        webSocket: client,
        headers: {
          'Access-Control-Allow-Origin': '*'
        }
      });
      
    } catch (error) {
      console.error('WebSocket proxy error:', error);
      return new Response(`WebSocket connection failed: ${error.message}`, { 
        status: 500,
        headers: {
          'Access-Control-Allow-Origin': '*'
        }
      });
    }
  }
}
