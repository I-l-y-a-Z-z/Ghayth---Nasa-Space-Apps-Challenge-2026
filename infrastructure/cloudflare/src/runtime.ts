import {containerEnvironment, forwardedRequest, instanceType, internalPath} from './config.ts';
import type {DeploymentEnv, InstanceType} from './config.ts';

export interface ContainerRuntime {
  running: boolean;
  images: Record<string, string>;
  start(options: {image: string; instance: InstanceType; enableInternet: boolean; env: Record<string, string>}): void;
  setInactivityTimeout(milliseconds: number): Promise<void>;
  getTcpPort(port: number): {fetch(request: Request | string, init?: RequestInit): Promise<Response>};
}

export interface RuntimeContext {
  container: ContainerRuntime;
  storage: {setAlarm(time: number): Promise<void>; deleteAlarm(): Promise<void>};
  prepareCache(): Promise<void>;
}

const IDLE_TIMEOUT = 15 * 60 * 1000;
const ACTIVE_POLL = 30_000;

export class ContainerSupervisor {
  private starting: Promise<void> | undefined;
  private ready = false;
  private ctx: RuntimeContext;
  private env: DeploymentEnv;
  private wait: (ms: number) => Promise<void>;

  constructor(
    ctx: RuntimeContext,
    env: DeploymentEnv,
    wait = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms)),
  ) {
    this.ctx = ctx;
    this.env = env;
    this.wait = wait;
  }

  private async ensureRunning(): Promise<void> {
    const container = this.ctx.container;
    if (this.ready && container.running) {
      await container.setInactivityTimeout(IDLE_TIMEOUT);
      return;
    }
    this.starting ??= this.startAndWait().finally(() => { this.starting = undefined; });
    await this.starting;
  }

  private async startAndWait(): Promise<void> {
    const container = this.ctx.container;
    this.ready = false;
    await this.ctx.prepareCache();
    if (!container.running) {
      container.start({
        image: container.images.base,
        instance: instanceType(this.env),
        enableInternet: true,
        env: containerEnvironment(this.env),
      });
    }
    await container.setInactivityTimeout(IDLE_TIMEOUT);
    const port = container.getTcpPort(8000);
    for (let attempt = 0; attempt < 120; attempt++) {
      try {
        const response = await port.fetch('http://container/health/ready', {
          signal: AbortSignal.timeout(2000),
        });
        await response.body?.cancel();
        if (response.ok) { this.ready = true; return; }
      } catch { /* Startup and database wake-up can take time. */ }
      await this.wait(500);
    }
    throw new Error('The container did not become ready');
  }

  async fetch(request: Request): Promise<Response> {
    if (internalPath(new URL(request.url).pathname)) return new Response('Not found', {status: 404});
    try {
      await this.ensureRunning();
      // Durable alarms keep acquisition/inference alive after the browser closes.
      await this.ctx.storage.setAlarm(Date.now() + ACTIVE_POLL);
      return await this.ctx.container.getTcpPort(8000).fetch(forwardedRequest(request));
    } catch {
      this.ready = false;
      return Response.json({error: {code: 'SERVICE_STARTING', message: 'The service is starting. Please retry shortly.'}}, {
        status: 503, headers: {'Retry-After': '30', 'Cache-Control': 'no-store'},
      });
    }
  }

  async alarm(): Promise<void> {
    try {
      // If a process stopped during a job, starting it resumes persisted work.
      await this.ensureRunning();
      const response = await this.ctx.container.getTcpPort(8000).fetch('http://container/_internal/runtime', {
        headers: {'X-Agrix-Internal-Token': this.env.AGRIX_INTERNAL_TOKEN!},
        signal: AbortSignal.timeout(5000),
      });
      if (!response.ok) throw new Error('Runtime probe failed');
      const state = await response.json() as {busy?: boolean};
      if (state.busy || this.env.GHAYTH_KEEP_WARM === 'true') {
        await this.ctx.container.setInactivityTimeout(IDLE_TIMEOUT);
        await this.ctx.storage.setAlarm(Date.now() + (state.busy ? ACTIVE_POLL : 5 * 60 * 1000));
      } else {
        await this.ctx.storage.deleteAlarm();
      }
    } catch {
      this.ready = false;
      await this.ctx.storage.setAlarm(Date.now() + 5 * 60 * 1000);
    }
  }
}
