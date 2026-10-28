"""Read-only runtime product identity, separate from organization and sender identity."""

import json
import re
from pathlib import Path
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ProductBrandingError(ValueError):
    """A configured branding file cannot safely be used."""


class ProductBranding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    name: str = Field(default="BeyondCandidate", min_length=1, max_length=80)
    short_name: str = Field(default="BC", min_length=1, max_length=8)
    logo_url: str | None = "/favicon.svg"
    favicon_url: str | None = "/favicon.svg"

    @model_validator(mode="before")
    @classmethod
    def default_favicon_to_logo(cls, value):
        if isinstance(value, dict) and "logo_url" in value and "favicon_url" not in value:
            return {**value, "favicon_url": value["logo_url"]}
        return value

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value):
        if isinstance(value, str):
            if re.search(r"[\x00-\x1f\x7f]", value):
                raise ValueError("product name must not contain controls")
            return value.strip()
        return value

    @field_validator("short_name")
    @classmethod
    def validate_short_name(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9]{1,8}", value) is None:
            raise ValueError("short name must use ASCII letters and digits")
        return value

    @field_validator("logo_url", "favicon_url")
    @classmethod
    def validate_asset_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        decoded = value
        # Proxies and browsers may decode differently; validate each encoded layer.
        for _ in range(8):
            if (
                not decoded.startswith("/") or decoded.startswith("//")
                or re.search(r"[?#\\\x00-\x20\x7f]", decoded)
                or ".." in decoded.split("/")
                or re.search(r"%(?![0-9A-Fa-f]{2})", decoded)
            ):
                raise ValueError("branding assets must use safe root-relative paths")
            next_decoded = unquote(decoded, errors="strict")
            if next_decoded == decoded:
                return value
            decoded = next_decoded
        raise ValueError("branding asset encoding is too deeply nested")


def load_product_branding(path: str | None = None) -> ProductBranding:
    if path is None:
        return ProductBranding()
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not {"name", "short_name", "logo_url"} <= value.keys():
            raise ValueError("required branding fields are missing")
        return ProductBranding.model_validate(value)
    except (OSError, ValueError, ValidationError):
        # Never include file paths, JSON contents or validation input in startup errors.
        raise ProductBrandingError("Product branding configuration is invalid or unreadable.") from None
