# Moroccan agronomy and environmental planning

The catalog `morocco-environment-screening-v2` combines qualified Moroccan guidance with explicit environmental screening parameters. It supports wheat, durum wheat, barley, faba bean, lentil, winter chickpea and fallow. [Model sources](model-data-sources.md) describes the current inputs and calculations.

## Published rules

| Rule | Scope and implementation | Primary reference |
| --- | --- | --- |
| Cereals: sowing early November to mid-December | Inclusive 1 November–15 December advisory window; applied only in the pilot lookup regions, with regional confirmation still required | Ministry/NOVEC cereal technical reference, version 2237-N891-16c, edition 29 May 2017, printed p.43 |
| Barley: avoid cereal predecessors | Explicit exclusion of wheat, durum wheat or barley as the immediately preceding crop; unknown history remains unknown | Same reference, printed p.23 |
| Winter legumes: November–December | Faba and lentil: 1 November–31 December | INRA practical legume guide, 2015, printed p.11 |
| Winter chickpea regional calendar | Chaouia/Abda: November–December; Saiss/Zaer: December–January. Cross-year windows include 31 January. Spring chickpea requires a separate template | INRA guide, printed p.8 |
| Faba and winter chickpea pH | Faba: 6.5–8; chickpea: 6–9. A modeled outside-range pH triggers confirmation, rather than a categorical rejection | INRA guide, printed pp.8–9 |
| Indicative legume seed quantities | Lentil 50, chickpea 80, faba 120 kg/ha. Seed size, germination and field conditions must be confirmed | INRA guide, printed p.11 |

Read the [cereal reference PDF](https://ardna.org/admincockpit/files/contenus_files/ticket_file641196ed6f8cc-N891-R%C3%A9f%C3%A9rentiel-c%C3%A9r%C3%A9ales-MEP-HB-29_05_2017-%28final%29.pdf) and [INRA legume guide PDF](https://www.agripedia.ma/sites/default/files/2019-03/Guide-pratique-pour-le-conseil-agricole-lentille-pois-chiche-f%C3%A8ve.pdf). The latter is an INRA-authored document on an Agripedia mirror, legal deposit `2015MO4520`, ISBN `978-9954-36-822-0`.

The cereal guide's regional rotation table concerns holdings below 5 ha. Its Settat cereal–legume recommendations therefore do not become an unqualified national prohibition. No generic family return interval, disease-breaking fallow duration, annual-rainfall threshold or climate-to-yield multiplier is invented. The guide's soft-wheat water requirement is not interpreted as a precipitation-only threshold: irrigation, soil storage, runoff and evapotranspiration would also be needed.

## Geographic and farming-system qualification

Approximate lookup extents near Chaouia/Settat, Saiss/Meknes, Zaer and Abda are assumptions, not official agricultural-zone boundaries. Whole-polygon containment is required for those named lookups. Broader qualified rainfed screening is supported where the source permits it; regional chickpea calendars remain restricted. Unknown coverage remains explicit.

Unknown and irrigated answers can use conditional rainfed calendar references while retaining the declared farming system. This does not establish an irrigated agronomic template or invent irrigation supply. Generated multi-season plans alternate crop families as a conservative policy, not a documented disease-return interval.

## Search and dates

The beam search screens crop/date windows environmentally before truncation, preserves distinct crop paths, then reassesses complete sequences with continuous water storage. The shortlist chooses the best dates per ordered productive sequence and prioritizes distinct combinations over reversed versions. Fallow padding does not create a new productive rotation identity.

Automatic maturity uses historical thermal accumulation and target uncertainty when supported. Missing temperatures and unreached maturity remain explicit. Farmer-supplied end dates are authoritative; incompatible short schedules need confirmation rather than silently dropping operations.

## Historical scenarios

Scenarios use common historical anchor years and preserve offsets across a rotation. They report rain totals, coverage, confirmed dry spells, diagnostic heat/frost and a defined three-day rain-onset indicator. Missing days never become dry days or zero rain. These are exposure scenarios rather than forecasts or calibrated yield responses.

Environmental scores rank conservative supported bounds and expose missing-input uncertainty. Source-qualified rules, farmer constraints and provisional screening policies remain distinguishable. See the versioned [environmental parameters](../apps/api/src/agrix/fixtures/environmental_rules.yaml).

Focused checks: `.venv/bin/python -m pytest apps/api/tests/test_agronomy.py apps/api/tests/test_environmental_selection.py -q`.
