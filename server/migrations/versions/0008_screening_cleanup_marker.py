"""screening quarantine cleanup marker"""
from alembic import op
import sqlalchemy as sa
revision="0008_screening_cleanup_marker"; down_revision="0007_file_quarantine"; branch_labels=None; depends_on=None
def upgrade(): op.add_column("file_objects",sa.Column("quarantine_cleanup_key",sa.String(512),nullable=True))
def downgrade(): op.drop_column("file_objects","quarantine_cleanup_key")
