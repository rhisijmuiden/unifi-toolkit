"""Add WAN samples, speed tests and firmware changes

Revision ID: a7c4e9d21b58
Revises: f3d8a2b64c19
Create Date: 2026-11-19 00:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7c4e9d21b58'
down_revision: Union[str, None] = 'f3d8a2b64c19'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('insights_wan_samples',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
        sa.Column('wan', sa.String(), nullable=False),
        sa.Column('up', sa.Boolean(), nullable=False),
        sa.Column('latency_ms', sa.Float(), nullable=True),
        sa.Column('wan_ip', sa.String(), nullable=True),
        sa.Column('isp', sa.String(), nullable=True),
        sa.Column('reason', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_insights_wan_samples_ts', 'insights_wan_samples', ['ts'])
    op.create_table('insights_speedtests',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('run_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('download_mbps', sa.Float(), nullable=True),
        sa.Column('upload_mbps', sa.Float(), nullable=True),
        sa.Column('latency_ms', sa.Float(), nullable=True),
        sa.Column('wan', sa.String(), nullable=True),
        sa.Column('server', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('run_at', name='speedtest_run_at_unique')
    )
    op.create_index('ix_insights_speedtests_run_at', 'insights_speedtests', ['run_at'])
    op.create_table('insights_firmware_changes',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
        sa.Column('mac', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=True),
        sa.Column('model', sa.String(), nullable=True),
        sa.Column('old_version', sa.String(), nullable=True),
        sa.Column('new_version', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_insights_firmware_changes_ts', 'insights_firmware_changes', ['ts'])
    op.create_index('ix_insights_firmware_changes_mac', 'insights_firmware_changes', ['mac'])


def downgrade() -> None:
    op.drop_table('insights_firmware_changes')
    op.drop_table('insights_speedtests')
    op.drop_table('insights_wan_samples')
