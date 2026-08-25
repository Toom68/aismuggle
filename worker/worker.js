// Cloudflare Worker — proxies requests to the Render backend.
//
// This hides the onrender.com SNI from DPI. The school network sees a
// connection to *.workers.dev (Cloudflare), which is used by millions of
// legitimate sites and is almost never blocked.
//
// Deploy:
//   1. Go to https://dash.cloudflare.com -> Workers & Pages -> Create
//   2. Name it whatever (e.g. "search-proxy")
//   3. Paste this code
//   4. Set the BACKEND_URL environment variable to your Render URL:
//      https://quicksearch-bw7h.onrender.com
//   5. Deploy
//   6. Note the Worker URL: https://search-proxy.<your-subdomain>.workers.dev
//   7. On the client, set:
//      export AISEARCH_URL=https://search-proxy.<your-subdomain>.workers.dev

const DEFAULT_BACKEND = "https://quicksearch-bw7h.onrender.com";

export default {
  async fetch(request, env) {
    const backend = env.BACKEND_URL || DEFAULT_BACKEND;
    const url = new URL(request.url);

    // Build the backend URL with the same path and query params.
    const backendUrl = new URL(backend);
    backendUrl.pathname = url.pathname;
    backendUrl.search = url.search;

    // Forward the request to the Render backend.
    // We use a simple fetch — Cloudflare handles TLS to the backend.
    const backendReq = new Request(backendUrl.toString(), {
      method: request.method,
      headers: request.headers,
      body: request.method === "POST" ? request.body : null,
      redirect: "follow",
    });

    // Clean up headers that might cause issues.
    backendReq.headers.delete("host");
    backendReq.headers.delete("cf-connecting-ip");
    backendReq.headers.delete("cf-ipcountry");
    backendReq.headers.delete("cf-ray");
    backendReq.headers.delete("cf-visitor");

    const resp = await fetch(backendReq);

    // Return the response, preserving streaming.
    // Strip headers that might cause issues.
    const newHeaders = new Headers(resp.headers);
    newHeaders.delete("x-render-origin-server");
    newHeaders.set("x-proxied-by", "cloudflare-worker");

    return new Response(resp.body, {
      status: resp.status,
      statusText: resp.statusText,
      headers: newHeaders,
    });
  },
};
