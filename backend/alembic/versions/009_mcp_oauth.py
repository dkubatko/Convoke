"""OAuth support for MCP servers

Revision ID: 009
Revises: 008
Create Date: 2026-07-03

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "009"
down_revision: str | None = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = [
    # plain string server_default is quoted by SQLAlchemy — do not pre-quote
    ("auth_type", sa.Text(), "none"),
    ("oauth_status", sa.Text(), None),
    ("oauth_error", sa.Text(), None),
    ("oauth_client_id", sa.Text(), None),
    ("oauth_client_secret_encrypted", sa.Text(), None),
    ("oauth_authorization_endpoint", sa.Text(), None),
    ("oauth_token_endpoint", sa.Text(), None),
    ("oauth_scopes", sa.Text(), None),
    ("oauth_resource", sa.Text(), None),
    ("oauth_state", sa.Text(), None),
    ("oauth_pkce_verifier_encrypted", sa.Text(), None),
    ("oauth_access_token_encrypted", sa.Text(), None),
    ("oauth_refresh_token_encrypted", sa.Text(), None),
]


def upgrade() -> None:
    for name, type_, default in COLUMNS:
        op.add_column(
            "mcp_servers",
            sa.Column(name, type_, nullable=(name != "auth_type"), server_default=default),
        )
    op.add_column("mcp_servers", sa.Column("oauth_expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("mcp_servers", "oauth_expires_at")
    for name, _, _ in reversed(COLUMNS):
        op.drop_column("mcp_servers", name)
