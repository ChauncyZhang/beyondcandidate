import logging

from fastapi.testclient import TestClient
from prometheus_client import generate_latest
from prometheus_client.parser import text_string_to_metric_families

from server.app.main import create_app


class HealthyProbe:
    async def check(self) -> None:
        return None


def test_request_log_and_metrics_do_not_contain_path_or_query_pii(caplog) -> None:
    app = create_app(database_probe=HealthyProbe(), storage_probe=HealthyProbe())

    @app.get("/test/people/{person_id}")
    async def person(person_id: str) -> dict[str, str]:
        return {"person_id": person_id}

    canary = "alice.canary@example.test"
    with caplog.at_level(logging.INFO, logger="server.app.main"):
        client = TestClient(app)
        assert client.get(f"/test/people/{canary}?token=canary-secret").status_code == 200
        metrics = client.get("/metrics").text

    contexts = [getattr(record, "context", {}) for record in caplog.records]
    assert canary not in repr(contexts)
    assert "canary-secret" not in repr(contexts)
    assert canary not in metrics
    assert "canary-secret" not in metrics
    assert any(context.get("route") == "/test/people/{person_id}" for context in contexts)


def test_unknown_database_dimensions_are_collapsed_to_fixed_labels() -> None:
    from server.app.observability.collectors import QueueSnapshot
    from server.app.observability.exporter import build_registry

    canary = "alice.canary@example.test"
    snapshot = QueueSnapshot(
        job_counts={(canary, "queued"): 1},
        outbox_counts={(canary, "queued"): 1},
    )

    payload = generate_latest(build_registry(lambda: snapshot)).decode("utf-8")

    assert canary not in payload
    assert 'job_type="other"' in payload
    assert 'topic="other"' in payload


def test_collapsed_dimensions_are_aggregated_into_one_time_series() -> None:
    from server.app.observability.collectors import AttemptStats, QueueSnapshot
    from server.app.observability.exporter import build_registry

    snapshot = QueueSnapshot(
        job_counts={("unknown.one", "queued"): 1, ("unknown.two", "queued"): 2},
        attempt_stats={
            ("screening.parse_item", "failed", "parser_timeout"): AttemptStats(1, 1.0),
            ("screening.parse_item", "failed", "document_invalid"): AttemptStats(2, 2.5),
        },
        outbox_counts={("unknown.one", "queued"): 3, ("unknown.two", "queued"): 4},
    )

    payload = generate_latest(build_registry(lambda: snapshot)).decode("utf-8")
    samples = [
        sample
        for family in text_string_to_metric_families(payload)
        for sample in family.samples
    ]

    assert [
        sample.value
        for sample in samples
        if sample.name == "ux09_jobs"
        and sample.labels == {"job_type": "other", "status": "queued"}
    ] == [3]
    assert [
        sample.value
        for sample in samples
        if sample.name == "ux09_job_attempts_total"
        and sample.labels
        == {
            "job_type": "screening.parse_item",
            "result": "failed",
            "error_class": "parse",
        }
    ] == [3]
    assert [
        sample.value
        for sample in samples
        if sample.name == "ux09_outbox_events"
        and sample.labels == {"topic": "other", "status": "queued"}
    ] == [7]
