// Cloudflare Pages Function: catch-all proxy to ElevenLabs API
// Route: /api/*
// Required env var: ELEVEN_KEY – your ElevenLabs API key

export async function onRequest(context) {
  const { request, env, params } = context;

  // Handle preflight OPTIONS requests for CORS
  if (request.method === "OPTIONS") {
    return handleCors();
  }

  // Build target URL
  const path = Array.isArray(params.path) ? params.path.join('/') : "";
  const origUrl = new URL(request.url);
  const target = `https://api.elevenlabs.io/${path}${origUrl.search}`;

  // Clone headers & inject key
  const headers = new Headers(request.headers);
  headers.set("xi-api-key", env.ELEVEN_KEY);
  headers.delete("host"); // Let fetch set the correct host

  const init = {
    method: request.method,
    headers,
    body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
    redirect: "follow",
  };

  try {
    const resp = await fetch(target, init);
    
    // Create a new response with CORS headers
    const responseHeaders = new Headers(resp.headers);
    responseHeaders.set("Access-Control-Allow-Origin", "*");
    responseHeaders.set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS");
    responseHeaders.set("Access-Control-Allow-Headers", "Content-Type, Authorization, xi-api-key");
    
    return new Response(resp.body, { 
      status: resp.status, 
      headers: responseHeaders 
    });
  } catch (error) {
    console.error("Proxy error:", error);
    return new Response(JSON.stringify({ error: "Failed to proxy request" }), {
      status: 500,
      headers: {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*"
      }
    });
  }
}

// Helper function for CORS preflight requests
function handleCors() {
  return new Response(null, {
    status: 204,
    headers: {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, Authorization, xi-api-key",
      "Access-Control-Max-Age": "86400"
    }
  });
}
