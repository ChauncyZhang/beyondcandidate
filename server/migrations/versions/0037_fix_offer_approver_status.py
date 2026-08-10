"""Align Offer approver triggers with persisted user status values.

Revision ID: 0037_fix_offer_approver_status
Revises: 0036_email_sender_identity
"""

from alembic import op


revision = "0037_fix_offer_approver_status"
down_revision = "0036_email_sender_identity"
branch_labels = None
depends_on = None


def _replace_functions(status_predicate: str) -> None:
    op.execute(f"""
        CREATE OR REPLACE FUNCTION validate_special_offer_approver() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM users u
            WHERE u.organization_id = NEW.organization_id
              AND u.id = NEW.approver_id
              AND {status_predicate}
              AND EXISTS (
                SELECT 1 FROM user_roles ur
                WHERE ur.user_id = u.id
                  AND ur.role IN ('recruiting_admin','hiring_manager')
              )
          ) THEN
            RAISE EXCEPTION 'special offer approver is not eligible';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute(f"""
        CREATE OR REPLACE FUNCTION validate_job_offer_defaults() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.offer_approver_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM users u
            WHERE u.organization_id = NEW.organization_id
              AND u.id = NEW.offer_approver_id
              AND {status_predicate}
              AND EXISTS (
                SELECT 1 FROM user_roles ur
                WHERE ur.user_id = u.id
                  AND ur.role IN ('recruiting_admin','hiring_manager')
              )
          ) THEN
            RAISE EXCEPTION 'job offer approver is not eligible';
          END IF;
          IF NEW.offer_template_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM offer_templates t
            WHERE t.organization_id = NEW.organization_id
              AND t.id = NEW.offer_template_id
              AND t.status = 'active'
          ) THEN
            RAISE EXCEPTION 'job offer template is not active';
          END IF;
          RETURN NEW;
        END $$
    """)


def upgrade() -> None:
    _replace_functions("lower(u.status) = 'active'")


def downgrade() -> None:
    _replace_functions("u.status = 'active'")
