"""rename processing_duration_ms to processing_duration_seconds

Revision ID: 20261104_0008
Revises: 20261104_0007
Create Date: 2026-11-04

Store end-to-end job duration as whole seconds instead of milliseconds.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "20261104_0008"
down_revision: Union[str, None] = "20261104_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "radiology_findings",
        "processing_duration_ms",
        new_column_name="processing_duration_seconds",
    )


def downgrade() -> None:
    op.alter_column(
        "radiology_findings",
        "processing_duration_seconds",
        new_column_name="processing_duration_ms",
    )
