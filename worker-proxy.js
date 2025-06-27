// Cloudflare Worker для полного прокси ElevenLabs API (включая WebSocket)
// Поддерживает как HTTP/HTTPS запросы, так и WebSocket соединения

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

// Обработка WebSocket соединений
async function handleWebSocket(request, env) {
  const url = new URL(request.url);
  
  // Извлекаем путь после /api/
  const apiPath = url.pathname.replace(/^\/api\//, '');
  
  // Строим целевой WebSocket URL
  const targetUrl = `wss://api.elevenlabs.io/${apiPath}${url.search}`;
  
  console.log('WebSocket proxy:', request.url, '->', targetUrl);
  
  // Создаем WebSocket пару
  const webSocketPair = new WebSocketPair();
  const [client, server] = Object.values(webSocketPair);
  
  // Подключаемся к целевому WebSocket серверу
  const headers = new Headers(request.headers);
  headers.set('xi-api-key', env.ELEVEN_KEY);
  headers.delete('host');
  headers.delete('origin'); // Убираем origin для избежания CORS
  
  try {
    // Устанавливаем соединение с ElevenLabs
    const targetSocket = new WebSocket(targetUrl, {
      headers: Object.fromEntries(headers.entries())
    });
    
    // Проксируем сообщения от клиента к серверу
    server.addEventListener('message', event => {
      if (targetSocket.readyState === WebSocket.OPEN) {
        targetSocket.send(event.data);
      }
    });
    
    // Проксируем сообщения от сервера к клиенту
    targetSocket.addEventListener('message', event => {
      if (server.readyState === WebSocket.OPEN) {
        server.send(event.data);
      }
    });
    
    // Обработка закрытия соединений
    server.addEventListener('close', () => {
      if (targetSocket.readyState === WebSocket.OPEN) {
        targetSocket.close();
      }
    });
    
    targetSocket.addEventListener('close', () => {
      if (server.readyState === WebSocket.OPEN) {
        server.close();
      }
    });
    
    // Обработка ошибок
    server.addEventListener('error', event => {
      console.error('Client WebSocket error:', event);
      if (targetSocket.readyState === WebSocket.OPEN) {
        targetSocket.close();
      }
    });
    
    targetSocket.addEventListener('error', event => {
      console.error('Target WebSocket error:', event);
      if (server.readyState === WebSocket.OPEN) {
        server.close();
      }
    });
    
    // Принимаем WebSocket соединение
    server.accept();
    
    return new Response(null, {
      status: 101,
      webSocket: client,
      headers: {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type, Authorization, xi-api-key, Upgrade, Connection, Sec-WebSocket-Key, Sec-WebSocket-Version, Sec-WebSocket-Protocol'
      }
    });
    
  } catch (error) {
    console.error('WebSocket proxy error:', error);
    return new Response('WebSocket connection failed', { 
      status: 500,
      headers: {
        'Access-Control-Allow-Origin': '*'
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
