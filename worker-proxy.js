// Cloudflare Worker для полного прокси ElevenLabs API с поддержкой WebSocket (April 2025+)
// Требует compatibility_date >= "2025-04-15"

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    
    // Обработка WebSocket upgrade запросов
    if (request.headers.get('Upgrade') === 'websocket') {
      return handleWebSocket(request, env);
    }
    
    // Обработка обычных HTTP запросов
    return handleHttpRequest(request, env);
  }
};

// Обработка WebSocket соединений (новый API April 2025)
async function handleWebSocket(request, env) {
  const url = new URL(request.url);
  
  // Извлекаем путь после /api/
  const apiPath = url.pathname.replace(/^\/api\//, '');
  
  // Строим целевой WebSocket URL
  const targetUrl = `wss://api.elevenlabs.io/${apiPath}${url.search}`;
  
  console.log('WebSocket proxy:', request.url, '->', targetUrl);
  
  try {
    // 1️⃣ Создаем пару WebSocket
    const webSocketPair = new WebSocketPair();
    const [clientWS, serverWS] = Object.values(webSocketPair);
    
    // Принимаем соединение от клиента
    serverWS.accept();
    
    // 2️⃣ Устанавливаем соединение с ElevenLabs (новый API!)
    const upstream = await fetch(targetUrl, {
      headers: { 
        'Upgrade': 'websocket',
        'xi-api-key': env.ELEVEN_KEY 
      }
    });
    
    if (upstream.status !== 101) {
      throw new Error(`WebSocket upgrade failed with status: ${upstream.status}`);
    }
    
    const remoteWS = upstream.webSocket;
    if (!remoteWS) {
      throw new Error('No WebSocket in upstream response');
    }
    
    remoteWS.accept();
    
    // 3️⃣ Проксируем фреймы в обе стороны
    remoteWS.addEventListener('message', (event) => {
      try {
        if (serverWS.readyState === WebSocket.OPEN) {
          serverWS.send(event.data);
        }
      } catch (error) {
        console.error('Error forwarding message to client:', error);
      }
    });
    
    serverWS.addEventListener('message', (event) => {
      try {
        if (remoteWS.readyState === WebSocket.OPEN) {
          remoteWS.send(event.data);
        }
      } catch (error) {
        console.error('Error forwarding message to server:', error);
      }
    });
    
    // Обработка закрытия соединений
    remoteWS.addEventListener('close', (event) => {
      console.log('Remote WebSocket closed:', event.code, event.reason);
      try {
        if (serverWS.readyState === WebSocket.OPEN) {
          serverWS.close(event.code, event.reason);
        }
      } catch (error) {
        console.error('Error closing client WebSocket:', error);
      }
    });
    
    serverWS.addEventListener('close', (event) => {
      console.log('Client WebSocket closed:', event.code, event.reason);
      try {
        if (remoteWS.readyState === WebSocket.OPEN) {
          remoteWS.close(event.code, event.reason);
        }
      } catch (error) {
        console.error('Error closing remote WebSocket:', error);
      }
    });
    
    // Обработка ошибок
    remoteWS.addEventListener('error', (event) => {
      console.error('Remote WebSocket error:', event);
      try {
        if (serverWS.readyState === WebSocket.OPEN) {
          serverWS.close(1011, 'Remote connection error');
        }
      } catch (error) {
        console.error('Error closing client on remote error:', error);
      }
    });
    
    serverWS.addEventListener('error', (event) => {
      console.error('Client WebSocket error:', event);
      try {
        if (remoteWS.readyState === WebSocket.OPEN) {
          remoteWS.close(1011, 'Client connection error');
        }
      } catch (error) {
        console.error('Error closing remote on client error:', error);
      }
    });
    
    // Возвращаем WebSocket клиенту
    return new Response(null, {
      status: 101,
      webSocket: clientWS,
      headers: {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type, Authorization, xi-api-key, Upgrade, Connection, Sec-WebSocket-Key, Sec-WebSocket-Version, Sec-WebSocket-Protocol'
      }
    });
    
  } catch (error) {
    console.error('WebSocket proxy error:', error);
    return new Response(`WebSocket connection failed: ${error.message}`, { 
      status: 500,
      headers: {
        'Access-Control-Allow-Origin': '*',
        'Content-Type': 'text/plain'
      }
    });
  }
}

// Обработка обычных HTTP запросов
async function handleHttpRequest(request, env) {
  const url = new URL(request.url);
  
  // Обработка CORS preflight
  if (request.method === 'OPTIONS') {
    return new Response(null, {
      status: 204,
      headers: {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type, Authorization, xi-api-key, Upgrade, Connection, Sec-WebSocket-Key, Sec-WebSocket-Version, Sec-WebSocket-Protocol',
        'Access-Control-Max-Age': '86400'
      }
    });
  }
  
  // Извлекаем путь после /api/
  const apiPath = url.pathname.replace(/^\/api\//, '');
  
  // Строим целевой URL
  const targetUrl = `https://api.elevenlabs.io/${apiPath}${url.search}`;
  
  console.log('HTTP proxy:', request.url, '->', targetUrl);
  
  // Клонируем заголовки и добавляем API ключ
  const headers = new Headers(request.headers);
  headers.set('xi-api-key', env.ELEVEN_KEY);
  headers.delete('host');
  
  const init = {
    method: request.method,
    headers,
    body: ['GET', 'HEAD'].includes(request.method) ? undefined : request.body,
    redirect: 'manual' // Обрабатываем редиректы вручную
  };
  
  try {
    const response = await fetch(targetUrl, init);
    
    // Обработка редиректов
    if (response.status >= 300 && response.status < 400 && response.headers.has('location')) {
      const location = response.headers.get('location');
      const locationUrl = new URL(location);
      
      // Переписываем редирект обратно на наш прокси
      const newLocation = `/api${locationUrl.pathname}${locationUrl.search}`;
      
      const redirectHeaders = new Headers(response.headers);
      redirectHeaders.set('Location', newLocation);
      addCorsHeaders(redirectHeaders);
      
      return new Response(response.body, {
        status: response.status,
        headers: redirectHeaders
      });
    }
    
    // Обычный ответ
    const responseHeaders = new Headers(response.headers);
    addCorsHeaders(responseHeaders);
    
    return new Response(response.body, {
      status: response.status,
      headers: responseHeaders
    });
    
  } catch (error) {
    console.error('HTTP proxy error:', error);
    return new Response(JSON.stringify({ error: 'Proxy request failed' }), {
      status: 500,
      headers: {
        'Content-Type': 'application/json',
        'Access-Control-Allow-Origin': '*'
      }
    });
  }
}

// Добавление CORS заголовков
function addCorsHeaders(headers) {
  headers.set('Access-Control-Allow-Origin', '*');
  headers.set('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
  headers.set('Access-Control-Allow-Headers', 'Content-Type, Authorization, xi-api-key, Upgrade, Connection, Sec-WebSocket-Key, Sec-WebSocket-Version, Sec-WebSocket-Protocol');
}
