"""add processing_duration_ms to radiology_findings

Revision ID: 20261104_0007
Revises: 20260429_0006
Create Date: 2026-11-04

Wall-clock ms from job enqueue (created_at / queued) until the row reaches
a terminal status (completed or failed). Includes queue wait, preprocess,
LLM, and any Celery retries.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261104_0007"
down_revision: Union[str, None] = "20260429_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "radiology_findings",
        sa.Column("processing_duration_ms", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("radiology_findings", "processing_duration_ms")
