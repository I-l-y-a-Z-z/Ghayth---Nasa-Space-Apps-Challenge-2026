import assert from 'node:assert/strict';
import test from 'node:test';
import {cacheGateway, CACHE_HOST} from '../src/cache.ts';

function fixture() {
  const objects = new Map<string, Uint8Array>();
  const parts = new Map<number, Uint8Array>();
  const events: string[] = [];
  const bucket = {
    async get(key: string) {
      const bytes = objects.get(key);
      return bytes ? {body: new Response(bytes).body, size: bytes.length} : null;
    },
    async put(key: string, body: ReadableStream, options?: {sha256: string}) {
      const bytes = new Uint8Array(await new Response(body).arrayBuffer());
      if (options?.sha256) {
        const hash = Buffer.from(await crypto.subtle.digest('SHA-256', bytes)).toString('hex');
        assert.equal(options.sha256, hash);
      }
      objects.set(key, bytes);
    },
    async createMultipartUpload() { events.push('start'); return {uploadId: 'upload-1'}; },
    resumeMultipartUpload(key: string, id: string) {
      assert.equal(id, 'upload-1');
      return {
        async uploadPart(partNumber: number, body: ReadableStream) {
          events.push(`part-${partNumber}`);
          parts.set(partNumber, new Uint8Array(await new Response(body).arrayBuffer()));
          return {partNumber, etag: `etag-${partNumber}`};
        },
        async complete(uploaded: R2UploadedPart[]) {
          objects.set(key, Buffer.concat(uploaded.map(p => parts.get(p.partNumber)!)));
          events.push('complete');
        },
        async abort() { events.push('abort'); },
      };
    },
  } as unknown as R2Bucket;
  const call = (path: string, init?: RequestInit) => cacheGateway(new Request(`http://${CACHE_HOST}/${path}`, init), bucket, 'ghayth/test');
  return {call, bucket, objects, events};
}

test('the private binding restricts keys and supports a verified cache roundtrip', async () => {
  const f = fixture();
  assert.equal((await f.call('../private')).status, 404);
  assert.equal((await f.call('blobs/not-a-hash')).status, 404);
  assert.equal((await cacheGateway(new Request('https://public.example/index.json'), f.bucket, 'x')).status, 404);
  assert.equal((await f.call('index.json')).status, 404);
  const body = new TextEncoder().encode('scientific asset');
  const hash = Buffer.from(await crypto.subtle.digest('SHA-256', body)).toString('hex');
  assert.equal((await f.call(`blobs/${hash}`, {method: 'PUT', body, headers: {'Content-Length': String(body.length)}})).status, 204);
  assert.equal(await (await f.call(`blobs/${hash}`)).text(), 'scientific asset');
  assert.ok(f.objects.has(`ghayth/test/blobs/${hash}`));
  assert.equal((await f.call('index.json', {method: 'DELETE'})).status, 405);
  assert.equal((await f.call('index.json', {method: 'PUT', body: '{}'})).status, 413);
  assert.equal((await f.call('index.json', {method: 'PUT', body: '{}', headers: {'Content-Length': String(17 * 1024 * 1024)}})).status, 413);
});

test('multipart parts are scoped to one blob, completed in order, or aborted', async () => {
  const f = fixture(), key = 'blobs/' + 'a'.repeat(64);
  const upload = await (await f.call(key, {method: 'POST'})).json() as {upload_id: string};
  const parts = [];
  for (const [index, body] of ['first', 'second'].entries()) {
    parts.push(await (await f.call(`${key}?upload_id=${upload.upload_id}&part_number=${index + 1}`, {
      method: 'PUT', body, headers: {'Content-Length': String(body.length)},
    })).json());
  }
  assert.equal((await f.call(`${key}?upload_id=${upload.upload_id}`, {method: 'POST', body: JSON.stringify(parts)})).status, 204);
  assert.equal(await (await f.call(key)).text(), 'firstsecond');
  await f.call(`${key}?upload_id=${upload.upload_id}`, {method: 'DELETE'});
  assert.deepEqual(f.events, ['start', 'part-1', 'part-2', 'complete', 'abort']);
});
