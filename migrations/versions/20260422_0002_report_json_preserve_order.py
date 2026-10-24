"""use JSON for report column to preserve key order

Revision ID: 20260422_0002
Revises: 20260422_0001
Create Date: 2026-04-22

PostgreSQL JSONB does not preserve object key order. Switch report to JSON
so stored payloads keep:
  technical_metadata → findings → clinical_impression → recommendations
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260422_0002"
down_revision: Union[str, None] = "20260422_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "radiology_findings",
        "report",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        type_=postgresql.JSON(astext_type=sa.Text()),
        existing_nullable=False,
        postgresql_using="report::json",
    )


def downgrade() -> None:
    op.alter_column(
        "radiology_findings",
        "report",
        existing_type=postgresql.JSON(astext_type=sa.Text()),
        type_=postgresql.JSONB(astext_type=sa.Text()),
        existing_nullable=False,
        postgresql_using="report::jsonb",
    )
