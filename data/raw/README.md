# Original scientific samples

Original NASA POWER UTC and IMERG Early/Late exports are retained with their product identities. Their source metadata is recorded in [the source registry](../source_registry.yaml). The packaged monthly sample view uses copies in `apps/api/src/agrix/fixtures/weather_samples.json`.

New plot analyses use the live provider/cache pipeline; these limited October 2024 samples are not complete field profiles. Download helper scripts are acquisition tools rather than scientific observations. Register chosen files with `agrix import-asset`; see [data setup](../../docs/data-setup.md) and [Earth integration](../../docs/earth-integration.md).

Downloaded/processed runtime assets belong in `data/cache/`, which is ignored by source control. Preserve raw source files and their original identities when preparing new subsets.
