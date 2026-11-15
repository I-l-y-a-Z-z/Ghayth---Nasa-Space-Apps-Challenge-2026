# Product scope

Ghayth helps a farmer in Morocco answer: what can I grow on this field, in what order, and when? Environmental selection, sowing dates and crop durations are the primary decisions. Financial estimates support that comparison.

## Farmer experience

The main flow has four steps: select the field, answer farming questions, compare crop options, and inspect/save the plan. The interface supports phones and Darija, French and English. Darija uses RTL; switching languages preserves the selected plot and draft.

The map supports search, optional device location, polygon drawing/editing and explicit confirmation. The server validates the boundary and calculates hectares. Search and device location center the map without establishing a field boundary.

Analysis starts after confirmation. Dataset progress, missing coverage and retries remain visible without discarding successful layers. Reopening reuses a matching analysis; changed boundaries or explicit refresh request new work.

The farmer form asks about irrigation, tractor access and previous crops, with unknown answers allowed. Irrigated answers can include supply reliability. Financial fields and technical payloads stay out of the normal flow. The API retains detailed constraint and measurement inputs for integrations.

## Planning

The catalog includes wheat, durum wheat, barley, faba bean, lentil, winter chickpea and fallow. Search evaluates dates and sequences against qualified crop rules, historical environmental exposure and farm information. The shortlist favors distinct productive crop combinations rather than repeated sequences with different dates.

Suggestions and custom rotations use the same evaluator. Plans show planting/harvest dates, operations, environmental fit, estimated expenses/sales and outstanding confirmations. Comparisons use shared assumptions. Known conflicts and missing evidence carry explicit reasons.

Unknown and irrigated systems can receive conditional screening against qualified rainfed calendar references. The engine preserves the declared system and never infers irrigation volume. Generated multi-season plans alternate crop families as a conservative policy, not a validated disease-return rule.

Automatic estimates use Moroccan historical producer prices, yields and crop budgets. Applicable local records can override references. Missing references remain unavailable. Weather stress does not automatically become a precise yield or revenue adjustment.

## Assistant and saved plans

Typed and spoken questions use the selected plot and evaluation. The assistant explains engine facts and can evaluate changes on a copy. Saving a changed result is explicit. Voice is limited to 25 seconds, transcribed for review/editing, then sent after confirmation. Audio is not persisted.

Plans retain their plot revision, dates, assumptions and evidence. Source updates and revised rules affect new evaluations rather than existing snapshots.

## Evidence and validation

Regional estimates, field measurements, farmer inputs and assumptions remain distinguishable. Scientific coverage can differ from crop-rule coverage. Missing values never become observed zeroes or invented crop histories.

Environmental outputs are historical screening indices, not forecasts, calibrated yield predictions or probabilities of success. Local cultivar/soil calibration, current field economics and agricultural speech accuracy remain validation work. See [model sources](model-data-sources.md) and [testing](testing.md).
