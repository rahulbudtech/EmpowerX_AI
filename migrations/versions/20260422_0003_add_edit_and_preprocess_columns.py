"""add edited_report, edited_at, preprocess columns (schema only)

Revision ID: 20260422_0003
Revises: 20260422_0002
Create Date: 2026-04-22

Provisional columns for specialist edits + preprocess inputs (S3 image URL).
Application logic / APIs not wired yet — product schema still TBD.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260422_0003"
down_revision: Union[str, None] = "20260422_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "radiology_findings",
        sa.Column("edited_report", postgresql.JSON(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "radiology_findings",
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "radiology_findings",
        sa.Column("preprocess_metadata", postgresql.JSON(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "radiology_findings",
        sa.Column("preprocess_image_url", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("radiology_findings", "preprocess_image_url")
    op.drop_column("radiology_findings", "preprocess_metadata")
    op.drop_column("radiology_findings", "edited_at")
    op.drop_column("radiology_findings", "edited_report")
