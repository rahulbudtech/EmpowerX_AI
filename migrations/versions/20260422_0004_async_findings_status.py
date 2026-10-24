"""nullable report + queued/processing statuses for Celery jobs

Revision ID: 20260422_0004
Revises: 20260422_0003
Create Date: 2026-04-22
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260422_0004"
down_revision: Union[str, None] = "20260422_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "radiology_findings",
        "report",
        existing_type=postgresql.JSON(astext_type=sa.Text()),
        nullable=True,
    )


def downgrade() -> None:
    # Fail downgrade if any null reports exist — fill or delete first.
    op.alter_column(
        "radiology_findings",
        "report",
        existing_type=postgresql.JSON(astext_type=sa.Text()),
        nullable=False,
    )
