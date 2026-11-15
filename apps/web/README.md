# Ghayth web client

React/TypeScript, Vite and MapLibre. Run `npm run dev` from the repository root alongside the API on port 8000. `npm run build` builds the client and copies it into the API static directory.

`src/App.tsx` coordinates plots, analysis, rotations and saved plans. `MapPanel.tsx` handles field boundaries, `FarmForm.tsx` farming questions, `SimplePlan.tsx` the farmer plan, and `ChatPanel.tsx` typed/voice assistance. Localization is in `i18n.ts`; contracts are in `types.ts`. Shared presentational primitives live in `packages/ui`.

The development proxy forwards API/speech requests to the backend. Production assets use `/app/`; `infrastructure/security` supplies browser policy. See [architecture](../../docs/architecture.md) and [verification](../../docs/testing.md).
