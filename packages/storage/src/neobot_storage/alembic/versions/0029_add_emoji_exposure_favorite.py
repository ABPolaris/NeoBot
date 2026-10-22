"""表情包新增曝光分与爱用标记

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-22

表情包选择改成「爱用固定候选 + 随机槽位」后需要两种状态：

- exposure_score：进入候选池但没被发送时累加（每次 0.5），只用于排序，
  真实发送次数仍记在 use_count —— 分开记才不会把「候选过但不合适」
  误当成「用过」。
- is_favorite：爱用表情包每轮固定进入候选池，与随机槽位分开计数。

两列都有 server_default，既保证旧库里的已有行拿到 0 / false，也保证
不显式赋值的新行走默认值；downgrade 只删列，emojis 既有数据不受影响。
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "emojis",
        sa.Column("exposure_score", sa.Float(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "emojis",
        sa.Column("is_favorite", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("emojis", "is_favorite")
    op.drop_column("emojis", "exposure_score")
