# Infrastructure

The root `Dockerfile` builds React and packages the Python app; `compose.yaml` adds local PostgreSQL/PostGIS and persistent volumes. `cloudflare/` contains the Worker/Containers deployment configuration for hosted PostgreSQL/PostGIS and durable R2 scientific checkpoints. See [deployment setup](../docs/cloudflare.md).

The root `render.yaml` prepares a free text-only demo on Render with the existing Neon database. It creates one Free web service and no paid resources. See [the free deployment guide](../docs/render.md). `render/.env.production` holds private generated deployment values for this checkout and is excluded from Git and image contexts.

`security/headers.mjs` defines production and Vite-development browser policies. `apps/web/vite.config.ts` consumes them and emits portable `_headers` configuration. The product middleware in `app.py` supplies response headers for API-served assets. Tests in `security/` verify map access and SVG handling.

Run `npm test` for policy tests and `npm run check` for the complete frontend check. Apply equivalent response headers when using another hosting platform. See [workspace access](../docs/local-records.md) for the shared-password gate and production settings.
