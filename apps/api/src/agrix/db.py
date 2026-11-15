"""SQLAlchemy persistence for versioned plots and immutable result snapshots.

PostgreSQL/PostGIS is the application database. SQLite is an explicit option for
offline contract tests; it stores geometry JSON as text and supplies no spatial
database behavior. Callers own their Session and transaction boundaries.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import UserDefinedType


class PostGISPolygon(UserDefinedType):
    """Bind GeoJSON text as an SRID-4326 PostGIS Polygon without GeoAlchemy."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "geometry(POLYGON,4326)"

    def bind_expression(self, bindvalue: Any) -> Any:
        return func.ST_SetSRID(func.ST_GeomFromGeoJSON(bindvalue), 4326)

    def column_expression(self, column: Any) -> Any:
        return func.ST_AsGeoJSON(column, type_=Text())


class Base(DeclarativeBase):
    pass


def _json_type() -> Any:
    return JSON().with_variant(JSONB(), "postgresql")


class PlotRow(Base):
    __tablename__ = "plots"
    __table_args__ = (CheckConstraint("revision >= 1", name="ck_plot_revision_positive"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(128), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(_json_type())


class PlotRevisionRow(Base):
    __tablename__ = "plot_revisions"
    __table_args__ = (CheckConstraint("revision >= 1", name="ck_snapshot_revision_positive"),)

    plot_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("plots.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    geometry: Mapped[str] = mapped_column(Text().with_variant(PostGISPolygon(), "postgresql"))
    payload: Mapped[dict[str, Any]] = mapped_column(_json_type())


Index(
    "ix_plot_revisions_geometry",
    PlotRevisionRow.geometry,
    postgresql_using="gist",
).ddl_if(dialect="postgresql")


class AnalysisRow(Base):
    __tablename__ = "analyses"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plot_id", "plot_revision"],
            ["plot_revisions.plot_id", "plot_revisions.revision"],
        ),
        UniqueConstraint(
            "plot_id", "plot_revision", "fixture_version", name="uq_analysis_revision_fixture"
        ),
        UniqueConstraint("id", "plot_id", "plot_revision", name="uq_analysis_id_plot_revision"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plot_id: Mapped[str] = mapped_column(String(64), index=True)
    plot_revision: Mapped[int] = mapped_column(Integer)
    fixture_version: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(_json_type())


class EvaluationRow(Base):
    __tablename__ = "evaluations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plot_id", "plot_revision"],
            ["plot_revisions.plot_id", "plot_revisions.revision"],
        ),
        ForeignKeyConstraint(
            ["analysis_id", "plot_id", "plot_revision"],
            ["analyses.id", "analyses.plot_id", "analyses.plot_revision"],
        ),
        UniqueConstraint("id", "plot_id", name="uq_evaluation_id_plot"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plot_id: Mapped[str] = mapped_column(String(64), index=True)
    plot_revision: Mapped[int] = mapped_column(Integer)
    analysis_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(_json_type())


class PlanRow(Base):
    __tablename__ = "plans"
    __table_args__ = (
        ForeignKeyConstraint(
            ["evaluation_id", "plot_id"], ["evaluations.id", "evaluations.plot_id"]
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plot_id: Mapped[str] = mapped_column(String(64), ForeignKey("plots.id"), index=True)
    evaluation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(_json_type())


class LocalRecordRow(Base):
    __tablename__ = "local_records"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(_json_type())


_ROWS: dict[str, type[Base]] = {
    "plots": PlotRow,
    "analyses": AnalysisRow,
    "evaluations": EvaluationRow,
    "plans": PlanRow,
    "local_records": LocalRecordRow,
}


def make_engine(url: str) -> Engine:
    """Create an engine without silently switching to another database backend."""
    parsed = make_url(url)
    backend = parsed.get_backend_name()
    if backend not in {"postgresql", "sqlite"}:
        raise ValueError("AgriX supports PostgreSQL/PostGIS and explicit SQLite tests only")
    options: dict[str, Any] = {"pool_pre_ping": True}
    if backend == "sqlite":
        options["connect_args"] = {"check_same_thread": False}
        if parsed.database in {None, "", ":memory:"}:
            options["poolclass"] = StaticPool
    engine = create_engine(url, **options)
    if backend == "sqlite":

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection: Any, connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def create_schema(engine: Engine) -> None:
    """Bootstrap an explicit test/development database; deployments use Alembic."""
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        Base.metadata.create_all(connection)


class Repository:
    """Persist JSON-safe domain snapshots; HTTP and models are deliberately absent."""

    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def _row_type(kind: str) -> type[Base]:
        try:
            return _ROWS[kind]
        except KeyError as exc:
            raise ValueError(f"Unknown repository collection: {kind}") from exc

    def get(self, kind: str, id: str) -> dict[str, Any] | None:
        row = self.session.get(self._row_type(kind), str(id))
        return copy.deepcopy(row.payload) if row is not None else None

    def list_project(self, kind: str, project_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """Bounded, project-scoped discovery for the local testing workspace."""
        if kind not in {"plots", "plans", "local_records", "analyses"}:
            raise ValueError("This collection is not discoverable")
        row_type = self._row_type(kind)
        scope = (
            (row_type.project_id == project_id)
            if kind == "plots"
            else (row_type.payload["project_id"].as_string() == project_id)
        )
        rows = self.session.scalars(
            select(row_type).where(scope).order_by(row_type.id).limit(limit)
        )
        return [copy.deepcopy(row.payload) for row in rows]

    def public_weather(self) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(LocalRecordRow)
            .where(
                LocalRecordRow.payload["record_kind"].as_string() == "weather_sample",
                LocalRecordRow.payload["public"].as_boolean().is_(True),
            )
            .order_by(LocalRecordRow.id)
            .limit(10)
        )
        return [copy.deepcopy(row.payload) for row in rows]

    def put(self, kind: str, id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if str(payload.get("id")) != str(id):
            raise ValueError("Snapshot id must match its repository key")
        if kind == "plots":
            return self.save_plot(payload)
        if kind == "plans":
            return self.save_plan(payload)
        row_type = self._row_type(kind)
        existing = self.session.get(row_type, str(id))
        data = copy.deepcopy(payload)
        if existing is not None and kind in {"analyses", "evaluations"}:
            if existing.payload != data:
                raise ValueError(f"{kind} snapshots are immutable; create a new id")
            return copy.deepcopy(existing.payload)

        fields: dict[str, Any] = {"payload": data}
        if kind in {"analyses", "evaluations"}:
            fields.update(plot_id=str(data["plot_id"]), plot_revision=int(data["plot_revision"]))
        if kind == "analyses":
            fields["fixture_version"] = str(data["fixture_version"])
        elif kind == "evaluations":
            fields["analysis_id"] = data.get("analysis_id")

        if existing is None:
            self.session.add(row_type(id=str(id), **fields))
        else:
            for key, value in fields.items():
                setattr(existing, key, value)
        self.session.flush()
        return copy.deepcopy(data)

    def find_analysis(
        self, plot_id: str, revision: int, fixture_version: str
    ) -> dict[str, Any] | None:
        # Serialize cache creation with the plot row until this transaction ends.
        # This never changes the explicitly requested snapshot revision.
        self.session.scalar(select(PlotRow.id).where(PlotRow.id == str(plot_id)).with_for_update())
        row = self.session.scalar(
            select(AnalysisRow).where(
                AnalysisRow.plot_id == str(plot_id),
                AnalysisRow.plot_revision == revision,
                AnalysisRow.fixture_version == fixture_version,
            )
        )
        return copy.deepcopy(row.payload) if row is not None else None

    def latest_live_analysis(
        self, plot_id: str, revision: int, request_key: str | None = None
    ) -> dict | None:
        query = select(AnalysisRow).where(
            AnalysisRow.plot_id == plot_id,
            AnalysisRow.plot_revision == revision,
            AnalysisRow.payload["synthetic"].as_boolean().is_(False),
        )
        if request_key is not None:
            query = query.where(AnalysisRow.payload["request_key"].as_string() == request_key)
        rows = self.session.scalars(query).all()
        return (
            copy.deepcopy(max(rows, key=lambda r: r.payload.get("created_at", "")).payload)
            if rows
            else None
        )

    def transition_analysis(self, payload: dict) -> None:
        """Only in-progress jobs can transition; terminal snapshots stay immutable."""
        row = self.session.scalar(
            select(AnalysisRow).where(AnalysisRow.id == payload["id"]).with_for_update()
        )
        if row is None:
            raise ValueError("Analysis not found")
        if row.payload.get("status") not in {"pending", "running"}:
            raise ValueError("Terminal analysis snapshots are immutable")
        if (row.plot_id, row.plot_revision) != (payload["plot_id"], payload["plot_revision"]):
            raise ValueError("Analysis identity cannot change")
        row.payload = copy.deepcopy(payload)
        self.session.flush()

    def save_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Advance a saved plan atomically while preserving its plot and project.

        This check closes the race between a service's expected_revision read and
        its write. SQLite ignores FOR UPDATE and is only an offline test backend.
        """
        data = copy.deepcopy(payload)
        plan_id = str(data["id"])
        revision = int(data["revision"])
        row = self.session.scalar(
            select(PlanRow)
            .where(PlanRow.id == plan_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            if revision != 1:
                raise ValueError("A new plan must start at revision 1")
            self.session.add(
                PlanRow(
                    id=plan_id,
                    plot_id=str(data["plot_id"]),
                    evaluation_id=data.get("evaluation_id"),
                    payload=data,
                )
            )
        else:
            if revision != int(row.payload["revision"]) + 1:
                raise ValueError("Plan edits must advance exactly one revision")
            if str(data["plot_id"]) != row.plot_id or data.get("project_id") != row.payload.get(
                "project_id"
            ):
                raise ValueError("A plan cannot be moved to another plot or project")
            row.evaluation_id = data.get("evaluation_id")
            row.payload = data
        self.session.flush()
        return copy.deepcopy(data)

    def save_plot(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = copy.deepcopy(payload)
        plot_id = str(data["id"])
        revision = int(data["revision"])
        row = self.session.scalar(
            select(PlotRow)
            .where(PlotRow.id == plot_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            if revision != 1:
                raise ValueError("A new plot must start at revision 1")
            row = PlotRow(
                id=plot_id,
                project_id=str(data["project_id"]),
                revision=revision,
                payload=data,
            )
            self.session.add(row)
            self.session.flush()
        else:
            if revision != row.revision + 1:
                raise ValueError("Plot edits must advance exactly one revision")
            if str(data["project_id"]) != row.project_id:
                raise ValueError("A plot cannot be moved to another project")
            row.revision = revision
            row.payload = data
        self.session.add(
            PlotRevisionRow(
                plot_id=plot_id,
                revision=revision,
                geometry=json.dumps(data["geometry"], separators=(",", ":")),
                payload=data,
            )
        )
        self.session.flush()
        return copy.deepcopy(data)
