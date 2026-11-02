"""create radiology_token_usage table

Revision ID: 20260429_0006
Revises: 20260424_0005
Create Date: 2026-04-29

One row per radiology finding job (DICOM analyze). Tokens + estimated USD
cost are accumulated across LLM retries for the same finding_id.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260429_0006"
down_revision: Union[str, None] = "20260424_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "radiology_token_usage",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("finding_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_id", sa.String(length=255), nullable=True),
        sa.Column("llm_model", sa.String(length=128), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "llm_calls",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["radiology_findings.id"],
            name="fk_radiology_token_usage_finding_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("finding_id", name="uq_radiology_token_usage_finding_id"),
    )
    op.create_index(
        "ix_radiology_token_usage_case_id",
        "radiology_token_usage",
        ["case_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_radiology_token_usage_case_id",
        table_name="radiology_token_usage",
    )
    op.drop_table("radiology_token_usage")
