/**
 * BMS Proxy — Cloudflare Worker
 *
 * Fetches BookMyShow pages on behalf of GitHub Actions.
 * Runs on Cloudflare's own edge network, so BMS's WAF doesn't block it.
 *
 * Deploy:
 *   1. Go to https://dash.cloudflare.com → Workers & Pages → Create Worker
 *   2. Paste this entire file into the editor
 *   3. Click "Deploy"
 *   4. Copy the Worker URL (e.g. https://bms-proxy.YOUR-SUBDOMAIN.workers.dev)
 *   5. Add that URL as the BMS_WORKER_URL secret in your GitHub repo
 *
 * Usage:
 *   GET https://bms-proxy.YOUR-SUBDOMAIN.workers.dev/?url=https://in.bookmyshow.com/bengaluru/cinemas
 *
 * Security: set a secret token so only your tracker can use it.
 *   Add an env var SECRET_TOKEN in the Worker settings.
 *   Pass it as ?token=YOUR_TOKEN in the request.
 *   If you skip this the Worker is open — fine for a personal tracker.
 */

export default {
  async fetch(request, env) {
    const incoming = new URL(request.url);

    // ── Optional auth token ──────────────────────────────────────────────────
    const secret = env.SECRET_TOKEN;
    if (secret) {
      const provided = incoming.searchParams.get("token");
      if (provided !== secret) {
        return new Response("Unauthorized", { status: 401 });
      }
    }

    // ── Target URL ──────────────────────────────────────────────────────────
    const target = incoming.searchParams.get("url");
    if (!target) {
      return new Response('Missing ?url= parameter', { status: 400 });
    }

    // Only allow in.bookmyshow.com to prevent misuse
    let targetUrl;
    try {
      targetUrl = new URL(target);
    } catch {
      return new Response("Invalid URL", { status: 400 });
    }
    if (!targetUrl.hostname.endsWith("bookmyshow.com")) {
      return new Response("Only bookmyshow.com URLs allowed", { status: 403 });
    }

    // ── Fetch BMS ────────────────────────────────────────────────────────────
    try {
      const resp = await fetch(target, {
        headers: {
          "User-Agent":
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
            "AppleWebKit/537.36 (KHTML, like Gecko) " +
            "Chrome/131.0.0.0 Safari/537.36",
          "Accept":
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
          "Accept-Language": "en-IN,en;q=0.9",
          "DNT": "1",
          "Upgrade-Insecure-Requests": "1",
        },
        redirect: "follow",
      });

      const body = await resp.text();

      return new Response(body, {
        status: resp.status,
        headers: {
          "Content-Type": resp.headers.get("Content-Type") || "text/html",
          "X-Proxied-Status": String(resp.status),
        },
      });
    } catch (err) {
      return new Response(`Fetch error: ${err.message}`, { status: 502 });
    }
  },
};
