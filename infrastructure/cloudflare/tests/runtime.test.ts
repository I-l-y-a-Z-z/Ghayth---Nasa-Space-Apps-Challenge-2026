import assert from 'node:assert/strict';
import test from 'node:test';
import {containerEnvironment, forwardedRequest, internalPath} from '../src/config.ts';
import {ContainerSupervisor} from '../src/runtime.ts';
import type {ContainerRuntime} from '../src/runtime.ts';

const environment = () => ({
  DATABASE_URL: 'postgresql://user:secret@db.example/ghayth?sslmode=require',
  AGRIX_ACCESS_PASSWORD: 'workspace-password',
  AGRIX_SESSION_SECRET: 's'.repeat(32), AGRIX_INTERNAL_TOKEN: 'i'.repeat(32),
  AGRIX_R2_BUCKET: 'ghayth-scientific',
  AGRIX_R2_ENDPOINT: 'https://account.r2.cloudflarestorage.com',
  AGRIX_R2_ACCESS_KEY_ID: 'access', AGRIX_R2_SECRET_ACCESS_KEY: 'secret',
});

test('only explicitly selected secrets/config reach the Python container', () => {
  const result = containerEnvironment({...environment(), CLOUDFLARE_API_TOKEN: 'never-forward'});
  assert.equal(result.AGRIX_ENV, 'production');
  assert.equal(result.AGRIX_ANALYSIS_WORKERS, '1');
  assert.equal(result.AGRIX_CACHE_GATEWAY, 'http://ghayth-cache.internal');
  assert.equal(result.AGRIX_R2_SECRET_ACCESS_KEY, undefined);
  assert.equal(result.CLOUDFLARE_API_TOKEN, undefined);
  assert.throws(() => containerEnvironment({...environment(), DATABASE_URL: 'sqlite:///local.db'}));
  assert.throws(() => containerEnvironment({...environment(), DATABASE_URL: 'postgresql://u:p@db.example/a'}));
  assert.throws(() => containerEnvironment({...environment(), AGRIX_SESSION_SECRET: 'short'}));
});

test('forwarding preserves API body/cookies and prevents internal-token spoofing', async () => {
  const request = new Request('https://ghayth.example/rotations/suggest?test=1', {
    method: 'POST', body: '{"horizon_days":365}',
    headers: {'Content-Type': 'application/json', Cookie: 'agrix_session=signed', 'X-Agrix-Internal-Token': 'spoof'},
  });
  const result = forwardedRequest(request);
  assert.equal(result.url, 'http://container/rotations/suggest?test=1');
  assert.equal(result.headers.get('Cookie'), 'agrix_session=signed');
  assert.equal(result.headers.get('X-Agrix-Internal-Token'), null);
  assert.equal(result.headers.get('X-Forwarded-Host'), 'ghayth.example');
  assert.equal(await result.text(), '{"horizon_days":365}');
  assert.ok(internalPath('/_internal/runtime'));
  assert.ok(internalPath('/%5finternal%2Fruntime'));
  assert.ok(internalPath('/%broken'));
  assert.ok(!internalPath('/rotations/suggest'));
});

function fixture() {
  let starts = 0, probes = 0, busy = false, alarm = 0, deleted = 0, cachePrepared = false;
  let failures = 0;
  const requests: Request[] = [];
  const container: ContainerRuntime = {
    running: false, images: {base: 'registry/image@sha256:verified'},
    start(options) { starts++; assert.ok(cachePrepared); assert.equal(options.enableInternet, true); this.running = true; },
    async setInactivityTimeout(ms) { assert.equal(ms, 15 * 60 * 1000); },
    getTcpPort(port) {
      assert.equal(port, 8000);
      return {async fetch(input, init) {
        const request = typeof input === 'string' ? new Request(input, init) : input;
        if (request.url.endsWith('/health/ready')) {
          probes++;
          if (failures-- > 0) throw new Error('Not ready');
          return new Response('{"status":"ready"}');
        }
        if (request.url.endsWith('/_internal/runtime')) {
          assert.equal(request.headers.get('X-Agrix-Internal-Token'), environment().AGRIX_INTERNAL_TOKEN);
          return Response.json({busy});
        }
        requests.push(request);
        return new Response('app-response', {headers: {'Set-Cookie': 'agrix_session=signed; Secure'}});
      }};
    },
  };
  const supervisor = new ContainerSupervisor({container, prepareCache: async () => { cachePrepared = true; }, storage: {
    async setAlarm(time) { alarm = time; }, async deleteAlarm() { deleted++; },
  }}, environment(), async () => {});
  return {
    supervisor, container, requests,
    state: () => ({starts, probes, alarm, deleted}),
    setBusy: (value: boolean) => { busy = value; },
    failProbes: (value: number) => { failures = value; },
  };
}

test('concurrent cold requests share startup and preserve backend cookies', async () => {
  const f = fixture();
  const responses = await Promise.all([f.supervisor.fetch(new Request('https://site.example/')),
    f.supervisor.fetch(new Request('https://site.example/app/assets/index.js'))]);
  assert.equal(f.state().starts, 1);
  assert.equal(f.state().probes, 1);
  assert.equal(f.requests.length, 2);
  assert.match(responses[0].headers.get('Set-Cookie')!, /Secure/);
});

test('an active background job renews its alarm; idle instances can sleep', async () => {
  const f = fixture();
  await f.supervisor.fetch(new Request('https://site.example/'));
  f.setBusy(true);
  await f.supervisor.alarm();
  assert.ok(f.state().alarm > Date.now());
  assert.equal(f.state().deleted, 0);
  f.setBusy(false);
  await f.supervisor.alarm();
  assert.equal(f.state().deleted, 1);
  f.container.running = false;
  await f.supervisor.alarm();
  assert.equal(f.state().starts, 2);
});

test('readiness retries are limited and never replay an application POST', async () => {
  const f = fixture();
  f.failProbes(2);
  const result = await f.supervisor.fetch(new Request('https://site.example/plots', {method: 'POST', body: '{}'}));
  assert.equal(result.status, 200);
  assert.equal(f.state().probes, 3);
  assert.equal(f.requests.length, 1);
  const blocked = await f.supervisor.fetch(new Request('https://site.example/_internal/runtime'));
  assert.equal(blocked.status, 404);
  assert.equal(f.requests.length, 1);
});
