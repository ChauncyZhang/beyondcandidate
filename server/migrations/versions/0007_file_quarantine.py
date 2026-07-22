"""file quarantine metadata"""
from alembic import op
import sqlalchemy as sa
revision="0007_file_quarantine"; down_revision="0006_screening_foundation"; branch_labels=None; depends_on=None
def upgrade():
    op.add_column("file_objects",sa.Column("storage_state",sa.String(20),nullable=False,server_default="clean")); op.add_column("file_objects",sa.Column("detected_type",sa.String(20))); op.add_column("file_objects",sa.Column("scan_status",sa.String(20),nullable=False,server_default="clean")); op.create_check_constraint("ck_file_objects_storage_state","file_objects","storage_state in ('quarantine','clean','rejected','deleted')"); op.create_check_constraint("ck_file_objects_scan_status","file_objects","scan_status in ('pending','clean','rejected','failed')"); op.create_check_constraint("ck_file_objects_detected_type","file_objects","detected_type is null or detected_type in ('pdf','docx','txt')"); op.create_index("ix_file_objects_tenant_sha","file_objects",["organization_id","sha256"])
def downgrade():
    op.drop_index("ix_file_objects_tenant_sha",table_name="file_objects"); op.drop_constraint("ck_file_objects_detected_type","file_objects",type_="check"); op.drop_constraint("ck_file_objects_scan_status","file_objects",type_="check"); op.drop_constraint("ck_file_objects_storage_state","file_objects",type_="check"); op.drop_column("file_objects","scan_status"); op.drop_column("file_objects","detected_type"); op.drop_column("file_objects","storage_state")
