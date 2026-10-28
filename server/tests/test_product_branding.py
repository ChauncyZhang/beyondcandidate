"""Runtime branding contracts; normal pytest gate, no real providers or deployment."""

import asyncio
import json
import traceback

import pytest
from fastapi.testclient import TestClient

from server.app.core.settings import Settings
from server.app.integrations.feishu import worker
from server.app.integrations.feishu.provider import FakeFeishuProvider
from server.app.main import create_app
from server.tests.test_feishu_api import Probe, configure_feishu, write_headers
from server.tests.test_feishu_notifications import _feedback_event
from server.tests.test_interview_api import seed_application


CUSTOM = {"name": "  Example Recruiting  ", "short_name": "ER", "logo_url": "/brand/logo.svg"}


def write_branding(tmp_path, value=CUSTOM):
    path = tmp_path / "branding.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def make_branded_app(tmp_path, path=None):
    return create_app(
        settings=Settings(
            environment="test",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'branding.db'}",
            cors_origins=["https://hr.example.test"],
            product_branding_file=str(path) if path is not None else None,
        ),
        database_probe=Probe(), storage_probe=Probe(), initialize_identity_schema=True,
    )


def test_defaults_and_explicit_file_are_immutable_and_read_only(tmp_path):
    from server.app.core.branding import ProductBranding, load_product_branding

    assert load_product_branding() == ProductBranding()
    assert load_product_branding().model_dump() == {
        "name": "BeyondCandidate", "short_name": "BC", "logo_url": "/favicon.svg",
        "favicon_url": "/favicon.svg",
    }
    path = write_branding(tmp_path)
    before = path.read_bytes()
    brand = load_product_branding(str(path))
    assert brand.name == "Example Recruiting"
    assert brand.favicon_url == brand.logo_url == "/brand/logo.svg"
    assert path.read_bytes() == before
    with pytest.raises(ValueError):
        brand.name = "Changed"


@pytest.mark.parametrize("name", ["x", " x ", "x" * 80, "\u62db\u8058"])
def test_name_boundaries(tmp_path, name):
    from server.app.core.branding import load_product_branding

    assert load_product_branding(str(write_branding(tmp_path, {**CUSTOM, "name": name}))).name == name.strip()


@pytest.mark.parametrize("patch", [
    {"name": " "}, {"name": "x" * 81}, {"name": 12},
    {"name": "Bad\rName"}, {"name": "Bad\nName"}, {"name": "Bad\tName"}, {"name": "Bad\x7fName"},
    {"short_name": ""}, {"short_name": "123456789"}, {"short_name": "A B"},
    {"short_name": "\uff21"}, {"short_name": "AB\n"}, {"short_name": 12},
    {"logo_url": False}, {"unexpected": "private-config-marker"},
])
def test_invalid_schema_has_safe_error(tmp_path, patch):
    from server.app.core.branding import ProductBrandingError, load_product_branding

    with pytest.raises(ProductBrandingError) as caught:
        load_product_branding(str(write_branding(tmp_path, {**CUSTOM, **patch})))
    rendered = "".join(traceback.format_exception(caught.value))
    assert str(caught.value) == "Product branding configuration is invalid or unreadable."
    assert "private-config-marker" not in rendered
    assert str(tmp_path) not in rendered


@pytest.mark.parametrize("field", ["logo_url", "favicon_url"])
@pytest.mark.parametrize("value", [
    "https://example.test/logo.svg", "//example.test/logo.svg", "relative.svg",
    "/logo.svg?q=1", "/logo.svg#hash", "/a\\b.svg", "/a/../b.svg",
    "/a\x00.svg", "/a\n.svg", "/a\x7f.svg", "/space logo.svg",
    "/%2fexample.test/logo.svg", "/a/%2e%2e/b.svg", "/%255cfoo",
    "/logo.svg%3Fq=1", "/logo.svg%23hash", "/%0a.svg", "/bad%zz.svg",
    "/%252525252525252525252fexample.test/logo.svg",
])
def test_unsafe_asset_paths_are_rejected(tmp_path, field, value):
    from server.app.core.branding import ProductBrandingError, load_product_branding

    with pytest.raises(ProductBrandingError):
        load_product_branding(str(write_branding(tmp_path, {**CUSTOM, field: value})))


@pytest.mark.parametrize("logo,favicon", [(None, None), ("/brand/logo.svg", "/brand/icon.ico"), ("/assets/%E6%8B%9B.svg", None)])
def test_valid_optional_assets(tmp_path, logo, favicon):
    from server.app.core.branding import load_product_branding

    brand = load_product_branding(str(write_branding(tmp_path, {**CUSTOM, "logo_url": logo, "favicon_url": favicon})))
    assert brand.logo_url == logo
    assert brand.favicon_url == favicon


@pytest.mark.parametrize("contents", ["not-json-private-config-marker", "[]", "null", "{}", '{"name":"Only"}', b"\xff"])
def test_bad_explicit_file_fails_startup_without_raw_contents(tmp_path, contents):
    from server.app.core.branding import ProductBrandingError

    path = tmp_path / "branding.json"
    path.write_bytes(contents if isinstance(contents, bytes) else contents.encode())
    with pytest.raises(ProductBrandingError) as caught:
        make_branded_app(tmp_path, path)
    assert "private-config-marker" not in "".join(traceback.format_exception(caught.value))


def test_missing_or_directory_file_fails_startup(tmp_path):
    from server.app.core.branding import ProductBrandingError

    for path in (tmp_path / "missing.json", tmp_path):
        with pytest.raises(ProductBrandingError):
            make_branded_app(tmp_path, path)


def test_environment_setting_is_independent_from_email_and_organization(monkeypatch, tmp_path):
    monkeypatch.delenv("PRODUCT_BRANDING_FILE", raising=False)
    assert Settings.from_environment().product_branding_file is None
    path = write_branding(tmp_path)
    monkeypatch.setenv("PRODUCT_BRANDING_FILE", str(path))
    monkeypatch.setenv("EMAIL_FROM_NAME", "Mail Sender")
    monkeypatch.setenv("DEFAULT_ORGANIZATION_NAME", "Organization")
    monkeypatch.setenv("DEFAULT_ORGANIZATION_SLUG", "organization")
    settings = Settings.from_environment()
    assert settings.product_branding_file == str(path)
    assert settings.email_from_name == "Mail Sender"
    assert settings.default_organization_name == "Organization"


def test_explicit_empty_path_is_not_silently_defaulted(tmp_path):
    from server.app.core.branding import ProductBrandingError, load_product_branding

    with pytest.raises(ProductBrandingError):
        load_product_branding("")


@pytest.mark.parametrize("custom", [False, True])
def test_api_loads_once_and_feishu_test_message_uses_brand(tmp_path, custom):
    path = write_branding(tmp_path) if custom else None
    app = make_branded_app(tmp_path, path)
    app.state.feishu_provider = FakeFeishuProvider()
    if path:
        path.write_text("invalid-after-startup", encoding="utf-8")
    with TestClient(app) as client:
        _, csrf = configure_feishu((app, client))
        response = client.post("/api/v1/settings/integrations/feishu/test", headers=write_headers(csrf))
    assert response.status_code == 200
    expected = "Example Recruiting" if custom else "BeyondCandidate"
    assert app.state.product_branding.name == expected
    assert app.state.feishu_provider.messages[-1][1] == f"{expected} \u98de\u4e66\u62db\u8058\u63d0\u9192\u6d4b\u8bd5\u6210\u529f\u3002"


@pytest.mark.parametrize("custom", [False, True])
def test_actual_worker_builder_loads_once_and_sends_configured_card(tmp_path, monkeypatch, custom):
    path = write_branding(tmp_path) if custom else None
    app = make_branded_app(tmp_path, path)
    app.state.identity_store.create_schema()
    seed = seed_application(app)
    event, interview_id = _feedback_event(app, seed)
    provider = FakeFeishuProvider()
    monkeypatch.setattr("server.app.integrations.feishu.provider.HttpFeishuProvider", lambda: provider)
    handlers = worker.build_feishu_outbox_handlers(app.state.settings)
    if path:
        path.write_text("invalid-after-builder", encoding="utf-8")
    handler = handlers["feishu.notification.send"]
    asyncio.run(handler(event, event.id))
    assert len(provider.cards) == 1
    card = provider.cards[0][1]
    expected = "Example Recruiting" if custom else "BeyondCandidate"
    assert card["header"]["subtitle"]["content"] == f"{expected} \u62db\u8058\u534f\u4f5c"
    assert card["header"]["title"]["content"] == "\u9762\u8bd5\u8bc4\u4ef7\u5f85\u63d0\u4ea4"
    assert card["body"]["elements"][-1]["behaviors"][0]["default_url"] == f"https://hr.example.test/interviews/{interview_id}/feedback"
    assert handlers["feishu.calendar.create"] is handlers["feishu.calendar.update"]
    assert handlers["feishu.approval.onboarding.create"] is handlers["feishu.approval.onboarding.reconcile"]


def test_worker_builder_fails_closed_for_missing_branding_before_provider_creation(tmp_path, monkeypatch):
    from server.app.core.branding import ProductBrandingError

    def unexpected_provider():
        pytest.fail("provider must not be constructed for invalid branding")

    monkeypatch.setattr("server.app.integrations.feishu.provider.HttpFeishuProvider", unexpected_provider)
    with pytest.raises(ProductBrandingError):
        worker.build_feishu_outbox_handlers(Settings(product_branding_file=str(tmp_path / "missing.json")))
