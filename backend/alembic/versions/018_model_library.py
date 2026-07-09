"""Model library: id-keyed connected models + role assignments

The role-keyed model_providers table becomes a many-row library
(connected_models, with probed capability flags) plus a role → model mapping
(model_role_assignments). Existing intent/agent configs are copied over —
deduped when they share an endpoint — so operators need no action. The
unconsumed 'embeddings' row is dropped: chat memory embeds locally by design.

Revision ID: 018
Revises: 017
Create Date: 2026-07-05

"""
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "018"
down_revision: str | None = "017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "connected_models",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("capabilities", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "model_role_assignments",
        sa.Column("role", sa.Text(), primary_key=True),
        sa.Column(
            "model_id",
            sa.Integer(),
            sa.ForeignKey("connected_models.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    # Carry over existing role configs: one library entry per distinct
    # endpoint, assigned to the role(s) that used it. Skipped under
    # `alembic upgrade --sql`: offline mode can't read rows (and a fresh
    # database rendered offline has none to copy anyway).
    if context.is_offline_mode():
        op.drop_table("model_providers")
        return
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT role, base_url, model_name, api_key_encrypted FROM model_providers "
            "WHERE role IN ('intent', 'agent') ORDER BY role"
        )
    ).fetchall()
    by_endpoint: dict[tuple, int] = {}
    used_names: set[str] = set()
    for role, base_url, model_name, key_enc in rows:
        endpoint = (base_url, model_name, key_enc)
        model_id = by_endpoint.get(endpoint)
        if model_id is None:
            name = model_name if model_name not in used_names else f"{model_name} ({role})"
            used_names.add(name)
            model_id = conn.execute(
                sa.text(
                    "INSERT INTO connected_models "
                    "(name, base_url, model_name, api_key_encrypted, capabilities) "
                    "VALUES (:name, :base_url, :model_name, :key, :caps) RETURNING id"
                ),
                {
                    "name": name,
                    "base_url": base_url,
                    "model_name": model_name,
                    "key": key_enc,
                    "caps": json.dumps({"chat": True}),
                },
            ).scalar_one()
            by_endpoint[endpoint] = model_id
        conn.execute(
            sa.text(
                "INSERT INTO model_role_assignments (role, model_id) VALUES (:role, :model_id)"
            ),
            {"role": role, "model_id": model_id},
        )

    op.drop_table("model_providers")


def downgrade() -> None:
    op.create_table(
        "model_providers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("role", sa.Text(), nullable=False, unique=True),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # Same offline-mode caveat as upgrade: the copy-back is a plain INSERT ..
    # SELECT, which does render offline, so no guard is needed here.
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO model_providers (role, base_url, model_name, api_key_encrypted) "
            "SELECT a.role, m.base_url, m.model_name, m.api_key_encrypted "
            "FROM model_role_assignments a JOIN connected_models m ON m.id = a.model_id "
            "WHERE a.role IN ('intent', 'agent')"
        )
    )
    op.drop_table("model_role_assignments")
    op.drop_table("connected_models")
