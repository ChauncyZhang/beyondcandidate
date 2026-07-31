"""Allow image resumes in quarantined file objects."""

from alembic import op


revision = "0028_image_resume_support"
down_revision = "0027_recruiting_user_scopes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_file_objects_detected_type", "file_objects", type_="check")
    op.create_check_constraint(
        "ck_file_objects_detected_type",
        "file_objects",
        "detected_type is null or detected_type in ('pdf','docx','txt','image')",
    )


def downgrade() -> None:
    op.execute("UPDATE file_objects SET detected_type = NULL WHERE detected_type = 'image'")
    op.drop_constraint("ck_file_objects_detected_type", "file_objects", type_="check")
    op.create_check_constraint(
        "ck_file_objects_detected_type",
        "file_objects",
        "detected_type is null or detected_type in ('pdf','docx','txt')",
    )
