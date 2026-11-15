"""Add backup settings, locations and history

Revision ID: e9b3c5a17f20
Revises: d4a8f2c61e57
Create Date: 2026-11-17 00:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e9b3c5a17f20'
down_revision: Union[str, None] = 'd4a8f2c61e57'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('backup_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('run_time', sa.String(), nullable=False, server_default='03:00'),
        sa.Column('full_every_days', sa.Integer(), nullable=False, server_default='7'),
        sa.Column('keep_chains', sa.Integer(), nullable=False, server_default='4'),
        sa.Column('passphrase_encrypted', sa.LargeBinary(), nullable=True),
        sa.Column('notify_on_failure', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('last_scheduled_date', sa.String(), nullable=True),
        sa.CheckConstraint('id = 1', name='backup_single_row_check'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_table('backup_destinations',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('host', sa.String(), nullable=False),
        sa.Column('port', sa.Integer(), nullable=False),
        sa.Column('username', sa.String(), nullable=True),
        sa.Column('password_encrypted', sa.LargeBinary(), nullable=True),
        sa.Column('private_key_encrypted', sa.LargeBinary(), nullable=True),
        sa.Column('host_key', sa.Text(), nullable=True),
        sa.Column('verify_tls', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('path', sa.String(), nullable=False, server_default=''),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('last_status', sa.String(), nullable=True),
        sa.Column('last_message', sa.Text(), nullable=True),
        sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_table('backup_runs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('action', sa.String(), nullable=False, server_default='backup'),
        sa.Column('kind', sa.String(), nullable=True),
        sa.Column('trigger', sa.String(), nullable=False, server_default='manual'),
        sa.Column('name', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False, server_default='running'),
        sa.Column('size', sa.Integer(), nullable=True),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('backup_runs')
    op.drop_table('backup_destinations')
    op.drop_table('backup_settings')
