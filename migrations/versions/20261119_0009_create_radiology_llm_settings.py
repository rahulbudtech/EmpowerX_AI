"""create radiology_llm_settings table

Revision ID: 20261119_0009
Revises: 20261104_0008
Create Date: 2026-11-19

Singleton-style settings row for primary + optional fallback LLM selection.
UI/API can override env defaults; runtime prefers DB when a row exists.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261119_0009"
down_revision: Union[str, None] = "20261104_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "radiology_llm_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("primary_provider", sa.String(length=64), nullable=False),
        sa.Column("primary_model", sa.String(length=128), nullable=False),
        sa.Column("fallback_provider", sa.String(length=64), nullable=True),
        sa.Column("fallback_model", sa.String(length=128), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("radiology_llm_settings")
