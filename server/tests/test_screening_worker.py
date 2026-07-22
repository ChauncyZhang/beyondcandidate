import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from server.app.core.settings import Settings
from server.app.queue.payloads import DEFAULT_PAYLOAD_POLICIES, UnsafePayload
from server.app.queue.service import RetryableJobError
from server.app.screening.terminal import LlmTerminalFinalizer
from server.app.screening.terminal import screening_terminal_callbacks
from server.app.worker.main import build_screening_handlers

def test_production_screening_registry_is_allowlisted_and_scanner_settings_are_bounded():
    settings=Settings(clamav_host="clamav",clamav_port=3310,clamav_connect_timeout_seconds=1,clamav_read_timeout_seconds=3,clamav_total_timeout_seconds=4)
    handlers=build_screening_handlers(settings,object(),object())
    assert set(handlers)=={"screening.parse_item","screening.profile_resume","screening.score_item","screening.llm_score_item","screening.llm_finalize_terminal","reports.export"}
    assert settings.clamav_total_timeout_seconds>=settings.clamav_read_timeout_seconds


def test_llm_queue_payload_accepts_application_id_without_business_facts():
    opaque_ids = {name: str(uuid.uuid4()) for name in (
        "organization_id",
        "screening_item_id",
        "screening_result_id",
        "application_id",
        "config_id",
        "prompt_version_id",
    )}
    payload = {**opaque_ids, "config_version": 3}

    assert DEFAULT_PAYLOAD_POLICIES.validate_job("screening.llm_score_item", payload) == payload
    for forbidden in ("required_hits", "required_missing", "bonus_hits", "rule_score", "rule_recommendation"):
        with pytest.raises(UnsafePayload):
            DEFAULT_PAYLOAD_POLICIES.validate_job(
                "screening.llm_score_item",
                {**payload, forbidden: "private-rule-fact"},
            )


def test_llm_terminal_finalizer_payload_is_bounded_and_supports_legacy_source():
    payload = {
        "organization_id": str(uuid.uuid4()),
        "source_job_id": str(uuid.uuid4()),
        "screening_item_id": str(uuid.uuid4()),
        "screening_result_id": str(uuid.uuid4()),
        "config_id": str(uuid.uuid4()),
        "config_version": 3,
        "prompt_version_id": str(uuid.uuid4()),
        "terminal_safe_error_code": "llm_handler_failed",
        "terminal_disposition": "route",
    }

    assert DEFAULT_PAYLOAD_POLICIES.validate_job(
        "screening.llm_finalize_terminal", payload
    ) == payload
    current = {**payload, "application_id": str(uuid.uuid4())}
    assert DEFAULT_PAYLOAD_POLICIES.validate_job(
        "screening.llm_finalize_terminal", current
    ) == current
    with pytest.raises(UnsafePayload):
        DEFAULT_PAYLOAD_POLICIES.validate_job(
            "screening.llm_finalize_terminal",
            {**payload, "provider_body": "private"},
        )
    technical = {
        key: payload[key]
        for key in (
            "organization_id",
            "source_job_id",
            "screening_item_id",
            "terminal_safe_error_code",
            "terminal_disposition",
        )
    }
    technical["terminal_safe_error_code"] = "llm_job_payload_invalid"
    technical["terminal_disposition"] = "technical"
    assert DEFAULT_PAYLOAD_POLICIES.validate_job(
        "screening.llm_finalize_terminal", technical
    ) == technical


def test_llm_terminal_finalizer_retries_database_unavailability():
    class BrokenSessions:
        def begin(self):
            raise OperationalError("select", {}, RuntimeError("offline"))

    with pytest.raises(RetryableJobError) as failed:
        asyncio.run(LlmTerminalFinalizer(BrokenSessions())(SimpleNamespace()))

    assert failed.value.safe_code == "queue_unavailable"


def test_llm_finalizer_exhaustion_callback_reschedules_only_infrastructure_failure():
    callbacks = screening_terminal_callbacks()
    callback = callbacks["screening.llm_finalize_terminal"]
    now = datetime.now(timezone.utc)
    job = SimpleNamespace(
        type="screening.llm_finalize_terminal",
        status="dead_letter",
        attempts=3,
        max_attempts=3,
        run_after=now,
        last_error_code="queue_unavailable",
    )

    class NoDomainSession:
        def __getattribute__(self, name):
            if name in {"execute", "scalar", "get"}:
                raise AssertionError("terminal recovery must not access domain tables")
            return super().__getattribute__(name)

    callback(NoDomainSession(), job, "queue_unavailable", now)

    assert job.status == "queued"
    assert job.attempts == 3
    assert job.max_attempts == 6
    assert job.run_after > now
    callback(NoDomainSession(), job, "queue_unavailable", now)
    assert job.attempts == 3 and job.max_attempts == 6

    technical = SimpleNamespace(
        type="screening.llm_finalize_terminal",
        status="dead_letter",
        attempts=3,
        max_attempts=3,
        run_after=now,
        last_error_code="internal_error",
    )
    callback(NoDomainSession(), technical, "internal_error", now)
    assert technical.status == "dead_letter" and technical.max_attempts == 3
