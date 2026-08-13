from pathlib import Path


MIGRATION = Path(__file__).parents[1] / "migrations" / "versions" / "0019_feishu_integration.py"
EVENT_RECEIPT_MIGRATION = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "0038_feishu_event_receipts.py"
)


def test_feishu_migration_is_reversible_and_contains_sync_boundaries() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    for table in (
        "feishu_organization_configs",
        "feishu_oauth_states",
        "feishu_identity_bindings",
        "feishu_interview_syncs",
    ):
        assert f'"{table}"' in source
        assert f'op.drop_table("{table}")' in source
    assert 'server_default=sa.false()' in source
    assert "encrypted_app_secret" in source
    assert "state_hash" in source
    assert "pending_confirmation" in source
    assert 'down_revision = "0018_password_invitations"' in source


def test_feishu_event_receipt_migration_is_reversible_and_on_current_head() -> None:
    source = EVENT_RECEIPT_MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "0038_feishu_event_receipts"' in source
    assert 'down_revision = "0037_fix_offer_approver_status"' in source
    assert '"feishu_event_receipts"' in source
    assert '"uq_feishu_event_receipts_org_event"' in source
    assert 'op.drop_table("feishu_event_receipts")' in source
