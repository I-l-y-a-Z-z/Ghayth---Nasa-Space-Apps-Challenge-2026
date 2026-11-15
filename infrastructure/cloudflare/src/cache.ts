// Only reachable through the container's private outbound binding.
export const CACHE_HOST = 'ghayth-cache.internal';

export async function cacheGateway(request: Request, bucket: R2Bucket, prefix: string): Promise<Response> {
  const url = new URL(request.url);
  const relative = url.pathname.slice(1);
  if (url.hostname !== CACHE_HOST || !/^(index\.json|blobs\/[a-f0-9]{64})$/.test(relative)
      || !prefix || prefix.split('/').includes('..')) return new Response(null, {status: 404});
  const key = `${prefix.replace(/\/$/, '')}/${relative}`;
  const uploadId = url.searchParams.get('upload_id');
  if (request.method === 'POST' && relative.startsWith('blobs/')) {
    if (!uploadId) {
      const upload = await bucket.createMultipartUpload(key);
      return Response.json({upload_id: upload.uploadId});
    }
    const parts = await request.json() as R2UploadedPart[];
    if (!Array.isArray(parts) || !parts.length || parts.length > 10_000) return new Response(null, {status: 400});
    await bucket.resumeMultipartUpload(key, uploadId).complete(parts);
    return new Response(null, {status: 204});
  }
  if (request.method === 'DELETE' && uploadId && relative.startsWith('blobs/')) {
    await bucket.resumeMultipartUpload(key, uploadId).abort();
    return new Response(null, {status: 204});
  }
  if (request.method === 'PUT') {
    const length = Number(request.headers.get('Content-Length'));
    if (!request.headers.has('Content-Length') || !Number.isSafeInteger(length) || length < 0
        || length > (relative === 'index.json' ? 16 * 1024 * 1024 : 64 * 1024 * 1024)) {
      return new Response(null, {status: 413});
    }
    if (uploadId && relative.startsWith('blobs/')) {
      const part = Number(url.searchParams.get('part_number'));
      if (!Number.isInteger(part) || part < 1 || part > 10_000) return new Response(null, {status: 400});
      return Response.json(await bucket.resumeMultipartUpload(key, uploadId).uploadPart(part, request.body!));
    }
    if (url.search) return new Response(null, {status: 400});
    await bucket.put(key, request.body, relative.startsWith('blobs/') ? {sha256: relative.slice(6)} : undefined);
    return new Response(null, {status: 204});
  }
  if (request.method === 'GET' && !url.search) {
    const object = await bucket.get(key);
    if (!object) return new Response(null, {status: 404});
    return new Response(object.body, {headers: {'Content-Length': String(object.size), 'Cache-Control': 'no-store'}});
  }
  return new Response(null, {status: 405});
}
