"""Add local LaTeX artifact types while retaining legacy DOCX rows.

Revision ID: 0002_latex_artifacts
Revises: 0001_architecture_v2
"""

from alembic import op
from sqlalchemy import CheckConstraint, Column, ForeignKey, Integer, MetaData, Table, Text, text

revision: str = "0002_latex_artifacts"
down_revision: str | None = "0001_architecture_v2"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_LEGACY = (
    "'RESUME_DOCX', 'RESUME_PDF', 'COVER_LETTER_DOCX', 'COVER_LETTER_PDF', "
    "'TRANSCRIPT', 'SCREENSHOT', 'JOB_SNAPSHOT', 'OTHER'"
)
_LATEX = (
    "'RESUME_DOCX', 'RESUME_TEX', 'RESUME_PDF', 'COVER_LETTER_DOCX', "
    "'COVER_LETTER_TEX', 'COVER_LETTER_PDF', 'TRANSCRIPT', 'SCREENSHOT', "
    "'JOB_SNAPSHOT', 'OTHER'"
)


def _artifacts(allowed: str) -> Table:
    metadata = MetaData()
    return Table(
        "artifacts",
        metadata,
        Column("artifact_id", Text, primary_key=True, nullable=False),
        Column("application_id", Text, ForeignKey("application_pipeline.application_id")),
        Column("task_id", Text, ForeignKey("agent_queue.task_id")),
        Column("artifact_type", Text, nullable=False),
        Column("path", Text, nullable=False),
        Column("sha256", Text, nullable=False),
        Column("mime_type", Text),
        Column("version", Integer, nullable=False, server_default=text("1")),
        Column("approved_for_submission", Integer, nullable=False, server_default=text("0")),
        Column("created_at", Text, nullable=False),
        CheckConstraint(f"artifact_type IN ({allowed})"),
        CheckConstraint("approved_for_submission IN (0, 1)"),
    )


def _replace(allowed: str) -> None:
    with op.batch_alter_table(
        "artifacts",
        copy_from=_artifacts(allowed),
        recreate="always",
    ):
        pass


def upgrade() -> None:
    _replace(_LATEX)


def downgrade() -> None:
    _replace(_LEGACY)
