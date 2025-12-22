"""add_audio_chunks_table

Revision ID: e721bd1ecf00
Revises: 5befe308fa8b
Create Date: 2025-12-22 13:37:27.891193

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "e721bd1ecf00"
down_revision = "5befe308fa8b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create audio_chunks table for chunk-based transcription storage
    op.create_table(
        "audio_chunks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("meeting_id", sa.Integer(), nullable=False),
        sa.Column("session_uid", sa.String(), nullable=True),
        sa.Column("audio_key", sa.String(512), nullable=False),  # R2 key: {session_uid}/{timestamp}-{chunk_index}.raw
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_timestamp", sa.BigInteger(), nullable=False),  # Unix timestamp ms
        sa.Column("duration", sa.Float(), nullable=True),  # Duration of audio in seconds
        sa.Column("full_text", sa.Text(), nullable=True),  # Full transcription text
        sa.Column(
            "segments", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),  # Array of {start, end, text, ...}
        sa.Column("language", sa.String(10), nullable=True),
        sa.Column("language_probability", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    # Create indexes
    op.create_index("ix_audio_chunks_id", "audio_chunks", ["id"])
    op.create_index("ix_audio_chunks_meeting_id", "audio_chunks", ["meeting_id"])
    op.create_index("ix_audio_chunks_session_uid", "audio_chunks", ["session_uid"])
    op.create_index("ix_audio_chunks_meeting_chunk", "audio_chunks", ["meeting_id", "chunk_index"])

    # Unique constraint on audio_key to prevent duplicate chunks (idempotent webhook)
    op.create_unique_constraint("uq_audio_chunks_audio_key", "audio_chunks", ["audio_key"])


def downgrade() -> None:
    op.drop_constraint("uq_audio_chunks_audio_key", "audio_chunks", type_="unique")
    op.drop_index("ix_audio_chunks_meeting_chunk", table_name="audio_chunks")
    op.drop_index("ix_audio_chunks_session_uid", table_name="audio_chunks")
    op.drop_index("ix_audio_chunks_meeting_id", table_name="audio_chunks")
    op.drop_index("ix_audio_chunks_id", table_name="audio_chunks")
    op.drop_table("audio_chunks")
