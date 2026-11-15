# Source provenance

Ghayth combines the existing local AgriX engine with the supplied frontend and Darija speech branch from [malakelassali/Nasa_hackathon](https://github.com/malakelassali/Nasa_hackathon).

| Source | Revision |
| --- | --- |
| Frontend main | `3def038da1ea2c6ab0a3608eef7349c1115ee007` |
| Speech branch | `11fda9ae409bc2769f265241d55c4bc6a5f3e834` |
| AgriX engine | Existing local backend, retained under `apps/api/` |

The client uses engine responses for geometry, analysis, recommendations, reports, saved plans and chat. Speech wraps recognition/synthesis models and sends reviewed transcripts to the same planning engine. The standalone voice development adapter is not mounted in Ghayth.

Scientific source identities, immutable evaluations and internal `agrix` compatibility names are retained. External speech models are pretrained dependencies; no separately trained agricultural checkpoint is claimed.

The application was consolidated into this root from the former `ghayth/` directory. Duplicate application trees, historical checkpoints, delivery archives and unused design references were removed. Runtime records, scientific caches, credentials and models were preserved.
