"""users without an email

`email` was NOT NULL and unique, so a second account added without one
collided with the first on the empty string. NULLs do not collide, and an
internal account with no mailbox is ordinary.

Revision ID: a1c4e7b90d2f
Revises: 52f6b0396034
Create Date: 2026-08-01 12:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c4e7b90d2f"
down_revision: str | Sequence[str] | None = "52f6b0396034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        "users",
        "email",
        existing_type=sa.String(length=256),
        nullable=True,
    )
    # Existing blanks become NULL, or they keep colliding with each other.
    op.execute("UPDATE users SET email = NULL WHERE email = ''")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("UPDATE users SET email = '' WHERE email IS NULL")
    op.alter_column(
        "users",
        "email",
        existing_type=sa.String(length=256),
        nullable=False,
    )
