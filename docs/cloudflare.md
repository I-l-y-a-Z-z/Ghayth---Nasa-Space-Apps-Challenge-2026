# Cloudflare deployment for a multi-month demo

**This deployment requires Cloudflare Workers Paid.** Enabling R2 alone does not enable Containers. The connected account currently has R2 enabled and the private `ghayth-scientific` bucket created, but Containers rejected access because Workers Paid is not active. No paid plan has been enabled. For a zero-payment deployment, the Python backend needs another host; a static Cloudflare deployment alone cannot run it or the speech models.

Ghayth deploys as one Cloudflare Worker routing to one Durable Object-managed Linux container. The container serves both the React build and FastAPI over one HTTPS origin, including `/app/`, API routes, sessions and voice. A hosted PostgreSQL/PostGIS database owns farm records. Private R2 objects preserve the scientific cache across container replacement.

This configuration does not depend on your laptop staying online or a temporary tunnel. The Worker URL remains deployed when an idle container sleeps; incoming requests start it again. Cloudflare, database and storage accounts must remain active for the intended months.

## What is included

- [wrangler.jsonc](../infrastructure/cloudflare/wrangler.jsonc): image build, Durable Object binding, instance and public deployment variables.
- A stable `ghayth-main` container identity and one Python analysis worker. Do not add round-robin instances: the current cache and executor require one writer.
- Runtime secret forwarding by explicit allowlist, production session cookies, private runtime probes and same-origin microphone permissions.
- A private outbound R2 binding at `http://ghayth-cache.internal`, available only inside the container. No R2 API keys or public cache endpoint are needed for the Cloudflare deployment. Multipart uploads use 32 MiB parts to stay below the container outbound request limit.
- Automatic PostgreSQL URL normalization and Alembic migrations before serving requests. Cloud deployment refuses SQLite, local database sockets, missing database TLS and missing persistence/session settings.
- R2 restoration before pending jobs resume. Immutable checksum-addressed blobs upload first; a checkpoint index commits last. A terminal successful/partial analysis is saved only after its scientific files have been checkpointed.
- Durable alarms while background acquisition or speech inference is active. Closing the browser does not intentionally put an active job to sleep. Persisted pending/running analyses resume after a process restart.
- Optional CPU speech dependencies in the image, using the existing MoulSot/Kandir models. The checked-in Cloudflare image enables them.
- A manually triggered [GitHub workflow](../.github/workflows/cloudflare.yml) that can build/validate without publishing; deployment is an explicit input.

## 1. Create the hosted database

The documented database choice is [Neon](https://neon.com/). Create a project for Ghayth, choose a region near the expected audience, and use a database owner connection. Keep a separate database/project for real records and tests.

Use the pinned Neon CLI in `infrastructure/cloudflare` with an existing authenticated profile, or a connected Neon MCP integration. If authentication needs refreshing, run `npx neon login` there interactively with Node 24; unattended setup does not launch a browser. A database can also be created in the Neon console. Do not paste database passwords into chat or commit connection strings.

Preserve the root development `.env`. Any project link/checkout must use `--no-env-pull`; write hosted connection details to an explicitly chosen ignored file or directly to Worker secrets. Do not use `neon init` over an existing configured app simply to add tooling. Codex skills and the URL-only OAuth MCP integration can be installed globally without rewriting the app configuration.

PostGIS is supported by Neon. In its SQL editor, verify:

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
SELECT PostGIS_Full_Version();
```

The application's initial migration also creates the extension. The migration role must be allowed to do so. Copy the **direct, non-pooled** connection string, retaining `sslmode=require` or stronger. For example:

```text
postgresql://OWNER:PASSWORD@YOUR_NEON_HOST/ghayth?sslmode=require
```

This minimal single-container setup uses one direct connection for both startup migrations and application traffic. Neon generally recommends pooled application connections; use a separately configured direct migration connection if adding pooling later. The container entry point converts plain `postgresql://` or `postgres://` into the installed psycopg driver's URL without exposing the password. Runtime records survive container deletion because they are in this external database.

A new database starts empty. Deployment does not automatically upload the local database, replace saved plans or seed fake farm records. To migrate existing records, take a PostgreSQL backup and deliberately restore it into the chosen destination before directing users there. Do not point verification tests at it.

Check the selected Neon plan's storage, compute, backup and availability limits for the expected traffic. Scale-to-zero introduces wake-up latency; it is separate from database data persistence.

References: [Neon PostGIS](https://neon.com/docs/extensions/postgis), [Neon scale-to-zero](https://neon.com/docs/introduction/scale-to-zero).

## 2. Install the deployment tools

Use Node 24, npm and a working Docker-compatible engine. From the repository root:

```bash
npm ci
npm run cloudflare:install
npm run cloudflare:check
```

The infrastructure package has its own committed lockfile. Wrangler and Cloudflare runtime types are pinned there. Python development tests also require the backend and speech base dependencies:

```bash
.venv/bin/python -m pip install -e './apps/api[dev,earthdata,cloudflare]'
.venv/bin/python -m pip install -r services/speech/requirements.txt
```

The hosted CPU speech dependencies are installed during the Docker build; local unit tests inject test models and do not download weights.

## 3. Create the private R2 bucket

Authenticate with the intended Cloudflare account, then create the bucket matching `AGRIX_R2_BUCKET` in `wrangler.jsonc`:

```bash
cd infrastructure/cloudflare
npx wrangler login
npx wrangler whoami
npx wrangler r2 bucket create ghayth-scientific
```

The Worker binds this bucket as `CACHE_BUCKET`. Before starting the container, it intercepts the virtual hostname `ghayth-cache.internal` and routes it to the `CacheGateway` entrypoint. That entrypoint accepts only the checkpoint index and content-addressed blobs under the configured scientific prefix. It is not exposed by the public Worker router.

The Python cache client streams downloads and uses multipart uploads for large blobs. Failed multipart transfers abort without publishing a new checkpoint index. The private binding replaces S3 credentials for this deployment.

For an external host or a local cache seed, the existing S3 adapter remains available. Create an R2 S3 API access key with Object Read & Write access limited to this bucket, and use this endpoint:

```text
https://YOUR_ACCOUNT_ID.r2.cloudflarestorage.com
```

A general Cloudflare API token is not an R2 S3 secret key. Keep the bucket private. Do not configure expiry on active checkpoint indices or referenced blobs. Use different prefixes/buckets for different deployments; two live backends must not write the same checkpoint namespace.

The default scientific prefix is `ghayth/production/scientific`. R2 stores scientific inputs/processed data, not the farm database, recordings or Hugging Face weights. The cache writes to normal local POSIX files and checkpoints to R2; it does not depend on FUSE rename semantics.

References: [private container bindings](https://developers.cloudflare.com/containers/configuration/workers-connections/), [outbound request limits](https://developers.cloudflare.com/containers/configuration/outbound-traffic/), [R2 S3 credentials](https://developers.cloudflare.com/r2/api/tokens/).

### Optional: seed existing scientific data

To reuse a prepared local scientific cache, export the scoped `AGRIX_R2_*` values locally, using exactly the bucket and prefix configured above, then run from the repository root:

```bash
.venv/bin/python scripts/cloudflare-cache.py seed --directory data/cache
```

The command refuses to overwrite an existing nonempty checkpoint. It transfers scientific files only, not farm records, credentials or model weights. Restoration to a separate directory is available through `restore --directory /tmp/ghayth-restored-cache`.

## 4. Store production secrets

Run these commands from `infrastructure/cloudflare`. Each command prompts locally for its value:

```bash
npx wrangler secret put DATABASE_URL
npx wrangler secret put AGRIX_ACCESS_PASSWORD
npx wrangler secret put AGRIX_SESSION_SECRET
npx wrangler secret put AGRIX_INTERNAL_TOKEN
```

Use separate random values of at least 32 characters for the session secret and internal token. Keep the session secret stable between deployments so sessions remain valid. The workspace password controls access to the shared farmer workspace; it is not a full multi-user identity/roles service.

Optional protected NASA transfers can also use:

```bash
npx wrangler secret put EARTHDATA_USERNAME
npx wrangler secret put EARTHDATA_PASSWORD
```

Public POWER and public mirror acquisition do not require Earthdata credentials. Credentials are never sent to the browser.

For local Wrangler/container smoke tests, copy [.dev.vars.example](../infrastructure/cloudflare/.dev.vars.example) to `.dev.vars` and populate it locally. That populated file is excluded from Git and Docker contexts. Production values live in Worker secrets, not in `wrangler.jsonc` or the root development `.env`.

## 5. Validate and publish

From the repository root:

```bash
npm run check
npm run cloudflare:check
.venv/bin/python -m pytest -q
npm run cloudflare:dry-run
```

Wrangler's dry run also needs Docker because it builds the configured image. It does not publish the Worker. For an explicit local image build:

```bash
docker build --build-arg INSTALL_SPEECH=1 -t ghayth:cloudflare .
```

Once the database, bucket, secrets and image are ready:

```bash
npm run cloudflare:deploy
```

Wrangler builds/pushes the image and prints the stable `workers.dev` HTTPS address. A custom hostname can be attached later through Cloudflare; the app keeps relative same-origin URLs.

On a first request, the Worker waits for `/health/ready`, which is only served after migrations and cache restoration. An unsuccessful cold start returns a retryable 503. The proxy retries readiness probes, not application POSTs.

### If Docker is unavailable locally

Use the manually triggered `Cloudflare build and optional deploy` workflow in GitHub Actions after pushing the reviewed source. Its hosted runner builds the image, runs tests and validates packaging. Leave `deploy` false for validation only.

To publish through that workflow, first populate the Worker secrets above and add `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` as GitHub repository secrets. Use an account-scoped deployment token with the permissions required by [Cloudflare Containers deployment](https://developers.cloudflare.com/containers/guides/deploy-containers/). Trigger the workflow with deployment selected. No push-triggered or automatic publication is configured.

The CI build path is provided for a Docker-capable runner. A local Worker type/unit check does not prove that an image builds or that remote services are configured.

## 6. Check the hosted application

At the deployed HTTPS address:

1. Check `/health` and `/health/ready`.
2. Log in with the workspace password.
3. Draw a supported field, confirm it and verify live acquisition progresses; partial layers remain allowed.
4. Generate suggestions, change duration and check automatic reload, open a plan and save it.
5. Close/reopen the app and confirm the saved plan persists.
6. Check `/speech/status`, then review a microphone transcript and try synthesis.
7. After an intentional restart, confirm the saved plan remains and scientific assets restore from R2. A pending analysis should resume.

`/_internal/runtime` is blocked at the public Worker. The private probe requires a separate secret and only reports whether analysis/inference work is active.

## 7. Lifecycle, performance and costs

`GHAYTH_KEEP_WARM=false` lets idle containers stop after 15 minutes. The public deployment remains available and starts a container on demand. During acquisition or inference, durable alarms probe every 30 seconds to keep the process active. A failed probe schedules recovery after five minutes.

To reduce cold starts during demonstrations, set `GHAYTH_KEEP_WARM` to `true` in `wrangler.jsonc` and redeploy. That keeps an allocated container warm with five-minute alarms and increases costs. Return it to false when that is no longer needed.

The default `standard-3` instance has 2 vCPUs, 8 GiB memory and 16 GB ephemeral disk. CPU speech is enabled through `INSTALL_SPEECH=1` in the image's `build_vars`; models download lazily to the ignored local Hugging Face cache. They may download again after container replacement. Scientific data and farm records remain separately durable. For measured voice RAM requirements, adjust to `standard-4` if needed; model quality and latency still require a real hosted test.

For a smaller typed-only deployment, set image build variable `INSTALL_SPEECH=0` and consider `GHAYTH_INSTANCE_TYPE=standard-1`. The normal Docker build already defaults to this speech-free image. Never point native Python/PyTorch processing at the Python Workers runtime.

Containers require the Workers Paid plan, with usage charges in addition to its base fee. Memory/disk while running, active CPU, R2 operations/storage, logs and the database all affect the total. Keeping an 8 GiB container warm all month costs materially more than the base Workers fee. Review the account's live pricing and limits before publishing or turning on keep-warm; this repository does not provision a paid plan or set a spending budget.

References: [Container instance limits](https://developers.cloudflare.com/containers/platform/limits/), [Container pricing](https://developers.cloudflare.com/containers/platform/pricing/), [R2 pricing](https://developers.cloudflare.com/r2/pricing/).

## 8. Operations over several months

- Keep the same deployment identity, database, session secret and R2 prefix across ordinary updates.
- Enable database backups appropriate to the retention required. R2 is a scientific cache checkpoint, not a replacement for PostgreSQL backups.
- Monitor `/health/ready`, Cloudflare container logs, ingestion status and account usage. Avoid public uptime probes every few seconds if allowing database/container scale-to-zero.
- R2 checkpoint indices are atomic publication points. Interrupted uploads can leave unreferenced blobs; they are harmless but may consume storage. No automatic garbage collection deletes source evidence.
- Pending jobs survive a process restart in PostgreSQL. The current executor is still one process, not a distributed queue. Keep one stable container identity and one analysis worker.
- A sudden kill can interrupt uncommitted cache work. The prior checkpoint remains valid; resumed work reruns missing acquisition. Storage failure leaves the analysis failed/retryable instead of claiming its files are durable.
- There is no automatic expiration date for this deployment. When the demo period ends, deliberately disable/remove the Worker/container and decide separately whether to retain the database and R2 evidence.

## Verification status

Local validation covers the Worker router's TypeScript types and isolated runtime tests, R2 restore/checkpoint behavior with injected S3 storage, startup validation, API/speech integration and the existing application suite. No live Cloudflare resource, Neon database or Docker image build is implied by these tests. Complete the real account/image/HTTP checks above before calling the service deployed.

### Recorded checks for these changes

- Backend/product/speech suite: 237 passed, five optional browser/PostGIS skips, nine passing subtests.
- Five Worker runtime tests cover settings, request/cookie forwarding, shared cold startup, background keepalive and readiness retries.
- Frontend TypeScript, security-policy tests and production build passed.
- Ruff lint and formatting passed for 63 scoped Python files.
- A Worker-only Wrangler packaging dry run passed. The complete image dry run reached the Docker prerequisite and could not build because Docker is unavailable in this workspace. The manual CI workflow supplies a Docker-capable build path; it has not been run here.
- No hosted database, R2 bucket or public deployment was created by local validation. Existing development configuration and records were preserved.
