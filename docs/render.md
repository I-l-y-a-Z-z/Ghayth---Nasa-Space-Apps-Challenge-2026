# Free hosted demo on Render

The recommended zero-payment demo uses one **Render Free** web service and the existing **Neon Free** PostgreSQL/PostGIS database. The same service serves React, FastAPI, saved plans and typed chat over HTTPS. Server speech recognition and synthesis are disabled. Cloudflare Containers and Workers Paid are not required.

[render.yaml](../render.yaml) defines only one service with `plan: free`. It creates no Render database, paid worker, persistent disk or scaling policy. Deployments are manual. Keep the Render workspace on its free plan and do not enable paid compute. Without a payment method, Render suspends free services or builds when their included allowances run out instead of billing overages. See [Render's free service limits](https://render.com/docs/free).

## Configuration

The Docker image builds the frontend and installs the scientific backend. `INSTALL_SPEECH=0` omits the speech-model dependencies. `AGRIX_SPEECH_ENABLED=false` also prevents importing or executing speech models, even if those packages are installed. The chat interface hides recording/playback controls and continues to accept typed questions.

One API process and one analysis worker limit concurrency. The blueprint caps HLS acquisition at three scenes and GDAL's cache at 32 MB. Existing scientific availability/provenance rules remain in force; this setup does not substitute demo fixtures for missing live observations. It does not configure a paid LLM API.

At startup, the application validates production authentication and a hosted TLS PostgreSQL connection, applies Alembic migrations, then listens on Render's `PORT`. `/health/ready` checks database access. A missing database or password makes startup fail instead of falling back to local SQLite.

## Deploy

1. Push the reviewed source, including `render.yaml`, to the GitHub branch you intend to deploy.
2. Sign in to Render and connect the Render integration here, or choose **New → Blueprint** in the Render dashboard and select that repository and branch.
3. Confirm that the proposed service is **Free**, then provide the prompted `DATABASE_URL` and workspace password. Render generates a stable session secret.
4. Deploy and open the resulting `https://…onrender.com/app/` address. No separate frontend host, proxy or CORS configuration is needed.

For this checkout, production connection details already live in the ignored `infrastructure/cloudflare/.env.neon`. A separate, owner-readable `infrastructure/render/.env.production` contains the database connection and generated workspace/session secrets for an API/manual deployment. Keep these files private. Do not paste credentials into chat or replace the development `.env`.

The Neon database is `ghayth`; its `0001_foundation` migration and PostGIS have been verified. It starts without local farm records. Do not provision Render's expiring free PostgreSQL database in its place. See [Neon plans](https://neon.com/docs/introduction/plans) for its current allowances.

## Data persistence

Farm records, saved plans, analyses and evaluation snapshots are stored in Neon and survive Render restarts. Render Free has an ephemeral filesystem: downloaded scientific cache files are lost when it sleeps, restarts or redeploys. Pending analyses resume from their database state and can reacquire inputs. Completed database snapshots remain available; this does not retain their raw scientific files outside the snapshot.

The initial free setup leaves R2 unconfigured and does not depend on a Cloudflare Worker. The private `ghayth-scientific` R2 bucket has already been created and is available if durable scientific-file retention is needed. To enable it on Render, add `AGRIX_R2_BUCKET`, `AGRIX_R2_PREFIX`, `AGRIX_R2_ENDPOINT`, `AGRIX_R2_ACCESS_KEY_ID` and `AGRIX_R2_SECRET_ACCESS_KEY`, using an Object Read & Write key scoped to that bucket. Keep one writer per prefix and stay within the [R2 free allowance](https://developers.cloudflare.com/r2/pricing/). The private Cloudflare container gateway cannot be used by an external Render service.

## Verify the deployment

- Open `/health` and `/health/ready`, sign in, and confirm `/speech/status` reports `enabled: false`.
- Draw a Moroccan field, wait for acquisition, compare rotations and change their duration.
- Ask a typed question, save a plan, restart the service, then confirm that the saved plan still opens.

The local demo planning smoke test peaked at approximately 162 MiB of process memory. That is a local measurement, not a bound on live geospatial processing. Render Free provides 512 MB RAM, sleeps after 15 minutes without incoming traffic, and typically takes about a minute to wake. Larger analyses can exceed the memory or outbound-traffic allowances. This is suitable for a small demo with cold starts, not guaranteed continuous availability over the intended months. See [Render's instance guidance](https://render.com/articles/from-side-project-to-production-scaling-your-first-app).

The repository configuration follows [Render's Blueprint specification](https://render.com/docs/blueprint-spec). Account connection and a successful hosted build are still needed before an application URL is available.
