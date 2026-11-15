import {DurableObject, WorkerEntrypoint} from 'cloudflare:workers';
import {cacheGateway, CACHE_HOST} from './cache.ts';
import {ContainerSupervisor} from './runtime.ts';
import type {DeploymentEnv} from './config.ts';

declare global {
  namespace Cloudflare {
    interface GlobalProps { mainModule: typeof import('./index.ts'); }
  }
}

interface Env {
  GHAYTH: DurableObjectNamespace<Ghayth>;
  CACHE_BUCKET: R2Bucket;
  AGRIX_R2_PREFIX: string;
  [name: string]: unknown;
}

export class Ghayth extends DurableObject<Env> {
  private supervisor: ContainerSupervisor;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.supervisor = new ContainerSupervisor({
      container: ctx.container!,
      storage: ctx.storage,
      prepareCache: () => ctx.container!.interceptOutboundHttp(CACHE_HOST, ctx.exports.CacheGateway as Fetcher),
    }, env as DeploymentEnv);
  }

  fetch(request: Request) { return this.supervisor.fetch(request); }
  alarm() { return this.supervisor.alarm(); }
}

export class CacheGateway extends WorkerEntrypoint<Env> {
  fetch(request: Request) {
    return cacheGateway(request, this.env.CACHE_BUCKET, this.env.AGRIX_R2_PREFIX);
  }
}

export default {
  // One stable identity: the backend's executor and cache have one writer.
  fetch(request: Request, env: Env): Promise<Response> {
    return env.GHAYTH.getByName('ghayth-main').fetch(request);
  },
} satisfies ExportedHandler<Env>;
