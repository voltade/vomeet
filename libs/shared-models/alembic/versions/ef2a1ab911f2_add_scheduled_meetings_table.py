"""add_scheduled_meetings_table

Revision ID: ef2a1ab911f2
Revises: f237b4131c22
Create Date: 2026-01-15 16:59:09.889941

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision = "ef2a1ab911f2"
down_revision = "f237b4131c22"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create scheduled_meetings table
    op.create_table(
        "scheduled_meetings",
        sa.Column("id", sa.Integer(), nullable=False),
        # User/Account context
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("account_user_id", sa.Integer(), nullable=True),  # NULL for ad-hoc
        sa.Column("integration_id", sa.Integer(), nullable=True),  # NULL for ad-hoc
        # Calendar event identifiers
        sa.Column("calendar_event_id", sa.String(length=255), nullable=True),  # NULL for ad-hoc
        sa.Column("calendar_provider", sa.String(length=50), nullable=False, server_default="api"),
        # Meeting details
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("platform", sa.String(length=100), nullable=False),
        sa.Column("native_meeting_id", sa.String(length=255), nullable=False),
        sa.Column("meeting_url", sa.Text(), nullable=True),
        # Schedule
        sa.Column("scheduled_start_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_end_time", sa.DateTime(timezone=True), nullable=True),
        # Organizer info
        sa.Column("is_creator_self", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_organizer_self", sa.Boolean(), nullable=False, server_default="false"),
        # Status tracking
        sa.Column("status", sa.String(length=50), nullable=False, server_default="scheduled"),
        # Metadata
        sa.Column("attendees", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # Timestamps
        sa.Column("last_synced_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        # Primary key
        sa.PrimaryKeyConstraint("id"),
        # Foreign keys
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_user_id"], ["account_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["integration_id"], ["account_user_google_integrations.id"], ondelete="CASCADE"),
    )

    # Create indexes
    op.create_index(op.f("ix_scheduled_meetings_id"), "scheduled_meetings", ["id"], unique=False)
    op.create_index(op.f("ix_scheduled_meetings_account_id"), "scheduled_meetings", ["account_id"], unique=False)
    op.create_index(
        op.f("ix_scheduled_meetings_account_user_id"), "scheduled_meetings", ["account_user_id"], unique=False
    )
    op.create_index(
        op.f("ix_scheduled_meetings_integration_id"), "scheduled_meetings", ["integration_id"], unique=False
    )
    op.create_index(
        op.f("ix_scheduled_meetings_scheduled_start_time"), "scheduled_meetings", ["scheduled_start_time"], unique=False
    )
    op.create_index(op.f("ix_scheduled_meetings_status"), "scheduled_meetings", ["status"], unique=False)

    # Composite indexes for efficient queries
    op.create_index(
        "ix_scheduled_meeting_spawn",
        "scheduled_meetings",
        ["status", "scheduled_start_time"],
        unique=False,
    )
    op.create_index(
        "ix_scheduled_meeting_account",
        "scheduled_meetings",
        ["account_id", "scheduled_start_time"],
        unique=False,
    )
    # Index for duplicate detection (ad-hoc meetings)
    op.create_index(
        "ix_scheduled_meeting_platform_native",
        "scheduled_meetings",
        ["account_id", "platform", "native_meeting_id", "status"],
        unique=False,
    )

    # Unique constraint: one calendar event per integration (for calendar-synced meetings)
    op.create_unique_constraint(
        "_scheduled_meeting_event_uc",
        "scheduled_meetings",
        ["integration_id", "calendar_event_id"],
    )

    # Add scheduled_meeting_id column to meetings table
    op.add_column(
        "meetings",
        sa.Column("scheduled_meeting_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        op.f("ix_meetings_scheduled_meeting_id"),
        "meetings",
        ["scheduled_meeting_id"],
        unique=False,
    )
    op.create_index(
        "ix_meeting_scheduled_meeting_status",
        "meetings",
        ["scheduled_meeting_id", "status"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_meetings_scheduled_meeting_id",
        "meetings",
        "scheduled_meetings",
        ["scheduled_meeting_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ============================================
    # DATA MIGRATION: Migrate existing meetings to scheduled_meetings
    # ============================================
    # Only migrate meetings that have account_id, platform, and platform_specific_id
    # These are treated as ad-hoc meetings (calendar_provider='api')
    conn = op.get_bind()

    # Map Meeting status to ScheduledMeeting status
    # Meeting statuses: requested, joining, awaiting_admission, active, completed, failed, stopping
    # ScheduledMeeting statuses: scheduled, bot_requested, bot_active, completed, cancelled
    # Insert scheduled_meetings and capture the IDs for linking
    conn.execute(
        text("""
        WITH inserted AS (
            INSERT INTO scheduled_meetings (
                account_id,
                account_user_id,
                integration_id,
                calendar_event_id,
                calendar_provider,
                title,
                platform,
                native_meeting_id,
                scheduled_start_time,
                scheduled_end_time,
                status,
                data,
                created_at,
                updated_at
            )
            SELECT 
                m.account_id,
                NULL as account_user_id,
                NULL as integration_id,
                NULL as calendar_event_id,
                'api' as calendar_provider,
                COALESCE(m.data->>'title', 'Ad-hoc Meeting') as title,
                m.platform,
                m.platform_specific_id as native_meeting_id,
                m.scheduled_start_time,
                m.scheduled_end_time,
                CASE 
                    WHEN m.status IN ('completed', 'failed') THEN 'completed'
                    WHEN m.status IN ('active', 'joining', 'awaiting_admission', 'stopping') THEN 'bot_active'
                    WHEN m.status = 'requested' THEN 'bot_requested'
                    ELSE 'scheduled'
                END as status,
                jsonb_build_object('migrated_from_meeting_id', m.id) || COALESCE(m.data, '{}'::jsonb) as data,
                m.created_at,
                COALESCE(m.updated_at, m.created_at)
            FROM meetings m
            WHERE m.account_id IS NOT NULL 
              AND m.platform IS NOT NULL 
              AND m.platform_specific_id IS NOT NULL
            RETURNING id, (data->>'migrated_from_meeting_id')::int as meeting_id
        )
        UPDATE meetings m
        SET scheduled_meeting_id = inserted.id
        FROM inserted
        WHERE m.id = inserted.meeting_id
    """)
    )


def downgrade() -> None:
    # Remove foreign key and column from meetings
    op.drop_constraint("fk_meetings_scheduled_meeting_id", "meetings", type_="foreignkey")
    op.drop_index("ix_meeting_scheduled_meeting_status", table_name="meetings")
    op.drop_index(op.f("ix_meetings_scheduled_meeting_id"), table_name="meetings")
    op.drop_column("meetings", "scheduled_meeting_id")

    # Drop indexes and constraints
    op.drop_constraint("_scheduled_meeting_event_uc", "scheduled_meetings", type_="unique")
    op.drop_index("ix_scheduled_meeting_platform_native", table_name="scheduled_meetings")
    op.drop_index("ix_scheduled_meeting_account", table_name="scheduled_meetings")
    op.drop_index("ix_scheduled_meeting_spawn", table_name="scheduled_meetings")
    op.drop_index(op.f("ix_scheduled_meetings_status"), table_name="scheduled_meetings")
    op.drop_index(op.f("ix_scheduled_meetings_scheduled_start_time"), table_name="scheduled_meetings")
    op.drop_index(op.f("ix_scheduled_meetings_integration_id"), table_name="scheduled_meetings")
    op.drop_index(op.f("ix_scheduled_meetings_account_user_id"), table_name="scheduled_meetings")
    op.drop_index(op.f("ix_scheduled_meetings_account_id"), table_name="scheduled_meetings")
    op.drop_index(op.f("ix_scheduled_meetings_id"), table_name="scheduled_meetings")

    # Drop table
    op.drop_table("scheduled_meetings")
