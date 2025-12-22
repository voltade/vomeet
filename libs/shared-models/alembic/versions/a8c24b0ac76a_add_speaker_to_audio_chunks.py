"""add speaker to audio_chunks

Revision ID: a8c24b0ac76a
Revises: e721bd1ecf00
Create Date: 2025-12-22 14:57:59.905337

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = "a8c24b0ac76a"
down_revision = "e721bd1ecf00"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add speaker column to audio_chunks table
    # Check if column already exists to handle environments that may already have it
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = [col["name"] for col in inspector.get_columns("audio_chunks")]

    if "speaker" not in columns:
        op.add_column("audio_chunks", sa.Column("speaker", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("audio_chunks", "speaker")
