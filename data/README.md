# Data layout

| Path | Content |
| --- | --- |
| `source_registry.yaml` | Versioned source identities, access, units and qualifications |
| `raw/` | Original NASA sample exports and acquisition helpers |
| `aoi/` | Illustrative pilot geometry, not verified parcel ownership |
| `fixtures/` | Example plot payloads for development |
| `cache/` | Ignored downloaded/processed scientific assets and manifests |
| `models/` | Ignored Hugging Face model cache |

Crop catalogs, environmental parameters, economic references and packaged test samples live in `apps/api/src/agrix/fixtures/`. Farm records and saved plans live in the configured database, independently of the scientific cache.

See [data setup](../docs/data-setup.md), [model sources](../docs/model-data-sources.md) and [local record imports](../docs/local-records.md). Keep credentials, consented audio, model weights and runtime databases out of source control.
