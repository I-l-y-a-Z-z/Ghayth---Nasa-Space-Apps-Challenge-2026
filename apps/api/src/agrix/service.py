"""Application orchestration with transaction-scoped storage and pure domain evaluation."""

from datetime import datetime, timezone
from uuid import uuid4

from agrix.db import Repository
from agrix.errors import DomainError
from agrix.evaluator import candidate_requests, evaluate, make_rotation
from agrix.geometry import normalize_geometry
from agrix.models import (
    Analysis,
    AnalysisRequest,
    Availability,
    Catalog,
    ChatReply,
    ChatRequest,
    DateRange,
    DemoScenario,
    Evaluation,
    EvaluationRequest,
    Plan,
    PlanCreate,
    PlanPatch,
    PlanSummary,
    Plot,
    PlotCreate,
    PlotPatch,
    Reason,
    SuggestionRequest,
    Suggestions,
    WeatherSample,
    Workspace,
)
from agrix.profiles import FIXTURE_VERSION, get_profile


class Service:
    def __init__(
        self,
        repository: Repository,
        catalog: Catalog,
        project_id: str,
        runner=None,
        default_analysis_mode: str = "demo",
    ):
        self.repo = repository
        self.catalog = catalog
        self.project_id = project_id
        self.runner = runner
        self.default_analysis_mode = default_analysis_mode

    def get(self, kind: str, id: str, model):
        payload = self.repo.get(kind, id)
        if payload is None or payload.get("project_id") != self.project_id:
            raise DomainError("NOT_FOUND", f"{kind.rstrip('s').capitalize()} not found", 404)
        return model.model_validate(payload)

    def put(self, kind: str, model):
        self.repo.put(kind, model.id, model.model_dump(mode="json"))
        return model

    def get_plot(self, id: str) -> Plot:
        return self.get("plots", id, Plot)

    def workspace(self) -> Workspace:
        scenarios = [
            DemoScenario.model_validate(
                {k: value for k, value in record.items() if k in DemoScenario.model_fields}
            )
            for record in self.repo.list_project("local_records", self.project_id)
            if record.get("record_kind") == "demo_scenario"
        ]
        scenario_ids = {item.plot_id for item in scenarios}
        plots = [
            Plot.model_validate(row) for row in self.repo.list_project("plots", self.project_id)
        ]
        plots.sort(key=lambda plot: (plot.id not in scenario_ids, plot.name))
        plans = []
        for row in self.repo.list_project("plans", self.project_id):
            plan = Plan.model_validate(row)
            plans.append(
                PlanSummary(
                    id=plan.id,
                    name=plan.name,
                    plot_id=plan.plot_id,
                    plot_revision=plan.plot_revision,
                    revision=plan.revision,
                    status=plan.status,
                    evaluation_id=plan.evaluation_id,
                    evaluation_status=plan.evaluation_snapshot.status
                    if plan.evaluation_snapshot
                    else None,
                    updated_at=plan.updated_at,
                )
            )
        plans.sort(key=lambda plan: plan.updated_at, reverse=True)
        weather = [
            WeatherSample.model_validate(
                {k: value for k, value in row.items() if k in WeatherSample.model_fields}
            )
            for row in self.repo.public_weather()
        ]
        return Workspace(
            plots=plots,
            plans=plans,
            scenarios=scenarios,
            weather_samples=weather,
            catalog=self.catalog,
        )

    @staticmethod
    def check_revision(plot: Plot, revision: int) -> None:
        if plot.revision != revision:
            raise DomainError("STALE_PLOT_REVISION", "Use the current plot revision", 409)

    def create_plot(self, request: PlotCreate) -> Plot:
        geometry, area = normalize_geometry(request.geometry)
        now = datetime.now(timezone.utc)
        mode = request.analysis_mode or self.default_analysis_mode
        constraints = request.constraints.model_copy(deep=True)
        if mode == "live":
            for field in ("setup_cost_mad", "wage_mad_per_person_day"):
                if field not in request.constraints.model_fields_set:
                    setattr(constraints, field, None)
        plot = Plot(
            id=str(uuid4()),
            project_id=self.project_id,
            name=request.name,
            revision=1,
            geometry=geometry,
            area_ha=area,
            constraints=constraints,
            created_at=now,
            updated_at=now,
            analysis_mode=mode,
        )
        return self.put("plots", plot)

    @staticmethod
    def live_constraints(constraints):
        constraints = constraints.model_copy(deep=True)
        for field in ("setup_cost_mad", "wage_mad_per_person_day"):
            if field not in constraints.model_fields_set:
                setattr(constraints, field, None)
        return constraints

    def patch_plot(self, id: str, request: PlotPatch) -> Plot:
        old = self.get_plot(id)
        self.check_revision(old, request.expected_revision)
        changes = request.model_dump(exclude_none=True, exclude={"expected_revision"})
        if request.geometry is not None:
            normalized, area = normalize_geometry(request.geometry)
            changes.update(geometry=normalized, area_ha=area)
        if request.constraints is not None:
            changes["constraints"] = (
                self.live_constraints(request.constraints)
                if old.analysis_mode == "live"
                else request.constraints
            )
        # Identical updates are idempotent; revisions describe actual input changes.
        proposed = Plot.model_validate({**old.model_dump(), **changes})
        if proposed == old:
            return old
        proposed = proposed.model_copy(
            update={"revision": old.revision + 1, "updated_at": datetime.now(timezone.utc)}
        )
        try:
            return self.put("plots", proposed)
        except ValueError as exc:
            raise DomainError(
                "STALE_PLOT_REVISION", "Plot changed during this update", 409
            ) from exc

    def latest_analysis(self, plot_id: str) -> Analysis:
        """Read the current revision without starting acquisition on every page open."""
        plot = self.get_plot(plot_id)
        cached = (
            self.repo.latest_live_analysis(plot.id, plot.revision)
            if plot.analysis_mode == "live"
            else self.repo.find_analysis(plot.id, plot.revision, FIXTURE_VERSION)
        )
        if cached and plot.analysis_mode == "live" and self.runner:
            period = cached.get("period") or {}
            if (period.get("start"), period.get("end")) != (
                str(self.runner.settings.analysis_start),
                str(self.runner.settings.analysis_end),
            ):
                cached = None
        if cached is None:
            raise DomainError("ANALYSIS_REQUIRED", "Start an analysis for this plot revision", 404)
        return Analysis.model_validate(cached)

    def analyze(
        self,
        plot_id: str,
        revision: int | None = None,
        request: AnalysisRequest | None = None,
        force_retry: bool = False,
    ) -> Analysis:
        plot = self.get_plot(plot_id)
        if revision is not None:
            self.check_revision(plot, revision)
        if plot.analysis_mode == "live":
            return self._live_analysis(plot, request or AnalysisRequest(), force_retry)
        cached = self.repo.find_analysis(plot.id, plot.revision, FIXTURE_VERSION)
        if cached:
            return Analysis.model_validate(cached)
        profile = get_profile(plot)
        layers = {layer.availability for layer in profile.layers}
        status = (
            "complete"
            if layers == {Availability.available}
            else ("unsupported" if layers == {Availability.unsupported} else "partial")
        )
        return self.put(
            "analyses",
            Analysis(
                id=str(uuid4()),
                project_id=self.project_id,
                plot_id=plot.id,
                plot_revision=plot.revision,
                fixture_version=FIXTURE_VERSION,
                status=status,
                profile=profile,
                created_at=datetime.now(timezone.utc),
            ),
        )

    def _live_analysis(self, plot: Plot, request: AnalysisRequest, force_retry=False):
        if self.runner is None:
            raise DomainError(
                "PROVIDER_RUNTIME_UNAVAILABLE", "Configure the live analysis runtime", 503
            )
        from agrix.jobs import pending_profile, request_key

        start = request.start_date or self.runner.settings.analysis_start
        end = request.end_date or self.runner.settings.analysis_end
        if end < start or (end - start).days > 365 * 35:
            raise DomainError(
                "INVALID_ANALYSIS_PERIOD", "Select an ordered analysis period of at most 35 years"
            )
        key = request_key(plot, start, end, self.runner.settings)
        cached = self.repo.latest_live_analysis(plot.id, plot.revision, key)
        if cached and cached["status"] in {"pending", "running"}:
            return Analysis.model_validate(cached)
        if cached and not force_retry and not request.retry_layers:
            return Analysis.model_validate(cached)
        reusable = (
            cached
            if cached and cached["status"] in {"complete", "partial", "unsupported"}
            else None
        )
        version = "earth:" + key + (":" + uuid4().hex[:12] if cached else "")
        analysis = Analysis(
            id=str(uuid4()),
            project_id=self.project_id,
            plot_id=plot.id,
            plot_revision=plot.revision,
            fixture_version=version,
            request_key=key,
            status="pending",
            profile=pending_profile(plot, version),
            synthetic=False,
            period=DateRange(start=start, end=end),
            plot_snapshot=plot,
            previous_analysis_id=reusable["id"] if reusable else None,
            retry_layers=request.retry_layers,
            created_at=datetime.now(timezone.utc),
        )
        self.put("analyses", analysis)
        self.runner.defer(
            self.repo.session,
            analysis,
            previous=Analysis.model_validate(reusable).profile if reusable else None,
            retry_layers=request.retry_layers,
        )
        return analysis

    def retry_analysis(self, id: str, request: AnalysisRequest | None = None) -> Analysis:
        previous = self.get("analyses", id, Analysis)
        plot = self.get_plot(previous.plot_id)
        self.check_revision(plot, previous.plot_revision)
        body = request or AnalysisRequest()
        if previous.period:
            body = body.model_copy(
                update={
                    "start_date": body.start_date or previous.period.start,
                    "end_date": body.end_date or previous.period.end,
                }
            )
        if not body.retry_layers and previous.profile:
            body = body.model_copy(
                update={
                    "retry_layers": [
                        layer.id
                        for layer in previous.profile.layers
                        if layer.availability != Availability.available or layer.state == "failed"
                    ]
                }
            )
        return self.analyze(plot.id, plot.revision, body, force_retry=True)

    def catalog_for(self, plot: Plot) -> Catalog:
        if plot.analysis_mode == "demo":
            return self.catalog
        from agrix.agronomy import load_agronomic_catalog

        return load_agronomic_catalog()

    def profile_for(self, plot: Plot):
        cached = (
            self.repo.latest_live_analysis(plot.id, plot.revision)
            if plot.analysis_mode == "live"
            else None
        )
        if plot.analysis_mode == "live" and cached is None:
            raise DomainError(
                "ANALYSIS_REQUIRED", "Start the plot analysis before evaluating a plan", 409
            )
        analysis = (
            Analysis.model_validate(cached) if cached else self.analyze(plot.id, plot.revision)
        )
        if analysis.status in {"pending", "running"}:
            raise DomainError(
                "ANALYSIS_PENDING", "Plot analysis is running; poll its analysis ID", 409
            )
        if analysis.profile is None or analysis.status == "failed":
            raise DomainError("ANALYSIS_UNAVAILABLE", "Retry the plot analysis", 409)
        return analysis

    def evaluate(
        self, request: EvaluationRequest, *, baseline: Evaluation | None = None
    ) -> Evaluation:
        plot = self.get_plot(request.plot_id)
        self.check_revision(plot, request.plot_revision)
        if plot.analysis_mode == "live":
            scenario = request.scenario.model_copy(deep=True)
            if scenario.id == "fixture-baseline-v1":
                scenario.id = "historical-baseline-v1"
                scenario.label_key = "scenario.historical"
            if scenario.evidence_ids == ["fixture-scenario-v1"]:
                scenario.evidence_ids = ["historical-scenario-v1"]
            scenario.synthetic = False
            request = request.model_copy(
                update={
                    "scenario": scenario,
                    "constraints": self.live_constraints(request.constraints)
                    if request.constraints
                    else None,
                }
            )
        if baseline is not None:
            if (baseline.project_id, baseline.plot_id, baseline.plot_revision) != (
                self.project_id,
                plot.id,
                plot.revision,
            ):
                raise DomainError(
                    "EVALUATION_PLOT_MISMATCH", "What-if context must match the current plot"
                )
            profile = baseline.input_snapshot.profile
            catalog = baseline.input_snapshot.catalog
            analysis_id = baseline.analysis_id
        else:
            analysis = self.profile_for(plot)
            profile, catalog, analysis_id = analysis.profile, self.catalog_for(plot), analysis.id
            if plot.analysis_mode == "live":
                profile = self.screening_profile(
                    plot, profile, catalog, request.constraints or plot.constraints
                )
        economic_evidence, economic_summary = [], None
        if plot.analysis_mode == "live" and baseline is None:
            from agrix.economics import resolve_economics

            enriched, economic_evidence, economic_summary = resolve_economics(
                self.repo,
                plot,
                profile,
                request.constraints or plot.constraints,
                request.start_date,
            )
            request = request.model_copy(update={"constraints": enriched})
        try:
            result = evaluate(plot, profile, request, catalog, analysis_id=analysis_id)
        except (ValueError, OverflowError) as exc:
            raise DomainError("EVALUATION_OUT_OF_RANGE", str(exc)) from exc
        if economic_summary is not None:
            result.economics = economic_summary
            known = {e.id for e in result.evidence}
            result.evidence.extend(e for e in economic_evidence if e.id not in known)
            ids = [e.id for e in economic_evidence]
            for totals in (result.financials.whole_plot, result.financials.per_ha):
                for field in type(totals).model_fields:
                    metric = getattr(totals, field)
                    metric.evidence_ids = list(dict.fromkeys([*metric.evidence_ids, *ids]))
            for event in result.cash_events:
                event.evidence_ids = list(dict.fromkeys([*event.evidence_ids, *ids]))
        if baseline is not None:
            result.economics = baseline.economics
            result.market_history = baseline.market_history.model_copy(deep=True)
            result.suppliers = baseline.suppliers.model_copy(deep=True)
            known = {e.id for e in result.evidence}
            result.evidence.extend(
                e.model_copy(deep=True) for e in baseline.evidence if e.id not in known
            )
        elif plot.analysis_mode == "live":
            from agrix.models import Evidence, Origin
            from agrix.records import get_market_history, get_services

            result.market_history = get_market_history(
                self.repo, plot, profile, [c.crop_id for c in result.rotation.crops]
            )
            result.suppliers = get_services(self.repo, plot, profile)
            known = {e.id for e in result.evidence}
            for observation in result.market_history.data or []:
                for eid in observation.evidence_ids:
                    if eid not in known:
                        result.evidence.append(
                            Evidence(
                                id=eid,
                                source_id=observation.source_label or "local-market-record",
                                source_version=str(observation.date),
                                origin=Origin.observed,
                                synthetic=False,
                                source_url=observation.source_url,
                            )
                        )
                        known.add(eid)
        return self.put("evaluations", result)

    @staticmethod
    def screening_profile(plot, profile, catalog, constraints):
        from agrix.agronomy import resolve_rule_coverage
        from agrix.models import Availability, Evidence, Origin

        profile = profile.model_copy(deep=True)
        reference_constraints = constraints
        if constraints.production_system in {"unknown", "irrigated"}:
            # A conservative rainfed reference can be screened without asserting irrigation access.
            reference_constraints = constraints.model_copy(update={"production_system": "rainfed"})
            profile.evidence.append(
                Evidence(
                    id="unconfirmed-rainfed-screen-v1",
                    source_id="conditional-rainfed-screening",
                    source_version="1",
                    origin=Origin.assumption,
                    synthetic=False,
                    availability=Availability.partial,
                    quality_notes=[
                        "Candidate dates use qualified rainfed crop references conditionally. The declared production system is preserved; irrigation volumes are never inferred."
                    ],
                )
            )
        effective = plot.model_copy(update={"constraints": reference_constraints})
        zone, coverage = resolve_rule_coverage(effective, catalog)
        profile.zone_id, profile.rule_coverage = zone, coverage
        if zone is None and reference_constraints.production_system == "rainfed":
            profile.zone_id = "morocco_screening"
            profile.rule_coverage = Availability.partial
            profile.evidence.append(
                Evidence(
                    id="morocco-broad-environmental-screen-v1",
                    source_id="qualified-Morocco-environmental-screening",
                    source_version="1",
                    origin=Origin.assumption,
                    synthetic=False,
                    quality_notes=[
                        "Broad ecological screening outside pilot extents; no nearest-region assignment or local calendar approval.",
                        "Only calendars without explicit regional restrictions can generate dates; chickpea still requires its named regional calendar.",
                    ],
                )
            )
        return profile

    def suggest(self, request: SuggestionRequest) -> Suggestions:
        plot = self.get_plot(request.plot_id)
        self.check_revision(plot, request.plot_revision)
        try:
            catalog = self.catalog_for(plot)
            if plot.analysis_mode == "live":
                from agrix.agronomy import generate_candidates

                analysis = self.profile_for(plot)
                request = request.model_copy(
                    update={
                        "constraints": self.live_constraints(request.constraints)
                        if request.constraints is not None
                        else plot.constraints.model_copy(deep=True)
                    }
                )
                profile = self.screening_profile(
                    plot, analysis.profile, catalog, request.constraints
                )
                requests = generate_candidates(request, catalog, profile, plot=plot)
            else:
                requests = candidate_requests(request, catalog)
        except (ValueError, OverflowError) as exc:
            raise DomainError("EVALUATION_OUT_OF_RANGE", str(exc)) from exc
        candidates = [self.evaluate(candidate) for candidate in requests]
        priorities = (request.constraints or plot.constraints).priorities
        if priorities or plot.analysis_mode == "live":
            priority = priorities[0] if priorities else "environment"

            def score(item):
                totals = item.financials.whole_plot
                value = (
                    totals.operating_margin.value
                    if priority in {"margin", "profit", "high_margin"}
                    else totals.peak_funding_need.value
                )
                secondary = (
                    value is None,
                    -value
                    if value is not None and priority in {"margin", "profit", "high_margin"}
                    else value or 0,
                )
                if priority == "low_labor":
                    labor = item.labor.data
                    days = (
                        labor.paid_person_days + labor.family_person_days
                        if labor.paid_person_days is not None
                        and labor.family_person_days is not None
                        else None
                    )
                    secondary = (days is None, days or 0)
                if plot.analysis_mode == "live":
                    from agrix.agronomy.environment import environmental_sort_key

                    agronomic_failure = any(
                        c.outcome == "fail"
                        and c.reason.code
                        not in {
                            "BUDGET_CHECK",
                            "LABOR_CAPACITY_CHECK",
                            "MONTHLY_LABOR_CHECK",
                            "MACHINERY_ACCESS_CHECK",
                        }
                        for c in item.checks
                    )
                    return (
                        agronomic_failure,
                        *environmental_sort_key(item.environmental),
                        *secondary,
                    )
                return secondary

            candidates.sort(key=score)
        return Suggestions(
            candidates=candidates,
            synthetic=plot.analysis_mode == "demo",
            reasons=[]
            if candidates
            else [
                Reason(
                    code="HORIZON_TOO_SHORT_FOR_FIXTURE_CROP"
                    if catalog.synthetic
                    else "NO_SUPPORTED_ROTATION_IN_PERIOD"
                )
            ],
        )

    def _current_evaluation(self, plot: Plot, id: str, selected: bool = False) -> Evaluation:
        evaluation = self.get("evaluations", id, Evaluation)
        if evaluation.plot_id != plot.id:
            raise DomainError("EVALUATION_PLOT_MISMATCH", "Evaluation belongs to another plot")
        self.check_revision(plot, evaluation.plot_revision)
        if selected and evaluation.status == "doesnt_fit":
            raise DomainError("PLAN_NOT_SELECTABLE", "Save a conflicting evaluation as a draft")
        return evaluation

    def _draft(self, plot: Plot, request: EvaluationRequest) -> EvaluationRequest:
        if request.plot_id != plot.id:
            raise DomainError("DRAFT_PLOT_MISMATCH", "Draft belongs to another plot")
        self.check_revision(plot, request.plot_revision)
        if plot.analysis_mode == "live":
            scenario = request.scenario.model_copy(deep=True)
            if scenario.id == "fixture-baseline-v1":
                scenario.id, scenario.label_key = "historical-baseline-v1", "scenario.historical"
            if scenario.evidence_ids == ["fixture-scenario-v1"]:
                scenario.evidence_ids = ["historical-scenario-v1"]
            scenario.synthetic = False
            request = request.model_copy(
                update={
                    "scenario": scenario,
                    "constraints": self.live_constraints(request.constraints)
                    if request.constraints is not None
                    else plot.constraints.model_copy(deep=True),
                }
            )
        try:
            catalog = self.catalog_for(plot)
            zone_id = None
            if plot.analysis_mode == "live":
                from agrix.agronomy import resolve_rule_coverage

                zone_id, _ = resolve_rule_coverage(plot, catalog)
            make_rotation(request, catalog, zone_id)
        except (ValueError, OverflowError) as exc:
            raise DomainError("EVALUATION_OUT_OF_RANGE", str(exc)) from exc
        return request.model_copy(update={"constraints": request.constraints or plot.constraints})

    def create_plan(self, request: PlanCreate) -> Plan:
        plot = self.get_plot(request.plot_id)
        evaluation = (
            self._current_evaluation(plot, request.evaluation_id, request.status == "selected")
            if request.evaluation_id
            else None
        )
        draft = self._draft(plot, request.draft) if request.draft else None
        now = datetime.now(timezone.utc)
        return self.put(
            "plans",
            Plan(
                id=str(uuid4()),
                project_id=self.project_id,
                plot_id=plot.id,
                plot_revision=plot.revision,
                revision=1,
                name=request.name,
                status=request.status,
                evaluation_id=evaluation.id if evaluation else None,
                evaluation_snapshot=evaluation,
                assumptions=evaluation.input_snapshot.request.constraints
                if evaluation
                else draft.constraints,
                draft=draft,
                created_at=now,
                updated_at=now,
                synthetic=evaluation.synthetic if evaluation else plot.analysis_mode == "demo",
            ),
        )

    def patch_plan(self, id: str, request: PlanPatch) -> Plan:
        plan = self.get("plans", id, Plan)
        if plan.revision != request.expected_revision:
            raise DomainError("STALE_PLAN_REVISION", "Use the current plan revision", 409)
        plot = self.get_plot(plan.plot_id)
        updates = {"revision": plan.revision + 1, "updated_at": datetime.now(timezone.utc)}
        if request.name is not None:
            updates["name"] = request.name
        status = request.status or plan.status
        evaluation = plan.evaluation_snapshot
        if request.evaluation_id is not None:
            evaluation = self._current_evaluation(plot, request.evaluation_id, status == "selected")
            updates.update(
                evaluation_id=evaluation.id,
                evaluation_snapshot=evaluation,
                assumptions=evaluation.input_snapshot.request.constraints,
                plot_revision=evaluation.plot_revision,
                draft=None,
                synthetic=evaluation.synthetic,
            )
        if request.draft is not None:
            if status == "selected":
                raise DomainError(
                    "DRAFT_CANNOT_BE_SELECTED", "Evaluate a draft before selecting it"
                )
            draft = self._draft(plot, request.draft)
            evaluation = None
            updates.update(
                draft=draft,
                evaluation_id=None,
                evaluation_snapshot=None,
                assumptions=draft.constraints,
                plot_revision=plot.revision,
            )
        if request.status == "selected":
            if evaluation is None:
                raise DomainError("EVALUATION_REQUIRED", "A selected plan needs an evaluation")
            self._current_evaluation(plot, evaluation.id, True)
        updates["status"] = status
        try:
            return self.put("plans", plan.model_copy(update=updates))
        except ValueError as exc:
            raise DomainError(
                "STALE_PLAN_REVISION", "Plan changed during this update", 409
            ) from exc

    def chat(self, request: ChatRequest) -> ChatReply:
        from agrix.chat import contextual_reply

        return contextual_reply(self, request)

    def _summary_chat(self, request: ChatRequest) -> ChatReply:
        plot = self.get_plot(request.plot_id)
        evaluations = [self.get("evaluations", id, Evaluation) for id in request.evaluation_ids]
        if any(item.plot_id != plot.id for item in evaluations):
            raise DomainError(
                "EVALUATION_PLOT_MISMATCH", "Chat evaluations must belong to this plot"
            )
        facts = []
        lines = []
        for index, evaluation in enumerate(evaluations, 1):
            totals = evaluation.financials.whole_plot
            peak = totals.peak_funding_need.value
            margin = totals.operating_margin.value
            peak_text = format(peak, ".2f") if peak is not None else None
            margin_text = format(margin, ".2f") if margin is not None else None
            facts.append(
                {
                    "evaluation_id": evaluation.id,
                    "plot_revision": evaluation.plot_revision,
                    "peak_funding_need_mad": peak_text,
                    "operating_margin_mad": margin_text,
                    "status": evaluation.status,
                }
            )
            if request.locale == "ary":
                peak_display = f"{peak_text} درهم" if peak_text is not None else "خاصنا معلومات كثر"
                margin_display = (
                    f"{margin_text} درهم" if margin_text is not None else "مازال ما معروفش"
                )
                lines.append(
                    f"الخطة {index}: أكبر مبلغ خاصك توفر هو {peak_display}، "
                    f"والهامش التقديري هو {margin_display}."
                )
            elif request.locale == "fr":
                lines.append(
                    f"Plan {index} : besoin maximal de financement "
                    f"{peak_text + ' MAD' if peak_text is not None else 'à confirmer'} ; "
                    f"marge estimée {margin_text + ' MAD' if margin_text is not None else 'inconnue'}."
                )
            else:
                lines.append(
                    f"Plan {index}: peak funding "
                    f"{peak_text + ' MAD' if peak_text is not None else 'needs confirmation'}; "
                    f"estimated operating margin {margin_text + ' MAD' if margin_text is not None else 'unknown'}."
                )
        notices = {
            "ary": "هاد الأرقام غير أمثلة مصطنعة للتجربة. هاد الجواب ما كيبدل حتى خطة محفوظة.",
            "fr": "Chiffres synthétiques de démonstration. Aucun plan enregistré n’est modifié.",
            "en": "Synthetic demonstration figures. Saved plans are unchanged.",
        }
        if any(item.plot_revision != plot.revision for item in evaluations):
            notices["ary"] += " هاد الحساب راجع لنسخة قديمة ديال القطعة."
            notices["fr"] += " Ce résultat utilise une ancienne révision de la parcelle."
            notices["en"] += " This result uses an older plot revision."
        return ChatReply(
            message="\n".join(lines + [notices[request.locale]]),
            locale=request.locale,
            evaluation_ids=request.evaluation_ids,
            facts=facts,
            evidence_ids=sorted({e.id for item in evaluations for e in item.evidence}),
            reason=Reason(code="SYNTHETIC_CHAT_FINANCIAL_SUMMARY"),
        )
