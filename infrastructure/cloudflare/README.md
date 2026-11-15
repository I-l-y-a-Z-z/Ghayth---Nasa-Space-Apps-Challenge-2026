# Ghayth on Cloudflare

This package deploys the full app through a Worker and one Durable Object-managed Linux container. PostgreSQL/PostGIS stores farm records; private R2 checkpoints preserve scientific assets. The browser uses one HTTPS origin for frontend, API and speech.

Containers requires Workers Paid. R2 is enabled and the private bucket has been created for this checkout, but the account has not been upgraded and the app has not been deployed. The free Cloudflare plan cannot run this container.

Use Node 24. From the repo root, run `npm run cloudflare:install` then `npm run cloudflare:check`. Docker is required for `npm run cloudflare:dry-run` and `npm run cloudflare:deploy`.

Follow [the deployment guide](../../docs/cloudflare.md) for the Neon database, R2 bucket, secrets, account costs and verification. `.dev.vars.example` is a local template; populated `.dev.vars` is ignored. Never commit secrets or replace the root development `.env` with production credentials.

The hosted database connection for this checkout is saved separately in the ignored `.env.neon` file with owner-only permissions. The root `.neon` context selects the hosted project without pulling values into the development `.env`. Upload the hosted `DATABASE_URL` as a Worker secret when configuring deployment.

`src/config.ts` validates and allowlists container settings. `src/runtime.ts` shares cold startup, forwards requests without replaying mutations, and keeps background jobs alive with durable alarms. `src/cache.ts` provides private R2 access through the container's outbound binding, including multipart uploads. `src/index.ts` supplies the Cloudflare bindings and stable singleton identity. Unit tests inject the container and R2 APIs without starting a real container.
