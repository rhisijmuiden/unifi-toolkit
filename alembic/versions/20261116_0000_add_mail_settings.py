"""Add mail (SMTP) settings table

Revision ID: d4a8f2c61e57
Revises: c7e1a4f9d2b6
Create Date: 2026-11-16 00:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4a8f2c61e57'
down_revision: Union[str, None] = 'c7e1a4f9d2b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('mail_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('host', sa.String(), nullable=False, server_default=''),
        sa.Column('port', sa.Integer(), nullable=False, server_default='587'),
        sa.Column('security', sa.String(), nullable=False, server_default='starttls'),
        sa.Column('verify_tls', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('username', sa.String(), nullable=True),
        sa.Column('password_encrypted', sa.LargeBinary(), nullable=True),
        sa.Column('from_address', sa.String(), nullable=False, server_default=''),
        sa.Column('from_name', sa.String(), nullable=False, server_default='UniFi Toolkit'),
        sa.Column('reply_to', sa.String(), nullable=True),
        sa.Column('default_recipients', sa.String(), nullable=True),
        sa.Column('timeout', sa.Integer(), nullable=False, server_default='20'),
        sa.CheckConstraint('id = 1', name='mail_single_row_check'),
        sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('mail_settings')
