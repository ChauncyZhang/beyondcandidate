from pathlib import Path


MIGRATION = Path(__file__).parents[1] / "migrations" / "versions" / "0019_feishu_integration.py"


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
