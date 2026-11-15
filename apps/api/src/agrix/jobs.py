"""Persisted analysis jobs with immutable terminal snapshots and per-layer progress."""

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from threading import Lock

from sqlalchemy import select
from sqlalchemy.orm import Session

from agrix.config import Settings
from agrix.db import AnalysisRow, Repository
from agrix.models import Analysis, Availability, LayerCoverage, Plot, PlotProfile, Reason


def request_key(plot: Plot, start: date, end: date, settings: Settings) -> str:
    from agrix.earth.pipeline import PROFILE_VERSION

    manifest = settings.cache_directory / "assets.json"
    manifest_hash = (
        hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.exists() else "empty"
    )
    body = {
        "version": PROFILE_VERSION,
        "plot": plot.id,
        "revision": plot.revision,
        "period": [str(start), str(end)],
        "assets": manifest_hash,
        "network": settings.network_enabled,
        "soilgrids": os.getenv("AGRIX_SOILGRIDS_WCS", "0"),
        "remote_assets": os.getenv("AGRIX_REMOTE_ASSETS", "0"),
        "hls_max_scenes": os.getenv("AGRIX_HLS_MAX_SCENES", "10"),
        "earthdata_configured": bool(
            os.getenv("EARTHDATA_USERNAME") and os.getenv("EARTHDATA_PASSWORD")
        ),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class AnalysisRunner:
    def __init__(self, engine, settings: Settings, pipeline_factory=None):
        self.engine = engine
        self.settings = settings
        self.pipeline_factory = pipeline_factory
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agrix-analysis")
        self._running = set()
        self._lock = Lock()

    def defer(
        self, session, analysis: Analysis, previous: PlotProfile | None = None, retry_layers=None
    ):
        session.info.setdefault("analysis_jobs", []).append((analysis, previous, retry_layers))

    def launch_committed(self, session):
        for args in session.info.pop("analysis_jobs", []):
            self.submit(*args)

    def submit(self, analysis: Analysis, previous: PlotProfile | None = None, retry_layers=None):
        with self._lock:
            if analysis.id in self._running:
                return
            self._running.add(analysis.id)
        self.executor.submit(self._run, analysis, previous, retry_layers)

    def resume_pending(self):
        try:
            with Session(self.engine) as session:
                rows = session.scalars(
                    select(AnalysisRow).where(
                        AnalysisRow.payload["status"].as_string().in_(["pending", "running"])
                    )
                ).all()
                jobs = [
                    Analysis.model_validate(row.payload)
                    for row in rows
                    if row.payload.get("plot_snapshot")
                ]
            for job in jobs:
                self.submit(job)
        except Exception:
            # Readiness reports missing database/migrations; do not hide the UI at startup.
            return

    def _save(self, analysis):
        with Session(self.engine) as session, session.begin():
            Repository(session).transition_analysis(analysis.model_dump(mode="json"))

    def _run(self, analysis, previous, retry_layers):
        try:
            if previous is None and analysis.previous_analysis_id:
                with Session(self.engine) as session:
                    payload = Repository(session).get("analyses", analysis.previous_analysis_id)
                if payload is not None:
                    earlier = Analysis.model_validate(payload)
                    if (
                        earlier.project_id,
                        earlier.plot_id,
                        earlier.plot_revision,
                        earlier.period,
                    ) != (
                        analysis.project_id,
                        analysis.plot_id,
                        analysis.plot_revision,
                        analysis.period,
                    ):
                        raise ValueError("Retry source must match the analysis identity and period")
                    previous = earlier.profile
            if retry_layers is None:
                retry_layers = analysis.retry_layers
            analysis = analysis.model_copy(deep=True, update={"status": "running"})
            self._save(analysis)
            from agrix.agronomy import load_agronomic_catalog, resolve_rule_coverage
            from agrix.earth import ProfilePipeline

            def progress(layer):
                if not analysis.profile:
                    return
                other = [
                    existing for existing in analysis.profile.layers if existing.id != layer.id
                ]
                analysis.profile.layers = [*other, layer]
                self._save(analysis)

            factory = self.pipeline_factory or ProfilePipeline
            pipeline = factory(
                self.settings.cache_directory, network_enabled=self.settings.network_enabled
            )
            pipeline.progress_callback = progress
            profile = pipeline.run(
                analysis.plot_snapshot,
                analysis.period.start,
                analysis.period.end,
                previous_profile=previous,
                retry_layers=retry_layers,
            )
            zone, coverage = resolve_rule_coverage(analysis.plot_snapshot, load_agronomic_catalog())
            profile.zone_id, profile.rule_coverage = zone, coverage
            self.apply_soil_tests(profile, analysis.plot_snapshot)
            states = {layer.availability for layer in profile.layers}
            status = (
                "complete"
                if states == {Availability.available}
                else "partial"
                if any(s in states for s in (Availability.available, Availability.partial))
                else (
                    "failed"
                    if any(layer.state == "failed" for layer in profile.layers)
                    else "unsupported"
                )
            )
            analysis = analysis.model_copy(
                update={
                    "profile": profile,
                    "status": status,
                    "completed_at": datetime.now(timezone.utc),
                    "error": None,
                }
            )
            self._save(analysis)
        except Exception as exc:
            analysis = analysis.model_copy(
                update={
                    "status": "failed",
                    "completed_at": datetime.now(timezone.utc),
                    "error": Reason(
                        code="ANALYSIS_FAILED", values={"error_type": type(exc).__name__}
                    ),
                }
            )
            try:
                self._save(analysis)
            except Exception:
                pass
        finally:
            with self._lock:
                self._running.discard(analysis.id)

    @staticmethod
    def apply_soil_tests(profile, plot):
        from agrix.models import DateRange, Evidence, Metric, Origin, UnitConversion

        conversions = {
            ("soil_ph", "pH"): ("pH", 1),
            ("soil_ph", "1"): ("pH", 1),
            ("sand_pct", "%"): ("%", 1),
            ("clay_pct", "%"): ("%", 1),
            ("silt_pct", "%"): ("%", 1),
            ("soil_moisture", "%"): ("m3/m3", 0.01),
            ("soil_moisture", "m3/m3"): ("m3/m3", 1),
        }
        from decimal import Decimal

        for index, test in enumerate(
            sorted(plot.constraints.soil_tests, key=lambda t: t.sample_date)
        ):
            conversion = conversions.get((test.property, test.unit))
            if conversion is None:
                continue
            unit, multiplier = conversion
            eid = f"farmer-soil-test-{index}"
            evidence = Evidence(
                id=eid,
                source_id=test.source_label,
                source_version=str(test.sample_date),
                origin=Origin.farmer_provided,
                synthetic=False,
                period=DateRange(start=test.sample_date, end=test.sample_date),
                depth_cm=(test.depth_top_cm, test.depth_bottom_cm),
                unit_conversion=UnitConversion(
                    original_unit=test.unit,
                    canonical_unit=unit,
                    multiplier=Decimal(str(multiplier)),
                ),
                quality_notes=[
                    "Farmer-supplied field measurement; only this property and sampled depth override grid context."
                ],
            )
            profile.evidence.append(evidence)
            property_key = test.property
            if test.property == "soil_moisture" and (test.depth_top_cm, test.depth_bottom_cm) != (
                0,
                100,
            ):
                property_key = (
                    "soil_moisture_surface"
                    if (test.depth_top_cm, test.depth_bottom_cm) == (0, 5)
                    else (f"soil_moisture_depth_{test.depth_top_cm}_{test.depth_bottom_cm}")
                )
            profile.metrics[property_key] = Metric(
                value=test.value * Decimal(str(multiplier)),
                unit=unit,
                origin=Origin.farmer_provided,
                availability=Availability.available,
                evidence_ids=[eid],
                depth_cm=(test.depth_top_cm, test.depth_bottom_cm),
            )
            layerid = "soil_properties" if test.property != "soil_moisture" else "soil_moisture"
            for layer in profile.layers:
                if layer.id == layerid:
                    layer.availability = Availability.partial
                    layer.state = "partial"
                    layer.evidence_ids.append(eid)

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)


def pending_profile(plot, version):
    return PlotProfile(
        plot_id=plot.id,
        plot_revision=plot.revision,
        fixture_version=version,
        zone_id=None,
        rule_coverage=Availability.unsupported,
        metrics={},
        evidence=[],
        synthetic=False,
        layers=[
            LayerCoverage(
                id=id,
                availability=Availability.missing,
                evidence_ids=[],
                state="pending",
                reason=Reason(code="LAYER_PENDING"),
            )
            for id in (
                "rainfall",
                "temperature",
                "soil_properties",
                "soil_moisture",
                "terrain",
                "vegetation",
            )
        ],
    )
