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
    redirect: "manual", // IMPORTANT: We handle redirects manually
  };

  try {
    const resp = await fetch(target, init);

    // Check if the response is a redirect (301, 302, 307, 308)
    if (resp.status >= 300 && resp.status < 400 && resp.headers.has("location")) {
      const location = resp.headers.get("location");
      const locationUrl = new URL(location);

      // Rewrite the redirect URL to point back to our proxy.
      // e.g., https://api.us.elevenlabs.io/v1/... -> /api/v1/...
      const newLocation = `/api${locationUrl.pathname}${locationUrl.search}`;

      const redirectHeaders = new Headers(resp.headers);
      redirectHeaders.set("Location", newLocation);
      addCorsHeaders(redirectHeaders);

      return new Response(resp.body, {
        status: resp.status,
        headers: redirectHeaders,
      });
    }
    
    // If not a redirect, handle as a normal response
    const responseHeaders = new Headers(resp.headers);
    addCorsHeaders(responseHeaders);
    
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

// Helper to add standard CORS headers
function addCorsHeaders(headers) {
  headers.set("Access-Control-Allow-Origin", "*");
  headers.set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS");
  headers.set("Access-Control-Allow-Headers", "Content-Type, Authorization, xi-api-key");
}

// Helper function for CORS preflight requests
function handleCors() {
  const headers = new Headers();
  addCorsHeaders(headers);
  headers.set("Access-Control-Max-Age", "86400");
  return new Response(null, {
    status: 204,
    headers: headers
  });
}
