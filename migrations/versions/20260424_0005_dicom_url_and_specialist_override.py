"""add dicom_file_url and specialist_override_text (schema only)

Revision ID: 20260424_0005
Revises: 20260422_0004
Create Date: 2026-04-24

- dicom_file_url: S3 (or local) link to the source DICOM
- specialist_override_text: free-text when specialist replaces full AI findings

Application logic / APIs not wired yet.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260424_0005"
down_revision: Union[str, None] = "20260422_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "radiology_findings",
        sa.Column("dicom_file_url", sa.Text(), nullable=True),
    )
    op.add_column(
        "radiology_findings",
        sa.Column("specialist_override_text", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("radiology_findings", "specialist_override_text")
    op.drop_column("radiology_findings", "dicom_file_url")
