# Local records and workspace access

Applicable imported producer/farm-gate observations can override automatic published references. The normal farmer flow does not ask for financial inputs; see [economic reference calculation](economics.md). The contracts below describe API/admin imports and access controls.

Market observations and service directory entries can be imported from user-supplied JSON or CSV. Records remain scoped to the selected project. Imports validate the complete batch before writing; repeated imports of the same project/kind/source ID update that record without creating duplicates. Applicable producer/farm-gate prices can supply new live estimates; retail/wholesale series are not silently treated as farm-sale prices. Existing saved estimates retain their original snapshot.

## Market observations

```json
{
  "record_kind": "market",
  "id": "bulletin-2025-11-wheat-settat",
  "crop_id": "wheat",
  "date": "2025-11-01",
  "price": "350.00",
  "unit": "MAD/quintal",
  "market_level": "wholesale",
  "locality": "Settat",
  "source_url": "https://example.org/your-actual-bulletin",
  "source_label": "Replace with the real bulletin title"
}
```

This is an input-format illustration, not a real quotation. Supply an actual dated source. A source URL or descriptive source label is required. Supported units are `MAD/kg`, `MAD/quintal` (100 kg) and `MAD/tonne` (1000 kg). The importer preserves `original_price` and `original_unit`, and stores the canonical price per kilogram once. Subsequent reads use the canonical value. Public money contracts serialize currency to two decimal places.

Market level must be `producer`, `farm_gate`, `wholesale`, `retail` or `benchmark`. These levels are retained; a retail or wholesale value is never silently presented as a farm-gate receipt. Future-dated observations are rejected. The farmer must enter a sale-price assumption separately before financial estimation can use one.

## Public service entries

```json
{
  "record_kind": "service",
  "id": "actual-directory-entry-id",
  "name": "Name from the supplied public directory",
  "service": "combine harvesting",
  "locality": "Settat",
  "verification_date": "2026-11-01",
  "source_url": "https://example.org/your-actual-public-directory"
}
```

Replace this example with a real entry and a date on which it was checked. An actual source URL and verification date are required. Contacts are optional. A supplied `contact` requires `contact_public: true`, confirming that the source publishes it for this business/service purpose. Entries without that declaration are rejected; no contact or supplier is invented. An entry last checked more than 365 days ago remains visible with `SERVICE_RECONFIRMATION_REQUIRED` and partial availability. Directory presence does not establish current machinery availability or a price quote.

## Location and project matching

Both record types accept `geographic_bounds: [west, south, east, north]` in WGS84. The whole plot polygon must fit the bounds. Bounds take precedence over a locality-name match. Without bounds, locality must match the profile's named pilot region or a documented alias, such as Settat/Chaouia or Saiss/Meknes. Unknown places do not match by proximity.

Explicit Morocco/national entries are included as labeled national context with partial availability. They do not become plot-level prices or verified local providers. Other-project records are never read. If no applicable user-supplied record exists, the section is explicitly missing with `LOCAL_MARKET_RECORDS_MISSING` or `LOCAL_SERVICE_RECORDS_MISSING`.

JSON imports contain a list of record objects; CSV imports use these field names as column headers. `record_kind` is `market` or `service`. For convenience, `kind`/`type` and `market_observation`/`supplier` are accepted aliases. CSV bounds can be a JSON array in a quoted cell. Empty optional cells become null. Each batch supports 1–5000 records. Caller transaction boundaries control commit/rollback.

## Optional password gate

`install_access_control(app, settings)` adds `GET /session`, `POST /session` with `{"password":"..."}`, and `DELETE /session`. Replies contain `authenticated`, `required` and allowed `projects`. Login errors never echo submitted passwords. Passwords are compared with constant-time HMAC comparison; session contents are signed with HMAC-SHA256.

With no access password, the local development workflow stays open. When configured, workspace, plot, analysis, rotation, evaluation, saved plan, chat, demo, market, service and record routes require an eight-hour signed session cookie. Cookies are HTTP-only and SameSite Strict; production cookies are also Secure and require HTTPS. Root UI, static assets, health and catalog remain public so the login interface can load.

`X-Project-ID` selects an allowed project after login; it is not itself a credential. The default is `demo`. A signed session cannot access projects outside its granted list, and current configuration further restricts previously signed grants. Configure a stable secret and password before production; the application's settings validation enforces that deployment requirement. Changing the secret invalidates existing sessions.

Verification: `.venv/bin/pytest -q apps/api/tests/test_access.py apps/api/tests/test_records.py`.
