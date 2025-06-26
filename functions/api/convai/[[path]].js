// Cloudflare Pages Function: catch-all proxy to ElevenLabs Convai API
// Route: /api/convai/* (the part after /api/convai is captured in param `path`)
// Required env var: ELEVEN_KEY – your ElevenLabs API key
// Docs: https://developers.cloudflare.com/pages/functions/

export async function onRequest(context) {
  const { request, env, params } = context;

  // Build target URL – keep original query string
  const suffix = params.path ? `/${params.path}` : "";
  const origUrl = new URL(request.url);
  const target = `https://api.elevenlabs.io${suffix}${origUrl.search}`;

  // Clone headers & inject key
  const headers = new Headers(request.headers);
  headers.set("xi-api-key", env.ELEVEN_KEY);
  headers.delete("host");


  const init = {
    method: request.method,
    headers,
    body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
    redirect: "follow",
  };

  const resp = await fetch(target, init);
  return new Response(resp.body, { status: resp.status, headers: resp.headers });
}
