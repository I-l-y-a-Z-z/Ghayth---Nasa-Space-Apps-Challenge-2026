# Automatic Moroccan economic references

The farmer is not asked for cash, prices, expected yield, wages, operation costs, equipment prices or payment terms. The app asks only farming questions. `agrix.economics.resolve_economics` enriches each new live evaluation from sourced references; saved farmer records are not rewritten and saved plans keep their immutable economic snapshot.

## Sources and limits

- **FAOSTAT PP**: official Morocco annual producer prices for wheat, barley, dry broad beans, lentils and chickpeas. Retrieved normalized bulk data on 2026-11-15. The packaged extract contains 2020–2024 observations; latest available annual values are used, bounded by the planning date. LCU/tonne for Morocco is converted to MAD/kg. Wholesale and retail records are excluded from automatic farm-sale prices.
- **FAOSTAT QCL**: official Moroccan national yields, in kg/ha, for the same items. These are national historical budgeting references, not plot-level yield predictions. Wheat and durum use the explicitly labeled aggregate FAOSTAT wheat series; no separate durum observation is invented.
- **ONCA cereals reference**, edition filename 2017-05-29, table 34 (printed page 108): published regional seasonal variable costs. Wheat 6,581 MAD/ha and durum 6,030 MAD/ha refer to the Berrechid–Benslimane–Settat plains; barley 4,142 MAD/ha refers to Khouribga–Brouj. These are historical costs, not current contractor prices.
- **ONCA legumes reference**, market 31/2015, phase III: regional faba, lentil and chickpea budgets, including Casablanca–Settat and Fes–Meknes tables. The observation year is not explicitly stated: it is stored as null and never presented as a dated contemporary quote.

Exact source URLs, locality, values, original FAOSTAT flags, units and qualifications are retained in `apps/api/src/agrix/fixtures/morocco_economics.json` and each evaluation's `economics.references`. All selected FAOSTAT rows currently have flag A. Actual applicable producer/farm-gate records imported through `/records/import` take priority. Locality matching takes priority over other regional reference budgets. Where no local cost observation exists, the app uses a clearly identified regional Moroccan benchmark; it does not claim this is a quote from the selected village.

Sources:

- https://www.fao.org/faostat/en/#data/PP
- https://www.fao.org/faostat/en/#data/QCL
- https://bulks-faostat.fao.org/production/Prices_E_All_Data_(Normalized).zip
- https://bulks-faostat.fao.org/production/Production_Crops_Livestock_E_All_Data_(Normalized).zip
- https://ardna.org/admincockpit/files/contenus_files/ticket_file641196ed6f8cc-N891-R%C3%A9f%C3%A9rentiel-c%C3%A9r%C3%A9ales-MEP-HB-29_05_2017-%28final%29.pdf
- https://ardna.org/admincockpit/files/contenus_files/ticket_file6197a6d5ca53c-N1077-Referentiel-legumineuse-Ph-III_VD_1.pdf

## Calculation

Revenue uses the historical reference yield × reference producer price × plot area. Costs use the selected historical seasonal cost × plot area for each productive crop. Costs are reserved at sowing for a conservative cash-reserve estimate; precise operation-level spending is left unknown rather than allocating an invented percentage. Seasonal reserves replace individual operation cash charges, preventing double counting. The operating estimate excludes equipment purchases and land acquisition; the zero setup amount describes that scope. Buyer payment lag remains an explicit scheduling assumption. No inflation adjustment or weather-calibrated yield response is invented. Missing references stay unavailable.

Financial estimates remain secondary to environmental rotation selection. The normal farmer request clears previously entered financial overrides. Existing API/admin overrides and imported local records remain supported.

## Refresh with requests

```bash
.venv/bin/python scripts/sync_economics.py
# In .env, then restart:
# AGRIX_ECONOMICS_DATA=./data/economics.json
```

The script requests official FAOSTAT bulk releases, extracts Morocco crop records and replaces the runtime snapshot atomically only after validation. It preserves the reviewed ONCA cost records. Network acquisition does not block an individual farmer's rotation request.
