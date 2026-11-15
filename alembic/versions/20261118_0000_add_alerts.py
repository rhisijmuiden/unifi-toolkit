"""Add alert settings, known devices and the alert log

Revision ID: f3d8a2b64c19
Revises: e9b3c5a17f20
Create Date: 2026-11-18 00:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f3d8a2b64c19'
down_revision: Union[str, None] = 'e9b3c5a17f20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('alert_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('recipients', sa.String(), nullable=True),
        sa.Column('unknown_device_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('threat_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('threat_min_severity', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('console_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('report_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('report_weekday', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('report_time', sa.String(), nullable=False, server_default='08:00'),
        sa.Column('devices_seeded', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('last_threat_id', sa.Integer(), nullable=True),
        sa.Column('last_report_date', sa.String(), nullable=True),
        sa.Column('console_down_since', sa.DateTime(timezone=True), nullable=True),
        sa.Column('console_down_alerted', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint('id = 1', name='alert_single_row_check'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_table('known_devices',
        sa.Column('mac', sa.String(), nullable=False),
        sa.Column('first_seen', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen', sa.DateTime(timezone=True), nullable=False),
        sa.Column('name', sa.String(), nullable=True),
        sa.Column('hostname', sa.String(), nullable=True),
        sa.Column('vendor', sa.String(), nullable=True),
        sa.Column('ip', sa.String(), nullable=True),
        sa.Column('network', sa.String(), nullable=True),
        sa.Column('essid', sa.String(), nullable=True),
        sa.Column('is_wired', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('baseline', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('approved', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.PrimaryKeyConstraint('mac')
    )
    op.create_table('alert_log',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('subject', sa.String(), nullable=False),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('sent', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('error', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('alert_log')
    op.drop_table('known_devices')
    op.drop_table('alert_settings')
