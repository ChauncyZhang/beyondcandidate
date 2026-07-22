import pytest
from pydantic import ValidationError
from pydantic import SecretStr

from server.app.core.settings import Settings
from server.app.main import _contact_lookup_key
from server.app.recruiting.security import ContactCipher


def production_settings(**overrides: object) -> Settings:
    values = {
        "environment": "production",
        "database_url": "postgresql+asyncpg://app:S3cureValue%21@postgres/app",
        "object_storage_endpoint": "minio:9000",
        "object_storage_access_key": "real-access-key",
        "object_storage_secret_key": "real-secret-key",
        "object_storage_bucket": "resumes",
        "contact_encryption_key": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
        "contact_lookup_secret": "ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8=",
        "llm_config_encryption_key": "QEFCQ0RFRkdISUpLTE1OT1BRUlNUVVZXWFlaW1xdXl8=",
        "feishu_config_encryption_key": "YGFiY2RlZmdoaWprbG1ub3BxcnN0dXZ3eHl6e3x9fn8=",
        "cors_origins": ["https://hr.example.com"],
    }
    values.update(overrides)
    return Settings(**values)


def test_default_organization_identity_loads_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("DEFAULT_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("DEFAULT_ORGANIZATION_NAME", "Acme Recruiting")

    settings = Settings.from_environment()

    assert settings.default_organization_slug == "acme"
    assert settings.default_organization_name == "Acme Recruiting"


@pytest.mark.parametrize(
    "overrides",
    [
        {"default_organization_slug": "acme"},
        {"default_organization_name": "Acme Recruiting"},
        {
            "default_organization_slug": "Acme",
            "default_organization_name": "Acme Recruiting",
        },
        {
            "default_organization_slug": "acme",
            "default_organization_name": "   ",
        },
    ],
)
def test_default_organization_identity_rejects_partial_or_invalid_config(
    overrides: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        Settings(**overrides)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_url", "postgresql+asyncpg://app:change-me@postgres/app"),
        ("object_storage_access_key", "change-me"),
        ("object_storage_secret_key", "placeholder"),
    ],
)
def test_production_rejects_placeholder_secrets(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        production_settings(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_url", "postgresql+asyncpg://app:replace-me-prod@postgres/app"),
        ("object_storage_access_key", "change-me-production-access"),
        ("object_storage_secret_key", "placeholder-production-secret"),
    ],
)
def test_production_rejects_prefixed_example_credentials_without_leak(
    field: str, value: str
) -> None:
    with pytest.raises(ValidationError) as error:
        production_settings(**{field: value})

    assert value not in str(error.value)


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+asyncpg://app@postgres/app",
        "postgresql+asyncpg://app:@postgres/app",
    ],
)
def test_production_rejects_database_url_without_password(database_url: str) -> None:
    with pytest.raises(ValidationError):
        production_settings(database_url=database_url)


@pytest.mark.parametrize(
    "password",
    [
        "secret",
        "safe-secret-value",
        "password",
        "safe-password-value",
        "change-me",
        "safe-change%2Dme-value",
        "example",
        "%73ecret",
    ],
)
def test_production_rejects_decoded_placeholder_database_password(
    password: str,
) -> None:
    with pytest.raises(ValidationError):
        production_settings(
            database_url=f"postgresql+asyncpg://app:{password}@postgres/app"
        )


def test_production_rejects_wildcard_cors() -> None:
    with pytest.raises(ValidationError):
        production_settings(cors_origins=["*"])


def test_production_rejects_insecure_cors_origin() -> None:
    with pytest.raises(ValidationError):
        production_settings(cors_origins=["http://hr.example.com"])


def test_production_accepts_explicit_origins_and_non_placeholder_secrets() -> None:
    settings = production_settings()

    assert settings.environment == "production"
    assert settings.cors_origins == ["https://hr.example.com"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("contact_encryption_key", "change-me"), ("contact_lookup_secret", "placeholder"), ("llm_config_encryption_key", "change-me"), ("feishu_config_encryption_key", "change-me")],
)
def test_production_rejects_placeholder_contact_secrets(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        production_settings(**{field: value})


def test_production_accepts_deployment_supplied_contact_secrets() -> None:
    settings = production_settings()
    assert isinstance(settings.contact_encryption_key, SecretStr)
    assert isinstance(settings.feishu_config_encryption_key, SecretStr)
    assert "AAECAw" not in repr(settings)


def test_production_contact_secrets_initialize_contact_cipher() -> None:
    settings = production_settings()

    cipher = ContactCipher(
        settings.contact_encryption_key.get_secret_value().encode(),
        _contact_lookup_key(settings.contact_lookup_secret.get_secret_value()),
    )

    assert cipher.protect("email", "candidate@example.com").masked_value == "c***@example.com"


@pytest.mark.parametrize("value", ["short", "not base64!", "+" * 43 + "=", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=", "Y2hhbmdlLW1l", "MDEyMzQ1Njc4OWFiY2RlZg=="])
def test_production_rejects_malformed_or_short_contact_keys(value: str) -> None:
    with pytest.raises(ValidationError):
        production_settings(contact_encryption_key=value)


def test_production_rejects_equal_contact_keys() -> None:
    key = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
    with pytest.raises(ValidationError):
        production_settings(contact_encryption_key=key, contact_lookup_secret=key)


def governance_settings(**overrides: object):
    from server.app.core.settings import GovernanceSettings

    values = {
        "environment": "production",
        "database_url": "postgresql+psycopg://ux09_governance:Gov-9f3c7a@postgres/ux09",
        "delete_access_key": "governance-delete-7f3a",
        "delete_secret_key": "delete-secret-8e4b6c",
        "resume_bucket": "resumes",
        "resume_prefix": "clean/",
        "object_storage_bucket": "resumes",
        "export_bucket": "resumes",
        "export_prefix": "exports/",
        "ledger_access_key": "governance-ledger-4a8d",
        "ledger_secret_key": "ledger-secret-2f7c9e",
        "ledger_bucket": "governance-ledger",
        "ledger_prefix": "deletions/",
        "signing_key": "signing-key-c6b8f2d4e9a7-4f1c8a2d",
    }
    values.update(overrides)
    return GovernanceSettings(**values)


def test_governance_settings_keep_every_secret_out_of_repr() -> None:
    settings = governance_settings()

    rendered = repr(settings)
    for secret in (
        "Gov-9f3c7a",
        "delete-secret-8e4b6c",
        "ledger-secret-2f7c9e",
        "signing-key-c6b8f2d4e9a7-4f1c8a2d",
    ):
        assert secret not in rendered


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_url", "postgresql+psycopg://ux09_governance:change-me@postgres/ux09"),
        ("delete_access_key", "change-me"),
        ("delete_secret_key", "placeholder"),
        ("ledger_access_key", "example"),
        ("ledger_secret_key", "password"),
        ("signing_key", "secret"),
    ],
)
def test_production_governance_settings_reject_missing_or_placeholder_credentials(
    field: str, value: str
) -> None:
    with pytest.raises(ValidationError):
        governance_settings(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_url", "postgresql+psycopg://ux09_governance:change-me-prod@postgres/ux09"),
        ("delete_access_key", "placeholder-delete-access"),
        ("delete_secret_key", "replace-me-delete-secret"),
        ("ledger_access_key", "change-me-ledger-access"),
        ("ledger_secret_key", "placeholder-ledger-secret"),
        ("signing_key", "replace-me-signing-key-with-padding-123456"),
    ],
)
def test_production_governance_settings_reject_prefixed_example_credentials_without_leak(
    field: str, value: str
) -> None:
    with pytest.raises(ValidationError) as error:
        governance_settings(**{field: value})

    assert value not in str(error.value)


def test_governance_settings_reject_shared_credentials() -> None:
    settings = governance_settings()

    with pytest.raises(ValidationError):
        governance_settings(ledger_access_key=settings.delete_access_key)
    with pytest.raises(ValidationError):
        governance_settings(
            ledger_secret_key=settings.delete_secret_key.get_secret_value()
        )
    with pytest.raises(ValidationError):
        governance_settings(
            signing_key=settings.delete_secret_key.get_secret_value()
        )


@pytest.mark.parametrize(
    "value",
    [
        "x" * 31,
        " " * 40,
        "a" * 64,
        "change-me-signing-key-with-padding-1234",
        "valid-looking-signing-key-with-space 123456",
    ],
)
def test_production_governance_settings_reject_weak_signing_keys(value: str) -> None:
    with pytest.raises(ValidationError):
        governance_settings(signing_key=value)


def test_governance_export_defaults_match_report_storage_contract() -> None:
    from server.app.core.settings import GovernanceSettings

    assert GovernanceSettings.model_fields["export_bucket"].default == "resumes"
    assert GovernanceSettings.model_fields["export_prefix"].default == "exports/"
    assert GovernanceSettings.model_fields["retention_sweep_batch_size"].default == 100
    assert GovernanceSettings.model_fields["recovery_max_ledgers"].default == 10_000


@pytest.mark.parametrize(
    "overrides",
    [
        {"retention_sweep_batch_size": 0},
        {"retention_sweep_batch_size": 1001},
        {"recovery_max_ledgers": 0},
        {"recovery_max_ledgers": 100_001},
    ],
)
def test_governance_operation_bounds_fail_closed(overrides) -> None:
    with pytest.raises(ValidationError):
        governance_settings(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"export_bucket": "exports"},
        {"export_prefix": "temporary/"},
        {"resume_prefix": "clean"},
        {"ledger_prefix": "deletions"},
        {"resume_prefix": ""},
    ],
)
def test_production_governance_settings_reject_storage_contract_drift(
    overrides: dict[str, str]
) -> None:
    with pytest.raises(ValidationError):
        governance_settings(**overrides)


def test_governance_settings_validate_separation_from_ordinary_runtime() -> None:
    settings = governance_settings()

    settings.validate_runtime_separation(
        database_url="postgresql+asyncpg://ux09_app:app-secret@postgres/ux09",
        object_access_key="app-object-access",
        object_secret_key="app-object-secret",
    )
    with pytest.raises(ValueError, match="database user"):
        settings.validate_runtime_separation(
            database_url="postgresql+asyncpg://ux09_governance:other@postgres/ux09",
            object_access_key="app-object-access",
            object_secret_key="app-object-secret",
        )
    with pytest.raises(ValueError, match="object credentials"):
        settings.validate_runtime_separation(
            database_url="postgresql+asyncpg://ux09_app:app-secret@postgres/ux09",
            object_access_key=settings.delete_access_key,
            object_secret_key="app-object-secret",
        )
