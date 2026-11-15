import type {Analysis, Evaluation, Plan, Plot, Workspace} from './types';

type Fixtures = {workspace: Workspace; plot: Plot; analysis: Analysis; candidates: Record<string, Evaluation[]>};
type State = {plots: Plot[]; plans: Plan[]; evaluations: Record<string, Evaluation>};
let active = false;
let fixtures: Promise<Fixtures> | undefined;
const key = 'ghayth.offline-demo.v1';
const copy = <T,>(value: T): T => structuredClone(value);
export const demoActive = () => active;
export function activateDemo() {
  if (!active) { active = true; window.dispatchEvent(new Event('ghayth-demo')); }
}
function persist(state: State) { try { localStorage.setItem(key, JSON.stringify(state)); } catch { /* Private browsing can disable storage. */ } }

export async function demoApi<T>(path: string, body?: unknown, method?: string): Promise<T> {
  fixtures ??= fetch('/app/demo-fixtures.json').then(async r => {
    if (!r.ok) throw new Error('Demo examples unavailable');
    return await r.json() as Fixtures;
  });
  const f = await fixtures;
  let state: State;
  try { state = JSON.parse(localStorage.getItem(key) || 'null') as State; } catch { state = null as unknown as State; }
  if (!state?.plots?.length) state = {plots: [copy(f.plot)], plans: [], evaluations: {}};
  const input = (body || {}) as Record<string, any>;
  const route = path.split('?')[0];
  const verb = method || (body === undefined ? 'GET' : 'POST');
  const selected = state.plots.find(p => p.id === input.plot_id) || state.plots[0];
  const profile = (plot: Plot): Analysis => ({...copy(f.analysis), id: `demo-analysis-${plot.id}`, plot_id: plot.id,
    plot_revision: plot.revision, synthetic: true, profile: {...copy(f.analysis.profile!), plot_id: plot.id, plot_revision: plot.revision, synthetic: true}});
  let result: unknown;
  if (route === '/client-config') result = {authentication_required: false, default_analysis_mode: 'demo', demo_mode: true};
  else if (route === '/workspace') result = {...copy(f.workspace), plots: state.plots, plans: state.plans};
  else if (route === '/catalog') result = copy(f.workspace.catalog);
  else if (route === '/session') result = {authenticated: true};
  else if (route === '/speech/status') result = {enabled: false, recognition: {available: false, model: null}, synthesis: {available: false, model: null}};
  else if (route === '/plots' && verb === 'POST') {
    const plot = {...copy(f.plot), ...input, id: crypto.randomUUID(), revision: 1, analysis_mode: 'demo' as const};
    state.plots.push(plot); result = plot;
  } else if (/^\/plots\/[^/]+$/.test(route)) {
    const plot = state.plots.find(p => p.id === route.split('/')[2]) || selected;
    if (verb === 'PATCH') Object.assign(plot, input, {revision: plot.revision + 1, analysis_mode: 'demo'});
    result = plot;
  } else if (/^\/plots\/[^/]+\/analyses/.test(route)) {
    result = profile(state.plots.find(p => p.id === route.split('/')[2]) || selected);
  } else if (route.startsWith('/analyses/')) result = profile(selected);
  else if (route === '/rotations/suggest' || route === '/rotations/evaluate') {
    const bank = f.candidates[String(input.horizon_days)] || f.candidates['730'];
    const candidates = bank.map((sample, i) => {
      const evaluation = copy(sample);
      evaluation.id = `demo-${selected.id}-${selected.revision}-${input.horizon_days || 730}-${i}`;
      evaluation.plot_id = selected.id; evaluation.plot_revision = selected.revision;
      evaluation.synthetic = true;
      evaluation.input_snapshot.request.plot_id = selected.id;
      evaluation.input_snapshot.request.plot_revision = selected.revision;
      state.evaluations[evaluation.id] = evaluation;
      return evaluation;
    });
    result = route.endsWith('suggest') ? {candidates} : candidates.find(c => c.rotation.crops[0]?.crop_id === input.sequence?.[0]?.crop_id) || candidates[0];
  } else if (route.startsWith('/evaluations/')) result = state.evaluations[route.split('/')[2]];
  else if (route === '/plans' && verb === 'POST') {
    const evaluation = state.evaluations[input.evaluation_id] || null;
    const plan: Plan = {id: crypto.randomUUID(), name: input.name || 'Demonstration plan', plot_id: selected.id,
      plot_revision: selected.revision, revision: 1, status: input.status || 'draft', evaluation_id: evaluation?.id || null,
      evaluation_status: evaluation?.status || null, updated_at: new Date().toISOString(),
      evaluation_snapshot: evaluation, assumptions: copy(selected.constraints), draft: input.draft || null};
    state.plans.push(plan); result = plan;
  } else if (route.startsWith('/plans/')) {
    const plan = state.plans.find(p => p.id === route.split('/')[2]);
    if (plan && verb === 'PATCH') Object.assign(plan, input, {revision: plan.revision + 1});
    result = plan;
  } else if (route === '/chat') {
    const evaluation = state.evaluations[input.evaluation_ids?.[0]];
    const message = input.locale === 'ary'
      ? 'هاد عرض تجريبي بمعطيات نموذجية. قارن خطط الزراعة وشوف وقت الزرع والمصاريف، وتقدر تحفظ الخطة فهاد الجهاز. التحليل المباشر ما متوفرش دابا.'
      : input.locale === 'fr'
        ? 'Cette démonstration utilise des exemples préparés. Comparez les rotations, leurs dates et leurs coûts, puis enregistrez un plan sur cet appareil. L’analyse en direct est indisponible.'
        : 'This demonstration uses prepared examples. Compare crop rotations, dates and costs, then save a plan on this device. Live field analysis is currently unavailable.';
    result = {message, locale: input.locale || 'en', evaluation_ids: evaluation ? [evaluation.id] : [], facts: [],
      evidence_ids: [], read_only: true, synthetic: true, requires_confirmation: false};
  } else if (route.startsWith('/health')) result = {status: 'ok', mode: 'demo'};
  else throw new Error('This action requires the live service');
  persist(state);
  return copy(result) as T;
}
