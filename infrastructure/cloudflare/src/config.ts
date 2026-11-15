export type DeploymentEnv = Record<string, string | undefined>;

const required = [
  'DATABASE_URL', 'AGRIX_ACCESS_PASSWORD', 'AGRIX_SESSION_SECRET',
  'AGRIX_INTERNAL_TOKEN', 'AGRIX_R2_BUCKET',
] as const;

const optional = [
  'AGRIX_ALLOWED_PROJECTS', 'AGRIX_ANALYSIS_START', 'AGRIX_ANALYSIS_END',
  'AGRIX_HLS_MAX_SCENES', 'AGRIX_SOILGRIDS_WCS', 'AGRIX_R2_PREFIX',
  'EARTHDATA_USERNAME', 'EARTHDATA_PASSWORD',
  'AGRIX_ASR_MODEL', 'AGRIX_ASR_BACKEND', 'AGRIX_ASR_DTYPE',
  'AGRIX_TTS_MODEL', 'AGRIX_TTS_BACKEND', 'AGRIX_INFERENCE_TIMEOUT',
  'AGRIX_LLM_API_URL', 'AGRIX_LLM_API_KEY', 'AGRIX_LLM_MODEL',
] as const;

export function containerEnvironment(env: DeploymentEnv): Record<string, string> {
  for (const name of required) {
    if (!env[name]?.trim()) throw new Error(`Missing deployment setting: ${name}`);
  }
  if (env.AGRIX_SESSION_SECRET!.length < 32 || env.AGRIX_INTERNAL_TOKEN!.length < 32) {
    throw new Error('Deployment secrets must contain at least 32 characters');
  }
  const database = new URL(env.DATABASE_URL!);
  if (!['postgres:', 'postgresql:', 'postgresql+psycopg:'].includes(database.protocol)
      || !database.hostname || ['localhost', '127.0.0.1', '[::1]'].includes(database.hostname)) {
    throw new Error('A hosted PostgreSQL/PostGIS database is required');
  }
  if (!['require', 'verify-ca', 'verify-full'].includes(database.searchParams.get('sslmode') || '')) {
    throw new Error('Database TLS must be enabled');
  }
  const result: Record<string, string> = {
    AGRIX_DEPLOYMENT: 'cloudflare', AGRIX_ENV: 'production',
    AGRIX_DEFAULT_ANALYSIS_MODE: 'live', AGRIX_PROVIDER_NETWORK: 'true',
    AGRIX_REMOTE_ASSETS: '1', AGRIX_ANALYSIS_WORKERS: '1',
    AGRIX_CACHE_DIRECTORY: '/product/data/cache', HF_HOME: '/product/data/models',
    AGRIX_CACHE_GATEWAY: 'http://ghayth-cache.internal',
    AGRIX_DEVICE: 'cpu', AGRIX_ASR_DTYPE: 'bfloat16',
    OMP_NUM_THREADS: '2', TOKENIZERS_PARALLELISM: 'false', PORT: '8000',
  };
  for (const key of [...required, ...optional]) {
    if (env[key]) result[key] = env[key]!;
  }
  return result;
}

export type InstanceType = 'standard-1' | 'standard-2' | 'standard-3' | 'standard-4';

export function instanceType(env: DeploymentEnv): InstanceType {
  const value = env.GHAYTH_INSTANCE_TYPE || 'standard-3';
  if (!['standard-1', 'standard-2', 'standard-3', 'standard-4'].includes(value)) {
    throw new Error('Unsupported container instance type');
  }
  return value as InstanceType;
}

export function internalPath(path: string): boolean {
  let decoded: string;
  try { decoded = decodeURIComponent(path); } catch { return true; }
  return /^\/_internal(?:\/|$)/.test(decoded);
}

export function forwardedRequest(request: Request): Request {
  const original = new URL(request.url);
  const target = new URL(request.url);
  target.protocol = 'http:';
  target.host = 'container';
  const forwarded = new Request(target, request);
  forwarded.headers.delete('host');
  forwarded.headers.delete('X-Agrix-Internal-Token');
  forwarded.headers.set('X-Forwarded-Proto', 'https');
  forwarded.headers.set('X-Forwarded-Host', original.host);
  return forwarded;
}
