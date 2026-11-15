"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    workspace: null,
    plot: null,
    profile: null,
    evaluation: null,
    candidates: [],
    sequence: [],
    baseConstraints: {},
    dirty: false,
    selectionVersion: 0,
    openPlanVersion: 0,
    workVersion: 0,
    busy: new Set(),
    toastTimer: null,
  };
  const cropLabels = { wheat_demo: "Wheat", barley_demo: "Barley", faba_demo: "Faba bean" };
  const cropName = (id) => cropLabels[id] || id.replaceAll("_", " ");
  const statusLabels = {
    fits_current_inputs: "Fits these inputs",
    needs_confirmation: "Needs confirmation",
    doesnt_fit: "Doesn’t fit",
  };
  const statusClass = (value) => value === "doesnt_fit" ? "negative" : value === "needs_confirmation" ? "caution" : "";
  const number = (value, digits = 0) => value === null || value === undefined || value === "" || !Number.isFinite(Number(value)) ? "—" : new Intl.NumberFormat("en-GB", { maximumFractionDigits: digits }).format(Number(value));
  const mad = (value) => value === null || value === undefined ? "Needs confirmation" : `${number(value, 2)} MAD`;
  const metricValue = (metric) => metric?.value === null || metric?.value === undefined ? null : Number(metric.value);
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const readableDate = (value, full = false) => {
    if (!value) return "To confirm";
    const date = new Date(`${value.slice(0, 10)}T12:00:00Z`);
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", ...(full ? { year: "numeric" } : {}), timeZone: "UTC" }).format(date);
  };

  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined && text !== null) item.textContent = String(text);
    return item;
  }

  function button(text, className, action) {
    const item = node("button", className, text);
    item.type = "button";
    item.addEventListener("click", action);
    return item;
  }

  function toast(message) {
    $("toast").textContent = message;
    $("toast").hidden = false;
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => { $("toast").hidden = true; }, 4500);
  }

  function showError(error) {
    $("error-message").textContent = error?.message || "Something went wrong. Please try again.";
    $("error-banner").hidden = false;
  }

  async function api(path, options = {}) {
    let response;
    try {
      response = await fetch(path, {
        ...options,
        headers: { "Content-Type": "application/json", "X-Project-ID": "demo", ...options.headers },
        ...(options.body !== undefined ? { body: JSON.stringify(options.body) } : {}),
      });
    } catch {
      throw new Error("The API could not be reached. Check that the local server is running, then refresh.");
    }
    let data;
    try { data = await response.json(); } catch { throw new Error("The server returned an unexpected response. Please refresh and try again."); }
    if (!response.ok) {
      const details = data.error?.details?.map((e) => `${e.loc?.filter((x) => x !== "body").join(" → ")}: ${e.message}`).join("; ");
      const error = new Error(`${data.error?.message || "Request failed"}${details ? `. ${details}` : ""}`);
      error.code = data.error?.code;
      throw error;
    }
    return data;
  }

  async function task(key, action, busyText) {
    if (state.busy.has(key)) return;
    state.busy.add(key);
    const control = $(key);
    const text = control?.textContent;
    if (control) { control.disabled = true; if (busyText) control.textContent = busyText; }
    $("error-banner").hidden = true;
    try { await action(); } catch (error) { showError(error); }
    finally {
      state.busy.delete(key);
      if (control) { control.disabled = false; control.textContent = text; }
      updateActions();
    }
  }

  function setView(view) {
    document.querySelectorAll(".view-tab").forEach((tab) => {
      const active = tab.dataset.view === view;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll(".view-panel").forEach((panel) => { panel.hidden = panel.id !== `view-${view}`; });
    if (view === "saved") renderSaved();
    if (view === "weather") renderWeather();
  }

  function scenarioFor(plotId) {
    return state.workspace?.scenarios?.find((item) => item.plot_id === plotId);
  }

  function renderSidebar() {
    const target = $("plot-list");
    target.replaceChildren();
    const plots = state.workspace?.plots || [];
    const scenarioIds = new Set((state.workspace?.scenarios || []).map((item) => item.plot_id));
    const ordered = [...plots].sort((a, b) => Number(scenarioIds.has(b.id)) - Number(scenarioIds.has(a.id)));
    for (const plot of ordered) {
      const scenario = scenarioFor(plot.id);
      const item = button("", "plot-choice", () => selectPlot(plot.id));
      item.classList.toggle("active", plot.id === state.plot?.id);
      item.setAttribute("aria-pressed", String(plot.id === state.plot?.id));
      item.append(node("span", "plot-choice-icon", "⌁"));
      const main = node("span", "plot-choice-main");
      main.append(node("span", "plot-choice-title", scenario?.title || plot.name));
      const meta = node("span", "plot-choice-meta");
      meta.append(node("span", `choice-indicator ${statusClass(scenario?.expected_status)}`));
      meta.append(node("span", "", `${scenario?.region || "Test plot"} · ${number(plot.area_ha, 1)} ha`));
      main.append(meta);
      item.append(main);
      target.append(item);
    }
    if (!plots.length) target.append(node("p", "muted sidebar-placeholder", "No plots yet. Load the sample scenarios or add your own test plot."));
  }

  function renderOverview() {
    const plot = state.plot;
    if (!plot) return;
    const scenario = scenarioFor(plot.id);
    $("plot-title").textContent = scenario?.title || plot.name;
    $("breadcrumb-plot").textContent = scenario?.region || plot.name;
    $("plot-region").textContent = (scenario?.region || "YOUR SELECTED PLOT").toUpperCase();
    $("plot-description").textContent = scenario?.description || "An illustrative boundary for testing crop rotations and farm constraints.";
    $("plot-area").textContent = number(plot.area_ha, 2);
    const coordinates = plot.geometry.coordinates[0];
    const center = coordinates.slice(0, -1).reduce((sum, coordinate) => [sum[0] + coordinate[0], sum[1] + coordinate[1]], [0, 0]).map((v) => v / (coordinates.length - 1));
    $("plot-coordinates").textContent = `${center[1].toFixed(3)}° N · ${Math.abs(center[0]).toFixed(3)}° ${center[0] < 0 ? "W" : "E"}`;
    $("plot-revision").textContent = `Revision ${plot.revision}`;
  }

  function renderProfile() {
    const target = $("profile-grid");
    target.replaceChildren();
    const definitions = [
      ["rainfall_mm", "Demo rainfall", "mm", "☂", "Annual fixture period"],
      ["temperature_c", "Mean temperature", "°C", "☀", "Regional demo estimate"],
      ["soil_ph", "Soil pH", "", "◉", "0–30 cm · Demo estimate"],
      ["soil_moisture", "Root-zone moisture", "m³/m³", "≈", "0–100 cm · Demo estimate"],
    ];
    const missing = [];
    for (const [id, label, unit, symbol, note] of definitions) {
      const card = node("div", "profile-card");
      const heading = node("div", "metric-heading");
      heading.append(node("span", "", label), node("span", "metric-symbol", symbol));
      card.append(heading);
      if (!state.profile) {
        card.append(node("div", "loading-shimmer wide"));
      } else {
        const metric = state.profile.metrics[id];
        const value = metricValue(metric);
        const valueNode = node("div", `metric-value${value === null ? " missing" : ""}`, value === null ? "Needs confirmation" : number(value, id === "soil_moisture" ? 2 : 1));
        if (value !== null) valueNode.append(node("span", "metric-unit", unit));
        else missing.push(label.toLowerCase());
        card.append(valueNode, node("div", "metric-note", value === null ? (metric?.availability === "unsupported" ? "Outside this fixture’s coverage" : "Not supplied for this scenario") : note));
      }
      target.append(card);
    }
    const rulesMissing = state.profile && state.profile.rule_coverage !== "available";
    $("profile-status").className = `soft-badge${missing.length || rulesMissing ? " caution" : ""}`;
    $("profile-status").textContent = !state.profile ? "Loading profile" : rulesMissing ? "Crop rules need evidence" : missing.length ? "Partial demo profile" : "Complete demo profile";
    $("profile-gaps").hidden = !missing.length && !rulesMissing;
    $("profile-gaps").textContent = [missing.length ? `Missing ${missing.join(", ")}.` : "", rulesMissing ? "No applicable crop rule is available here. The evaluator will keep unsupported yield and price values unknown." : ""].filter(Boolean).join(" ");
  }

  function fillInputs(constraints, request = null) {
    state.baseConstraints = clone(constraints || {});
    $("budget-input").value = constraints?.budget_mad ?? "";
    $("machinery-input").value = constraints?.machinery_access || "unknown";
    $("labor-input").value = constraints?.labor_person_days_available ?? "";
    $("payment-input").value = constraints?.payment_lag_days ?? 30;
    $("setup-input").value = constraints?.setup_cost_mad ?? "200.00";
    $("wage-input").value = constraints?.wage_mad_per_person_day ?? "100.00";
    $("planning-start").value = request?.start_date || "2026-11-01";
    const horizon = request?.horizon_days || 730;
    const select = $("planning-horizon");
    if (![...select.options].some((o) => Number(o.value) === horizon)) select.add(new Option(`${horizon} days`, String(horizon)));
    select.value = String(horizon);
    const container = $("crop-assumptions");
    container.replaceChildren();
    for (const crop of state.workspace?.catalog?.crops || []) {
      const section = node("div", "assumption-crop");
      section.append(node("h4", "", cropName(crop.id)));
      for (const [kind, label, value] of [
        ["yield", "Yield · kg / ha", constraints?.yield_kg_per_ha?.[crop.id]],
        ["price", "Sale price · MAD / kg", constraints?.sale_price_mad_per_kg?.[crop.id]],
      ]) {
        const wrapper = node("label", "", label);
        const input = node("input");
        input.type = "number";
        input.min = "0";
        input.step = kind === "yield" ? "50" : "0.10";
        input.value = value ?? "";
        input.placeholder = kind === "yield" ? `Catalog demo: ${number(crop.yield_kg_per_ha)}` : `Catalog demo: ${number(crop.sale_price_mad_per_kg, 2)}`;
        input.dataset.crop = crop.id;
        input.dataset.kind = kind;
        input.addEventListener("input", markDirty);
        wrapper.append(input);
        section.append(wrapper);
      }
      container.append(section);
    }
    state.dirty = false;
    $("input-dirty-note").textContent = scenarioFor(state.plot?.id)?.assumptions_note || "Demo prices, yields, and costs are editable assumptions.";
  }

  function currentConstraints() {
    const result = clone(state.baseConstraints);
    result.budget_mad = $("budget-input").value === "" ? null : $("budget-input").value;
    result.machinery_access = $("machinery-input").value;
    result.labor_person_days_available = $("labor-input").value === "" ? null : $("labor-input").value;
    result.payment_lag_days = Number($("payment-input").value);
    result.setup_cost_mad = $("setup-input").value || "0.00";
    result.wage_mad_per_person_day = $("wage-input").value || "0.00";
    result.sale_price_mad_per_kg = { ...result.sale_price_mad_per_kg };
    result.yield_kg_per_ha = { ...result.yield_kg_per_ha };
    $("crop-assumptions").querySelectorAll("input").forEach((input) => {
      const key = input.dataset.kind === "yield" ? "yield_kg_per_ha" : "sale_price_mad_per_kg";
      if (input.value !== "") result[key][input.dataset.crop] = input.value;
      else delete result[key][input.dataset.crop];
    });
    return result;
  }

  function requestBase() {
    if (!state.plot) throw new Error("Select or add a plot first.");
    if (!$("planning-form").reportValidity()) return null;
    return {
      plot_id: state.plot.id,
      plot_revision: state.plot.revision,
      start_date: $("planning-start").value,
      horizon_days: Number($("planning-horizon").value),
      constraints: currentConstraints(),
    };
  }

  function markDirty() {
    state.dirty = true;
    state.workVersion += 1;
    $("input-dirty-note").textContent = "Inputs changed — compare or evaluate again to update the displayed figures.";
    updateActions();
  }

  function updateActions() {
    $("save-plan-button").disabled = !state.evaluation || state.dirty || state.busy.has("save-plan-button");
    $("chat-button").disabled = !state.evaluation || state.busy.has("chat-button");
    for (const id of ["suggest-button", "evaluate-button", "save-draft-button", "analyze-button"]) {
      $(id).disabled = !state.plot || state.busy.has(id);
    }
  }

  async function refreshWorkspace(preferredId = null) {
    const workspace = await api("/workspace");
    state.workspace = workspace;
    $("connection-status").className = "connection";
    $("connection-status").replaceChildren(node("span", "status-dot"), document.createTextNode("API connected"));
    $("weather-count").textContent = String(workspace.weather_samples?.length || 0);
    $("saved-count").textContent = String(workspace.plans?.length || 0);
    $("empty-workspace").hidden = Boolean(workspace.plots?.length);
    $("planner-content").hidden = !workspace.plots?.length;
    const scenarioFirst = workspace.scenarios?.find((s) => workspace.plots?.some((p) => p.id === s.plot_id))?.plot_id;
    const id = preferredId || state.plot?.id || scenarioFirst || workspace.plots?.[0]?.id;
    if (id && !workspace.plots?.some((p) => p.id === id)) state.plot = null;
    renderSidebar();
    renderSaved();
    renderWeather();
    const freshPlot = workspace.plots?.find((plot) => plot.id === id);
    if (id && (!state.plot || state.plot.id !== id || state.plot.revision !== freshPlot?.revision)) await selectPlot(id);
    else if (freshPlot) { state.plot = freshPlot; renderOverview(); }
  }

  async function selectPlot(id, { analyze = true } = {}) {
    const plot = state.workspace?.plots?.find((item) => item.id === id);
    if (!plot) throw new Error("This plot is no longer in the workspace. Refresh the workspace first.");
    const token = ++state.selectionVersion;
    state.openPlanVersion += 1;
    state.workVersion += 1;
    state.plot = plot;
    state.profile = null;
    state.evaluation = null;
    state.candidates = [];
    fillInputs(plot.constraints);
    resetSequence();
    renderSidebar();
    renderOverview();
    renderProfile();
    renderCandidates();
    $("evaluation-section").hidden = true;
    $("chat-response").hidden = true;
    updateActions();
    if (analyze) {
      try {
        const analysis = await api(`/plots/${encodeURIComponent(plot.id)}/analyses`, { method: "POST", body: { plot_revision: plot.revision } });
        if (token !== state.selectionVersion) return;
        state.profile = analysis.profile;
        renderProfile();
      } catch (error) { if (token === state.selectionVersion) showError(error); }
    }
  }

  function resetSequence() {
    const year = Number($("planning-start").value.slice(0, 4)) || 2026;
    state.sequence = [{ crop_id: "wheat_demo", start_date: `${year}-11-01` }, { crop_id: "faba_demo", start_date: `${year + 1}-11-01` }];
    renderSequence();
  }

  function renderSequence() {
    const target = $("custom-sequence");
    target.replaceChildren();
    state.sequence.forEach((selection, index) => {
      const row = node("div", "custom-row");
      row.append(node("span", "season-number", String(index + 1).padStart(2, "0")));
      const cropLabel = node("label", "", "Crop");
      const crop = node("select");
      for (const item of state.workspace?.catalog?.crops || []) crop.add(new Option(cropName(item.id), item.id));
      crop.value = selection.crop_id;
      crop.setAttribute("aria-label", `Season ${index + 1} crop`);
      crop.addEventListener("change", () => { selection.crop_id = crop.value; delete selection.end_date; markDirty(); });
      cropLabel.append(crop);
      const dateLabel = node("label", "", "Sowing date");
      const date = node("input");
      date.type = "date";
      date.value = selection.start_date || "";
      date.required = true;
      date.setAttribute("aria-label", `Season ${index + 1} sowing date`);
      date.addEventListener("change", () => { selection.start_date = date.value; delete selection.end_date; markDirty(); });
      dateLabel.append(date);
      const actions = node("div", "row-actions");
      for (const [label, symbol, disabled, action] of [
        ["Move season up", "↑", index === 0, () => { [state.sequence[index - 1], state.sequence[index]] = [state.sequence[index], state.sequence[index - 1]]; renderSequence(); markDirty(); }],
        ["Move season down", "↓", index === state.sequence.length - 1, () => { [state.sequence[index + 1], state.sequence[index]] = [state.sequence[index], state.sequence[index + 1]]; renderSequence(); markDirty(); }],
        ["Remove season", "×", state.sequence.length === 1, () => { state.sequence.splice(index, 1); renderSequence(); markDirty(); }],
      ]) {
        const control = button(symbol, "icon-button", action);
        control.setAttribute("aria-label", `${label} ${index + 1}`);
        control.title = label;
        control.disabled = disabled;
        actions.append(control);
      }
      row.append(cropLabel, dateLabel, actions);
      target.append(row);
    });
    $("add-crop-button").disabled = state.sequence.length >= 20;
  }

  function renderCandidates() {
    const target = $("candidate-grid");
    target.replaceChildren();
    if (!state.candidates.length) {
      const empty = node("div", "empty-state card");
      empty.append(node("span", "empty-symbol", "↔"), node("h3", "", "Your comparison starts here"), node("p", "", "Choose your conditions above, then compare two demonstration crop rotations."));
      target.append(empty);
      return;
    }
    state.candidates.forEach((evaluation, index) => {
      const item = button("", "candidate-card", () => { selectEvaluation(evaluation, { applyInputs: true }); renderCandidates(); });
      item.classList.toggle("selected", state.evaluation?.id === evaluation.id);
      item.setAttribute("aria-pressed", String(state.evaluation?.id === evaluation.id));
      const top = node("div", "candidate-topline");
      top.append(node("span", "candidate-number", `ROTATION ${String(index + 1).padStart(2, "0")}`), node("span", `status-badge ${statusClass(evaluation.status)}`, statusLabels[evaluation.status]));
      item.append(top, node("div", "candidate-sequence", evaluation.rotation.crops.map((crop) => cropName(crop.crop_id)).join(" → ")));
      item.append(node("div", "candidate-date", `${readableDate(evaluation.rotation.start_date, true)} – ${readableDate(evaluation.rotation.end_date, true)} · ${evaluation.rotation.duration_days} days`));
      const totals = evaluation.financials.whole_plot;
      const financial = node("div", "candidate-financials");
      for (const [label, value] of [["Estimated operating margin", totals.operating_margin.value], ["Peak funding needed", totals.peak_funding_need.value]]) {
        const info = node("div");
        info.append(node("small", "", label), node("strong", "", value === null ? "To confirm" : number(value, 2)));
        if (value !== null) info.append(node("em", "", " MAD"));
        financial.append(info);
      }
      item.append(financial);
      const bottom = node("div", "candidate-bottom");
      bottom.append(node("span", "", state.evaluation?.id === evaluation.id ? "Viewing this rotation" : "Explore this rotation"), node("span", "", "↗"));
      item.append(bottom);
      target.append(item);
    });
  }

  function humanReason(check) {
    const { code, values: v = {} } = check.reason || check;
    const labels = {
      NO_APPLICABLE_CROP_RULE: ["Local crop rules need confirmation", "Environmental coverage alone cannot establish crop suitability."],
      CROP_RULE_NOT_APPLICABLE: ["No applicable rule for this crop", "A local agronomic reference is still needed."],
      PROFILE_INPUT_MISSING: [`${({ soil_ph: "Soil pH", rainfall_mm: "Rainfall" })[v.metric] || "A profile input"} is missing`, "The evaluator keeps this input unknown instead of assuming a value."],
      MACHINERY_ACCESS_CHECK: [v.access === "unavailable" ? "Required machinery is unavailable" : "Confirm machinery access", "Book the required equipment or update this assumption."],
      LABOR_CAPACITY_UNKNOWN: ["Labor availability is unknown", "Enter the farm person-days available across this rotation."],
      LABOR_CAPACITY_CHECK: ["Farm labor exceeds your capacity", `${v.required_person_days || "—"} person-days required; ${v.available_person_days || "—"} available.`],
      MONTHLY_LABOR_CHECK: ["Check monthly labor capacity", `Review availability for ${v.month || "this month"}.`],
      PREVIOUS_CROP_UNKNOWN: ["Previous crop needs confirmation", "Crop history helps check the first crop’s predecessor rule."],
      SOWING_WINDOW_CONFLICT: ["Sowing date is outside the demo window", "This demonstration catalog uses November sowing."],
      SEASON_OVERLAP_CHECK: ["Crop seasons overlap", "Move the next crop’s sowing date after the previous harvest."],
      PREDECESSOR_CHECK: ["The preceding crop conflicts with this sequence", "Try an alternate crop group or review the predecessor assumption."],
      PLANNING_HORIZON_CHECK: ["The crop extends beyond your planning window", "Increase the horizon or choose a different sequence."],
      PRODUCTION_SYSTEM_CHECK: ["Production system needs review", "The current fixture rules describe rainfed farming."],
    };
    if (code === "BUDGET_CHECK") return v.required_mad === null ? ["Funding needs are not yet known", "A crop yield or sale price is missing. Add confirmed assumptions before estimating funding."] : v.budget_mad === null ? ["Available budget needs confirmation", "Enter a budget to compare it with peak funding needs."] : ["Peak funding exceeds your budget", `${mad(v.required_mad)} needed against ${mad(v.budget_mad)} available.`];
    return labels[code] || [code.toLowerCase().replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase()), "Review this demonstration check before choosing a plan."];
  }

  function selectEvaluation(evaluation, { applyInputs = false } = {}) {
    state.evaluation = evaluation;
    if (applyInputs) fillInputs(evaluation.input_snapshot.request.constraints, evaluation.input_snapshot.request);
    state.dirty = false;
    state.sequence = evaluation.rotation.crops.map(({ crop_id, start_date }) => ({ crop_id, start_date }));
    renderSequence();
    $("input-dirty-note").textContent = "Showing the evaluated assumptions. Edit them to compare another scenario.";
    $("evaluation-section").hidden = false;
    $("evaluation-subtitle").textContent = `${evaluation.rotation.crops.map((crop) => cropName(crop.crop_id)).join(" → ")} · ${evaluation.rotation.duration_days} days · Demo estimates for the whole plot`;
    $("evaluation-status").className = `status-badge ${statusClass(evaluation.status)}`;
    $("evaluation-status").textContent = statusLabels[evaluation.status];
    $("plan-name").value = `${scenarioFor(state.plot?.id)?.region || state.plot?.name || "Farm"} · ${evaluation.rotation.crops.map((crop) => cropName(crop.crop_id)).join(" + ")}`;
    const totals = evaluation.financials.whole_plot;
    const metrics = $("evaluation-metrics");
    metrics.replaceChildren();
    for (const [label, value, note, featured] of [
      ["Estimated operating margin", totals.operating_margin.value, "Revenue less recurring operation costs", true],
      ["Peak funding needed", totals.peak_funding_need.value, "Largest cash shortfall across the rotation", false],
      ["Operation costs", totals.operating_cost.value, `Plus ${mad(totals.initial_investment.value)} one-time setup`, false],
      ["First scheduled receipt", evaluation.financials.first_receipt_date, "Includes your buyer payment delay", false],
    ]) {
      const card = node("div", `financial-card${featured ? " featured" : ""}`);
      const isDate = label === "First scheduled receipt";
      const amount = node("div", "financial-amount", isDate ? readableDate(value, true) : value === null ? "To confirm" : number(value, 2));
      if (!isDate && value !== null) amount.append(node("small", "", "MAD"));
      if (isDate) amount.style.fontSize = "20px";
      card.append(node("div", "financial-label", label), amount, node("div", "financial-subtitle", note));
      metrics.append(card);
    }
    const reasons = $("evaluation-reasons");
    reasons.replaceChildren();
    const concerns = evaluation.checks.filter((item) => item.outcome !== "pass");
    if (!concerns.length) {
      const item = node("div", "reason-item");
      const main = node("div");
      main.append(node("strong", "", "This plan fits the current demo inputs"), node("p", "", "Dates, equipment, labor and peak cash needs pass the implemented checks. Crop rules and budget estimates remain demonstration assumptions."));
      item.append(node("span", "reason-marker positive", "✓"), main);
      reasons.append(item);
    }
    const seen = new Set();
    concerns.forEach((check) => {
      const key = `${check.reason.code}:${JSON.stringify(check.reason.values)}`;
      if (seen.has(key)) return;
      seen.add(key);
      const [title, detail] = humanReason(check);
      const item = node("div", "reason-item");
      const main = node("div");
      main.append(node("strong", "", title), node("p", "", detail));
      item.append(node("span", `reason-marker ${check.outcome === "fail" ? "negative" : ""}`, check.outcome === "fail" ? "×" : "!"), main);
      reasons.append(item);
    });
    const timeline = $("timeline-body");
    timeline.replaceChildren();
    const instanceMap = new Map(evaluation.rotation.crops.map((crop) => [crop.id, crop]));
    for (const operation of evaluation.timeline.data || []) {
      const row = node("tr");
      const operationLabels = { prepare: "Land preparation", sow: "Sowing & seed", field_care: "Field care", harvest: "Harvest" };
      const labor = Number(operation.paid_person_days) + Number(operation.family_person_days);
      row.append(node("td", "", readableDate(operation.date, true)), node("td", "", operationLabels[operation.code] || operation.code.replaceAll("_", " ")), node("td", "", cropName(instanceMap.get(operation.crop_instance_id)?.crop_id || "")), node("td", "", `${number(labor, 1)} days`), node("td", "numeric", mad(operation.total_cash_cost_mad)));
      timeline.append(row);
    }
    const labor = evaluation.labor.data;
    $("labor-summary").textContent = labor ? `${number(Number(labor.paid_person_days) + Number(labor.family_person_days), 1)} farm person-days · Family labor unpaid` : "Labor needs confirmation";
    renderCashChart(evaluation);
    $("chat-response").hidden = true;
    $("raw-evaluation").textContent = JSON.stringify(evaluation, null, 2);
    updateActions();
  }

  function svgNode(tag, attributes = {}, text = null) {
    const item = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attributes)) item.setAttribute(key, String(value));
    if (text !== null) item.textContent = String(text);
    return item;
  }

  function lineChart(target, entries, { unit = "", ariaLabel = "Data chart", cash = false } = {}) {
    target.replaceChildren();
    const valid = entries.filter((item) => item.value !== null && Number.isFinite(Number(item.value)));
    if (!valid.length) { target.append(node("p", "muted", "No numeric values are available for this chart.")); return; }
    const width = 700, height = 215, left = 53, right = 20, top = 18, bottom = 35;
    const values = valid.map((item) => Number(item.value));
    let low = Math.min(...values, cash ? 0 : Infinity), high = Math.max(...values, cash ? 0 : -Infinity);
    if (low === high) { low = Math.min(0, low); high += 1; }
    const padding = (high - low) * .1;
    low -= padding;
    high += padding;
    const start = Date.parse(`${entries[0].date}T12:00:00Z`);
    const end = Date.parse(`${entries[entries.length - 1].date}T12:00:00Z`);
    const x = (value) => left + (Date.parse(`${value}T12:00:00Z`) - start) / Math.max(1, end - start) * (width - left - right);
    const y = (value) => top + (high - Number(value)) / (high - low) * (height - top - bottom);
    const svg = svgNode("svg", { viewBox: `0 0 ${width} ${height}`, class: "svg-chart", role: "img", "aria-label": ariaLabel });
    svg.append(svgNode("title", {}, ariaLabel));
    for (let i = 0; i <= 3; i += 1) {
      const value = low + (high - low) * i / 3;
      svg.append(svgNode("line", { x1: left, x2: width - right, y1: y(value), y2: y(value), class: "chart-grid" }));
      svg.append(svgNode("text", { x: left - 9, y: y(value) + 3, class: "chart-axis-label", "text-anchor": "end" }, number(value, Math.abs(high) < 10 ? 1 : 0)));
    }
    if (low < 0 && high > 0) svg.append(svgNode("line", { x1: left, x2: width - right, y1: y(0), y2: y(0), class: "chart-zero" }));
    const points = valid.map((item) => `${x(item.date)},${y(item.value)}`);
    const fill = `M${points[0]} L${points.join(" L")} L${x(valid[valid.length - 1].date)},${height - bottom} L${x(valid[0].date)},${height - bottom} Z`;
    svg.append(svgNode("path", { d: fill, class: "chart-fill" }));
    svg.append(svgNode("polyline", { points: points.join(" "), class: "chart-line" }));
    for (const item of valid) {
      const circle = svgNode("circle", { cx: x(item.date), cy: y(item.value), r: valid.length > 20 ? 2 : 3, fill: "#648d47" });
      circle.append(svgNode("title", {}, `${item.date}: ${number(item.value, 2)} ${unit}`));
      svg.append(circle);
    }
    const labels = [valid[0], valid[Math.floor((valid.length - 1) / 2)], valid[valid.length - 1]];
    labels.forEach((item, index) => svg.append(svgNode("text", { x: x(item.date), y: height - 10, class: "chart-axis-label", "text-anchor": index === 0 ? "start" : index === 2 ? "end" : "middle" }, readableDate(item.date, cash))));
    target.append(svg);
  }

  function renderCashChart(evaluation) {
    const target = $("cash-chart");
    if (evaluation.cash_events.some((event) => event.amount_mad === null)) {
      target.replaceChildren(node("div", "empty-state", "Cash flow needs a yield and sale-price assumption for every crop. Known costs are still shown above."));
      return;
    }
    const dated = new Map();
    for (const event of evaluation.cash_events) dated.set(event.date, (dated.get(event.date) || 0) + Number(event.amount_mad) * (event.direction === "inflow" ? 1 : -1));
    let balance = 0;
    const entries = [...dated].sort(([a], [b]) => a.localeCompare(b)).map(([date, amount]) => ({ date, value: (balance += amount) }));
    lineChart(target, entries, { unit: "MAD", ariaLabel: "Cumulative cash flow for this demonstration rotation", cash: true });
  }

  function renderSaved() {
    const target = $("saved-plans");
    target.replaceChildren();
    const plans = state.workspace?.plans || [];
    $("saved-count").textContent = String(plans.length);
    if (!plans.length) {
      const empty = node("div", "empty-state card");
      empty.append(node("span", "empty-symbol", "♡"), node("h3", "", "Your shortlist is ready when you are"), node("p", "", "Compare a rotation and save its evaluation, or keep an unfinished custom sequence as a draft."));
      target.append(empty);
    }
    for (const plan of plans) {
      const card = node("article", "card saved-card");
      const top = node("div", "candidate-topline");
      top.append(node("span", "candidate-number", plan.evaluation_id ? "SAVED EVALUATION" : "DRAFT SEQUENCE"), node("span", `status-badge ${statusClass(plan.evaluation_status)}`, plan.evaluation_status ? statusLabels[plan.evaluation_status] : "Not evaluated"));
      const plot = state.workspace.plots.find((p) => p.id === plan.plot_id);
      card.append(top, node("h3", "", plan.name), node("p", "", `${scenarioFor(plan.plot_id)?.region || plot?.name || "Saved plot"} · Plot revision ${plan.plot_revision}`));
      const footer = node("div", "saved-card-footer");
      footer.append(node("span", "", `Updated ${readableDate(plan.updated_at, true)}`), button("Open plan ↗", "button secondary", () => task(`open-${plan.id}`, () => openPlan(plan.id))));
      card.append(footer);
      target.append(card);
    }
  }

  async function openPlan(id) {
    const intent = ++state.openPlanVersion;
    const originalSelection = state.selectionVersion;
    const plan = await api(`/plans/${encodeURIComponent(id)}`);
    if (intent !== state.openPlanVersion || originalSelection !== state.selectionVersion) return;
    if (!state.workspace.plots.some((plot) => plot.id === plan.plot_id)) await refreshWorkspace();
    const selecting = selectPlot(plan.plot_id);
    const token = state.selectionVersion;
    await selecting;
    if (token !== state.selectionVersion) return;
    if (plan.evaluation_snapshot) {
      selectEvaluation(plan.evaluation_snapshot, { applyInputs: true });
      state.candidates = [plan.evaluation_snapshot];
      renderCandidates();
      if (plan.plot_revision !== state.plot.revision) {
        state.dirty = true;
        $("input-dirty-note").textContent = "This saved result belongs to an earlier plot revision. Reevaluate against the current plot before saving a new selection.";
      }
    } else if (plan.draft) {
      fillInputs(plan.draft.constraints || plan.assumptions, plan.draft);
      state.sequence = clone(plan.draft.sequence);
      renderSequence();
      state.dirty = true;
    }
    $("plan-name").value = plan.name;
    setView("planner");
    updateActions();
    toast("Saved plan reopened with its original assumptions.");
  }

  function weatherEntries(sample, variable) {
    return (sample.rows || []).map((row) => ({ date: row.date, value: row.values?.[variable.id] ?? null }));
  }

  function renderWeather() {
    const target = $("weather-samples");
    target.replaceChildren();
    const samples = state.workspace?.weather_samples || [];
    if (!samples.length) {
      const empty = node("div", "empty-state card");
      empty.append(node("h3", "", "No imported samples in this database yet"), node("p", "", "Load the sample scenarios to include the packaged, source-labeled NASA observations."));
      target.append(empty);
      return;
    }
    for (const sample of samples) {
      const card = node("article", "card weather-sample");
      const heading = node("div", "section-heading");
      const title = node("div");
      title.append(node("div", "eyebrow", sample.provider || "NASA"), node("h3", "", sample.title), node("p", "", `${readableDate(sample.period.start, true)} – ${readableDate(sample.period.end, true)} · ${sample.rows.length} daily records · ${sample.sample_type === "point" ? "Point extraction" : "Area average"}`));
      const variable = node("select", "weather-variable-select");
      variable.setAttribute("aria-label", `Chart variable for ${sample.title}`);
      for (const v of sample.variables) variable.add(new Option(`${v.label} (${v.unit})`, v.id));
      heading.append(title, variable);
      card.append(heading);
      const summary = node("div", "weather-sample-summary");
      const chart = node("div");
      function updateWeather() {
        const definition = sample.variables.find((v) => v.id === variable.value) || sample.variables[0];
        const entries = weatherEntries(sample, definition);
        const values = entries.filter((v) => v.value !== null).map((v) => Number(v.value));
        summary.replaceChildren();
        if (values.length) {
          const mean = values.reduce((sum, v) => sum + v, 0) / values.length;
          const isRain = /precip|rain/i.test(definition.id + definition.label);
          for (const [label, value] of [[isRain ? "Total in sample period" : "Daily mean", isRain ? values.reduce((sum, v) => sum + v, 0) : mean], ["Daily minimum", Math.min(...values)], ["Daily maximum", Math.max(...values)]]) {
            const item = node("div", "weather-stat");
            item.append(node("strong", "", `${number(value, 2)} ${isRain && label.startsWith("Total") ? "mm" : definition.unit}`), node("span", "", label));
            summary.append(item);
          }
        }
        lineChart(chart, entries, { unit: definition.unit, ariaLabel: `${definition.label}, daily imported values from ${sample.period.start} through ${sample.period.end}` });
      }
      variable.addEventListener("change", updateWeather);
      card.append(summary, chart);
      const notes = node("div", "weather-sample-notes");
      for (const note of sample.notes || []) notes.append(node("p", "", note));
      card.append(notes);
      if (sample.source_url) {
        try {
          const url = new URL(sample.source_url);
          if (["https:", "http:"].includes(url.protocol)) {
            const link = node("a", "weather-source", "View provider documentation ↗");
            link.href = url.href;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            card.append(link);
          }
        } catch { /* The source remains readable in the record when it is not a URL. */ }
      }
      target.append(card);
      updateWeather();
    }
  }

  $("planning-form").addEventListener("submit", (event) => {
    event.preventDefault();
    task("suggest-button", async () => {
      const request = requestBase();
      if (!request) return;
      const token = state.selectionVersion;
      const work = state.workVersion;
      const result = await api("/rotations/suggest", { method: "POST", body: request });
      if (token !== state.selectionVersion || work !== state.workVersion) return;
      state.candidates = result.candidates;
      if (!result.candidates.length) { state.evaluation = null; $("evaluation-section").hidden = true; toast("No complete crop fits this planning window. Try a longer horizon."); }
      else selectEvaluation(result.candidates[0]);
      renderCandidates();
    }, "Comparing…");
  });

  $("evaluate-button").addEventListener("click", () => task("evaluate-button", async () => {
    const base = requestBase();
    if (!base) return;
    if (![...$("custom-sequence").querySelectorAll("input")].every((input) => input.reportValidity())) return;
    const token = state.selectionVersion, work = state.workVersion;
    const evaluation = await api("/rotations/evaluate", { method: "POST", body: { ...base, sequence: clone(state.sequence) } });
    if (token !== state.selectionVersion || work !== state.workVersion) return;
    state.candidates = [evaluation];
    selectEvaluation(evaluation);
    renderCandidates();
    $("evaluation-section").scrollIntoView({ behavior: "smooth", block: "start" });
  }, "Evaluating…"));

  $("save-plan-button").addEventListener("click", () => task("save-plan-button", async () => {
    if (!state.evaluation || state.dirty) return;
    const plotId = state.plot.id;
    await api("/plans", { method: "POST", body: { plot_id: plotId, evaluation_id: state.evaluation.id, name: $("plan-name").value.trim() || "My crop rotation", status: state.evaluation.status === "doesnt_fit" ? "draft" : "selected" } });
    await refreshWorkspace(plotId);
    toast("Plan saved with its evaluation and assumptions.");
  }, "Saving…"));

  $("save-draft-button").addEventListener("click", () => task("save-draft-button", async () => {
    const base = requestBase();
    if (!base) return;
    if (![...$("custom-sequence").querySelectorAll("input")].every((input) => input.reportValidity())) return;
    const plotId = state.plot.id;
    await api("/plans", { method: "POST", body: { plot_id: plotId, name: $("plan-name").value.trim() || `${state.plot.name} · Custom draft`, status: "draft", draft: { ...base, sequence: clone(state.sequence) } } });
    await refreshWorkspace(plotId);
    toast("Custom sequence saved as an unevaluated draft.");
  }, "Saving…"));

  $("chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    task("chat-button", async () => {
      if (!state.evaluation) return;
      const evaluationId = state.evaluation.id;
      const token = state.selectionVersion;
      const locale = $("chat-locale").value;
      const reply = await api("/chat", { method: "POST", body: { plot_id: state.plot.id, evaluation_ids: [evaluationId], message: $("chat-message").value, locale } });
      if (token !== state.selectionVersion || state.evaluation?.id !== evaluationId) return;
      $("chat-response").textContent = reply.message;
      $("chat-response").dir = locale === "ary" ? "rtl" : "ltr";
      $("chat-response").lang = locale === "ary" ? "ar-MA" : locale;
      $("chat-response").hidden = false;
    }, "Reading plan…");
  });

  $("add-crop-button").addEventListener("click", () => {
    if (state.sequence.length >= 20) return;
    const last = state.sequence[state.sequence.length - 1];
    const year = Number((last?.start_date || $("planning-start").value).slice(0, 4)) + 1;
    state.sequence.push({ crop_id: last?.crop_id === "faba_demo" ? "wheat_demo" : "faba_demo", start_date: `${year}-11-01` });
    renderSequence();
    markDirty();
  });
  $("analyze-button").addEventListener("click", () => task("analyze-button", async () => {
    if (!state.plot) return;
    const token = state.selectionVersion;
    const analysis = await api(`/plots/${encodeURIComponent(state.plot.id)}/analyses`, { method: "POST", body: { plot_revision: state.plot.revision } });
    if (token !== state.selectionVersion) return;
    state.profile = analysis.profile;
    renderProfile();
    toast("The current plot profile is ready.");
  }, "Refreshing…"));
  $("planning-form").querySelectorAll("input, select").forEach((input) => input.addEventListener("input", markDirty));
  document.querySelectorAll(".view-tab").forEach((tab) => tab.addEventListener("click", () => setView(tab.dataset.view)));
  $("dismiss-error").addEventListener("click", () => { $("error-banner").hidden = true; });
  $("refresh-workspace").addEventListener("click", () => task("refresh-workspace", async () => { await refreshWorkspace(); toast("Workspace refreshed."); }));

  async function seedWorkspace() {
    const workspace = await api("/demo/seed", { method: "POST", body: {} });
    state.workspace = workspace;
    state.plot = null;
    await refreshWorkspace();
    toast("Sample plots, plans and source-labeled weather are ready.");
  }
  $("seed-button").addEventListener("click", () => task("seed-button", seedWorkspace, "Loading scenarios…"));
  $("empty-seed-button").addEventListener("click", () => task("empty-seed-button", seedWorkspace, "Loading scenarios…"));
  $("new-plot-button").addEventListener("click", () => $("new-plot-dialog").showModal());
  $("close-new-plot").addEventListener("click", () => $("new-plot-dialog").close());
  $("new-plot-form").addEventListener("submit", (event) => {
    event.preventDefault();
    task("create-plot-button", async () => {
      const latitude = Number($("new-plot-lat").value), longitude = Number($("new-plot-lon").value), area = Number($("new-plot-area").value);
      const halfMeters = Math.sqrt(area * 10000) / 2;
      const dy = halfMeters / 111320;
      const dx = halfMeters / (111320 * Math.cos(latitude * Math.PI / 180));
      const plot = await api("/plots", { method: "POST", body: {
        analysis_mode: "demo",
        name: $("new-plot-name").value.trim(),
        geometry: { type: "Polygon", coordinates: [[[longitude - dx, latitude - dy], [longitude + dx, latitude - dy], [longitude + dx, latitude + dy], [longitude - dx, latitude + dy], [longitude - dx, latitude - dy]]] },
        constraints: { budget_mad: $("new-plot-budget").value, machinery_access: "unknown", labor_person_days_available: null, previous_crops: null, payment_lag_days: 30, wage_mad_per_person_day: "180.00", setup_cost_mad: "1000.00" },
      } });
      $("new-plot-dialog").close();
      await refreshWorkspace(plot.id);
      setView("planner");
      toast("Test plot created. Confirm its unknown farm inputs before evaluating.");
    }, "Creating plot…");
  });

  refreshWorkspace().catch((error) => {
    $("connection-status").className = "connection offline";
    $("connection-status").replaceChildren(node("span", "status-dot"), document.createTextNode("API unavailable"));
    $("plot-list").replaceChildren(node("p", "muted sidebar-placeholder", "The workspace could not be loaded. Check the API connection and refresh."));
    showError(error);
  });
})();
