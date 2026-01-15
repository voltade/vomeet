"""add_scheduled_times_to_meetings

Revision ID: f237b4131c22
Revises: 5b7590eed3cf
Create Date: 2026-01-15 11:40:19.330661

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f237b4131c22'
down_revision = '5b7590eed3cf'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add scheduled_start_time and scheduled_end_time columns to meetings table
    op.add_column(
        "meetings",
        sa.Column("scheduled_start_time", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "meetings",
        sa.Column("scheduled_end_time", sa.DateTime(), nullable=True),
    )
    # Add indexes for efficient queries
    op.create_index(
        "ix_meetings_scheduled_start_time",
        "meetings",
        ["scheduled_start_time"],
        unique=False,
    )
    op.create_index(
        "ix_meetings_scheduled_end_time",
        "meetings",
        ["scheduled_end_time"],
        unique=False,
    )


def downgrade() -> None:
    # Remove indexes and columns
    op.drop_index("ix_meetings_scheduled_end_time", table_name="meetings")
    op.drop_index("ix_meetings_scheduled_start_time", table_name="meetings")
    op.drop_column("meetings", "scheduled_end_time")
    op.drop_column("meetings", "scheduled_start_time") 