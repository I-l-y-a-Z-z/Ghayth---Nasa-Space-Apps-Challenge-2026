"""Create versioned plot geometry and synthetic result snapshots.

Revision ID: 0001_foundation
Revises: None
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from agrix.db import PostGISPolygon

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    is_postgres = op.get_context().dialect.name == "postgresql"
    if is_postgres:
        op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.create_table(
        "plots",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", _json_type(), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_plot_revision_positive"),
    )
    op.create_index("ix_plots_project_id", "plots", ["project_id"])
    op.create_table(
        "plot_revisions",
        sa.Column("plot_id", sa.String(64), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column(
            "geometry", sa.Text().with_variant(PostGISPolygon(), "postgresql"), nullable=False
        ),
        sa.Column("payload", _json_type(), nullable=False),
        sa.ForeignKeyConstraint(["plot_id"], ["plots.id"], ondelete="CASCADE"),
        sa.CheckConstraint("revision >= 1", name="ck_snapshot_revision_positive"),
    )
    if is_postgres:
        op.create_index(
            "ix_plot_revisions_geometry",
            "plot_revisions",
            ["geometry"],
            postgresql_using="gist",
        )
    op.create_table(
        "analyses",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("plot_id", sa.String(64), nullable=False),
        sa.Column("plot_revision", sa.Integer(), nullable=False),
        sa.Column("fixture_version", sa.String(128), nullable=False),
        sa.Column("payload", _json_type(), nullable=False),
        sa.ForeignKeyConstraint(
            ["plot_id", "plot_revision"],
            ["plot_revisions.plot_id", "plot_revisions.revision"],
        ),
        sa.UniqueConstraint(
            "plot_id", "plot_revision", "fixture_version", name="uq_analysis_revision_fixture"
        ),
        sa.UniqueConstraint("id", "plot_id", "plot_revision", name="uq_analysis_id_plot_revision"),
    )
    op.create_index("ix_analyses_plot_id", "analyses", ["plot_id"])
    op.create_table(
        "evaluations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("plot_id", sa.String(64), nullable=False),
        sa.Column("plot_revision", sa.Integer(), nullable=False),
        sa.Column("analysis_id", sa.String(64), nullable=True),
        sa.Column("payload", _json_type(), nullable=False),
        sa.ForeignKeyConstraint(
            ["plot_id", "plot_revision"],
            ["plot_revisions.plot_id", "plot_revisions.revision"],
        ),
        sa.ForeignKeyConstraint(
            ["analysis_id", "plot_id", "plot_revision"],
            ["analyses.id", "analyses.plot_id", "analyses.plot_revision"],
        ),
        sa.UniqueConstraint("id", "plot_id", name="uq_evaluation_id_plot"),
    )
    op.create_index("ix_evaluations_plot_id", "evaluations", ["plot_id"])
    op.create_table(
        "plans",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("plot_id", sa.String(64), nullable=False),
        sa.Column("evaluation_id", sa.String(64), nullable=True),
        sa.Column("payload", _json_type(), nullable=False),
        sa.ForeignKeyConstraint(["plot_id"], ["plots.id"]),
        sa.ForeignKeyConstraint(
            ["evaluation_id", "plot_id"], ["evaluations.id", "evaluations.plot_id"]
        ),
    )
    op.create_index("ix_plans_plot_id", "plans", ["plot_id"])
    op.create_table(
        "local_records",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("payload", _json_type(), nullable=False),
    )


def downgrade() -> None:
    for table in ("local_records", "plans", "evaluations", "analyses", "plot_revisions", "plots"):
        op.drop_table(table)
    # The extension can be shared with other schemas. Never remove it on downgrade.
