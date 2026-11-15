# Serving product voice

Run the full Ghayth application from the repository root with `./scripts/start.sh`; the planning API and speech routes share one origin. Install [speech dependencies](../README.md) to enable model inference. The standalone development preview is separate and is not the product entry point.

Microphone access requires HTTPS or localhost. For access from another device, provide HTTPS through the chosen reverse proxy and configure the root `.env` production gate:

```bash
AGRIX_ENV=production
AGRIX_ACCESS_PASSWORD=your-workspace-password
AGRIX_SESSION_SECRET=a-stable-random-secret-at-least-32-characters-long
```

Use a stable secret and the intended database/cache configuration. The shared workspace gate is described in [access controls](../../../docs/local-records.md). No public hosting or tunnel is configured by this repository.

On the client, record up to 25 seconds, stop, review/edit the transcript and confirm sending. Audio is processed in memory. Failed recognition or synthesis leaves typed assistance available. Only one speech inference runs at a time; timed-out work keeps its slot until its worker finishes.
