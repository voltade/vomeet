"""add_push_notification_channels_to_google_integration

Revision ID: 5b7590eed3cf
Revises: 9063af59d4e3
Create Date: 2026-01-12 14:26:22.861344

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "5b7590eed3cf"
down_revision = "9063af59d4e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add push notification channel fields to account_user_google_integrations
    op.add_column("account_user_google_integrations", sa.Column("channel_id", sa.String(length=64), nullable=True))
    op.add_column("account_user_google_integrations", sa.Column("channel_token", sa.String(length=256), nullable=True))
    op.add_column("account_user_google_integrations", sa.Column("resource_id", sa.String(length=255), nullable=True))
    op.add_column("account_user_google_integrations", sa.Column("channel_expires_at", sa.DateTime(), nullable=True))
    op.add_column("account_user_google_integrations", sa.Column("sync_token", sa.Text(), nullable=True))


def downgrade() -> None:
    # Remove push notification channel fields
    op.drop_column("account_user_google_integrations", "sync_token")
    op.drop_column("account_user_google_integrations", "channel_expires_at")
    op.drop_column("account_user_google_integrations", "resource_id")
    op.drop_column("account_user_google_integrations", "channel_token")
    op.drop_column("account_user_google_integrations", "channel_id")
