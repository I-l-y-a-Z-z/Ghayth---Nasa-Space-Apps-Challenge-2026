// Static hosting continues to work when the free backend has no allowance left.
const api = /^\/(?:workspace|plots|analyses|rotations|evaluations|plans|chat|catalog|client-config|session|speech|records)(?:\/|$)/;
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname.startsWith('/_internal')) return new Response('Not found', {status: 404});
    if (url.pathname === '/') return Response.redirect(url.origin + '/app/', 302);
    if (url.pathname.startsWith('/health')) return Response.json({status: 'ok', mode: env.GHAYTH_BACKEND_ORIGIN ? 'backend-with-demo-fallback' : 'demo'});
    if (api.test(url.pathname)) {
      if (env.GHAYTH_BACKEND_ORIGIN) {
        const target = new URL(url.pathname + url.search, env.GHAYTH_BACKEND_ORIGIN);
        const headers = new Headers(request.headers);
        headers.delete('host'); headers.delete('X-Agrix-Internal-Token');
        headers.set('X-Forwarded-Proto', 'https'); headers.set('X-Forwarded-Host', url.host);
        try {
          const response = await fetch(new Request(target, request), {headers, redirect: 'manual', signal: AbortSignal.timeout(20000)});
          if (![402, 502, 503, 504, 530].includes(response.status)) return response;
          await response.body?.cancel();
        } catch { /* Quota limits and cold-start failures use public demo fixtures. */ }
      }
      return Response.json({error: {code: 'DEMO_FALLBACK', message: 'Prepared demonstration examples are available.'}}, {
        status: 503, headers: {'Cache-Control': 'no-store'},
      });
    }
    return env.ASSETS.fetch(request);
  },
};
