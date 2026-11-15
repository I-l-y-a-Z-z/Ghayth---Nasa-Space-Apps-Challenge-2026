# Infrastructure

The root `Dockerfile` builds React and packages the Python app; `compose.yaml` adds PostgreSQL/PostGIS and persistent database/scientific-cache volumes. No external deployment is configured.

`security/headers.mjs` defines production and Vite-development browser policies. `apps/web/vite.config.ts` consumes them and emits portable `_headers` configuration. The product middleware in `app.py` supplies response headers for API-served assets. Tests in `security/` verify map access and SVG handling.

Run `npm test` for policy tests and `npm run check` for the complete frontend check. Apply equivalent response headers when using another hosting platform. See [workspace access](../docs/local-records.md) for the shared-password gate and production settings.
