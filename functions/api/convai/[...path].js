// Cloudflare Pages Function: generic proxy to ElevenLabs Convai API
// Route: /api/convai/*
// It streams the response (audio/SSE) back to the browser and hides the xi-api-key on the edge.
// Required project env var: ELEVEN_KEY

export async function onRequest(context) {
  const { request, env, params } = context;

  // Build target URL: preserve sub-path and query string
  const originalUrl = new URL(request.url);
  const pathSuffix = params.path ? `/${params.path}` : "";
  const target = `https://api.elevenlabs.io${pathSuffix}${originalUrl.search}`;

  // Clone incoming headers, but override Host and inject API key
  const outgoingHeaders = new Headers(request.headers);
  outgoingHeaders.set("Host", "api.elevenlabs.io");
  outgoingHeaders.set("xi-api-key", env.ELEVEN_KEY);
  // Remove headers that might reveal origin or break CORS
  outgoingHeaders.delete("origin");
  outgoingHeaders.delete("referer");

  const fetchInit = {
    method: request.method,
    headers: outgoingHeaders,
    redirect: "follow",
    body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
  };

  // Perform request from Cloudflare edge (IP outside of RU block)
  const resp = await fetch(target, fetchInit);

  // Stream response back to client unchanged
  return new Response(resp.body, {
    status: resp.status,
    headers: resp.headers,
  });
}
