import asyncio
import json
import struct

import pytest

from server.app.llm.gateway import TransportResponse
from server.app.llm.policy import ProviderPolicyError
from server.app.ocr.gateway import (
    FIXED_PROBE_PNG,
    MAX_IMAGE_BYTES,
    OcrGateway,
    OcrGatewayError,
)


def resolver(address="8.8.8.8"):
    return lambda host, port, type: [(2, 1, 6, "", (address, port))]


class Transport:
    def __init__(self, responses=None):
        self.responses = list(responses or ["ok"])
        self.calls = []

    def post(self, spec, address, path, headers, body, max_response_bytes):
        self.calls.append((spec, address, path, headers, body, max_response_bytes))
        text = self.responses.pop(0)
        if isinstance(text, TransportResponse):
            return text
        return TransportResponse(200, json.dumps({"choices": [{"message": {"content": text}}]}).encode())


def test_gateway_dns_pins_https_and_disables_thinking_for_fixed_probe():
    transport = Transport(["test image"])
    gateway = OcrGateway(transport, resolver=resolver())

    assert asyncio.run(
        gateway.test_connection("vision", "https://vision.example/v1", "vision-1", "sk-private")
    ) >= 0

    _, address, path, headers, body, _ = transport.calls[0]
    document = json.loads(body)
    assert address == "8.8.8.8"
    assert path == "/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-private"
    assert document["thinking"] == {"type": "disabled"}
    assert document["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert all(value not in body.decode() for value in ("resume", "简历", "candidate", "13800000000"))


def test_fixed_probe_image_meets_provider_dimension_requirements():
    width, height = struct.unpack(">II", FIXED_PROBE_PNG[16:24])
    assert width > 10 and height > 10


@pytest.mark.parametrize(
    "base_url",
    [
        "http://vision.example/v1",
        "https://user:secret@vision.example/v1",
        "https://vision.example:8443/v1",
        "https://vision.example/v1?redirect=internal",
        "https://vision.example/v1#fragment",
        "https://127.0.0.1/v1",
        "https://169.254.169.254/v1",
    ],
)
def test_gateway_rejects_unsafe_provider_urls(base_url):
    with pytest.raises((ProviderPolicyError, OcrGatewayError)):
        OcrGateway(resolver=resolver()).validate_provider("vision", base_url, "model")


def test_gateway_rejects_private_dns_answers_and_pins_mixed_answers_closed():
    for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1"):
        with pytest.raises(ProviderPolicyError):
            OcrGateway(resolver=resolver(address)).validate_provider(
                "vision", "https://vision.example/v1", "model"
            )
    mixed = lambda *args, **kwargs: [
        (2, 1, 6, "", ("8.8.8.8", 443)),
        (2, 1, 6, "", ("127.0.0.1", 443)),
    ]
    with pytest.raises(ProviderPolicyError):
        OcrGateway(resolver=mixed).validate_provider("vision", "https://vision.example/v1", "model")


def test_extract_images_preserves_page_order_and_bounds_inputs_and_outputs():
    transport = Transport(["page one", "page two"])
    gateway = OcrGateway(transport, resolver=resolver())
    jpeg = b"\xff\xd8\xff" + b"small jpeg"

    result = asyncio.run(
        gateway.extract_images(
            "vision", "https://vision.example/v1", "model", "key", [FIXED_PROBE_PNG, jpeg]
        )
    )

    assert result == ["page one", "page two"]
    assert [json.loads(call[4])["thinking"] for call in transport.calls] == [
        {"type": "disabled"},
        {"type": "disabled"},
    ]
    with pytest.raises(OcrGatewayError, match="ocr_image_count_invalid"):
        asyncio.run(gateway.extract_images("vision", "https://vision.example/v1", "model", "key", []))
    with pytest.raises(OcrGatewayError, match="ocr_image_too_large"):
        asyncio.run(
            gateway.extract_images(
                "vision",
                "https://vision.example/v1",
                "model",
                "key",
                [b"\x89PNG\r\n\x1a\n" + b"x" * MAX_IMAGE_BYTES],
            )
        )
    with pytest.raises(OcrGatewayError, match="ocr_image_invalid"):
        asyncio.run(
            gateway.extract_images("vision", "https://vision.example/v1", "model", "key", [b"text"])
        )

    oversized = OcrGateway(Transport(["x" * 50_001]), resolver=resolver())
    with pytest.raises(OcrGatewayError, match="ocr_page_text_too_large"):
        asyncio.run(
            oversized.extract_images(
                "vision", "https://vision.example/v1", "model", "key", [FIXED_PROBE_PNG]
            )
        )


@pytest.mark.parametrize(
    ("status", "safe_code"),
    [
        (401, "provider_auth_failed"),
        (404, "provider_model_not_found"),
        (429, "provider_quota_or_rate_limited"),
        (302, "provider_redirect_rejected"),
        (500, "provider_unavailable"),
    ],
)
def test_gateway_maps_provider_errors_without_response_echo(status, safe_code):
    response = TransportResponse(status, b"secret provider details")
    with pytest.raises(OcrGatewayError) as raised:
        asyncio.run(
            OcrGateway(Transport([response]), resolver=resolver()).test_connection(
                "vision", "https://vision.example/v1", "model", "key"
            )
        )
    assert raised.value.safe_code == safe_code
    assert "secret provider details" not in str(raised.value)


def test_gateway_rejects_malformed_and_oversized_provider_responses():
    malformed = TransportResponse(200, b"not-json")
    with pytest.raises(OcrGatewayError, match="provider_response_invalid"):
        asyncio.run(
            OcrGateway(Transport([malformed]), resolver=resolver()).test_connection(
                "vision", "https://vision.example/v1", "model", "key"
            )
        )
    oversized = TransportResponse(200, b"x" * 101)
    with pytest.raises(OcrGatewayError, match="provider_response_too_large"):
        asyncio.run(
            OcrGateway(Transport([oversized]), resolver=resolver(), max_response_bytes=100).test_connection(
                "vision", "https://vision.example/v1", "model", "key"
            )
        )
