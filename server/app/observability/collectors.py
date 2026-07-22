from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import logging
from typing import Any

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from psycopg.rows import dict_row


logger = logging.getLogger(__name__)

ALLOWED_JOB_TYPES = {
    "reports.export",
    "screening.llm_score_item",
    "screening.parse_item",
    "screening.score_item",
}
ALLOWED_JOB_STATUSES = {
    "cancelled",
    "dead_letter",
    "failed",
    "queued",
    "running",
    "succeeded",
}
ALLOWED_ATTEMPT_RESULTS = {"abandoned", "cancelled", "failed", "running", "succeeded"}
ALLOWED_OUTBOX_TOPICS = {"audit.created"}
ALLOWED_OUTBOX_STATUSES = {"failed", "published", "queued", "running"}


@dataclass(frozen=True)
class AttemptStats:
    count: int
    duration_seconds: float


@dataclass(frozen=True)
class QueueSnapshot:
    job_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    oldest_ready_age_seconds: dict[str, float] = field(default_factory=dict)
    attempt_stats: dict[tuple[str, str, str], AttemptStats] = field(default_factory=dict)
    expired_leases: dict[str, int] = field(default_factory=dict)
    dead_letters: dict[str, int] = field(default_factory=dict)
    outbox_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    oldest_outbox_age_seconds: dict[str, float] = field(default_factory=dict)


def _job_type(value: object) -> str | None:
    text = str(value)
    if text.startswith("governance."):
        return None
    return text if text in ALLOWED_JOB_TYPES else "other"


def _job_status(value: object) -> str:
    text = str(value)
    return text if text in ALLOWED_JOB_STATUSES else "unknown"


def _attempt_result(value: object) -> str:
    text = str(value or "running")
    return text if text in ALLOWED_ATTEMPT_RESULTS else "unknown"


def _outbox_topic(value: object) -> str:
    text = str(value)
    return text if text in ALLOWED_OUTBOX_TOPICS else "other"


def _outbox_status(value: object) -> str:
    text = str(value)
    return text if text in ALLOWED_OUTBOX_STATUSES else "unknown"


def _error_class(value: object) -> str:
    code = str(value or "")
    if any(marker in code for marker in ("parse", "document", "pdf", "docx")):
        return "parse"
    if any(marker in code for marker in ("llm", "model", "provider")):
        return "llm"
    if "lease" in code:
        return "lease"
    if any(marker in code for marker in ("virus", "scan", "malware")):
        return "malware_scan"
    if any(marker in code for marker in ("internal", "handler", "unknown")):
        return "internal"
    return "none" if not code else "other"


class QueueCollector:
    def __init__(self, snapshot_provider: Callable[[], QueueSnapshot]) -> None:
        self._snapshot_provider = snapshot_provider

    def collect(self):  # type: ignore[no-untyped-def]
        collector_up = GaugeMetricFamily(
            "ux09_queue_collector_up", "Whether the last queue snapshot succeeded."
        )
        try:
            snapshot = self._snapshot_provider()
        except Exception as error:
            logger.warning(
                "queue_metrics_collection_failed",
                extra={"context": {"error_type": type(error).__name__}},
            )
            collector_up.add_metric([], 0)
            yield collector_up
            return
        collector_up.add_metric([], 1)
        yield collector_up

        jobs = GaugeMetricFamily(
            "ux09_jobs",
            "Current jobs by bounded type and status.",
            labels=("job_type", "status"),
        )
        normalized_jobs: dict[tuple[str, str], int] = {}
        for (raw_type, raw_status), count in snapshot.job_counts.items():
            job_type = _job_type(raw_type)
            if job_type is not None:
                labels = (job_type, _job_status(raw_status))
                normalized_jobs[labels] = normalized_jobs.get(labels, 0) + count
        for labels, count in normalized_jobs.items():
            jobs.add_metric(labels, count)
        yield jobs

        oldest = GaugeMetricFamily(
            "ux09_job_oldest_ready_age_seconds",
            "Age of the oldest runnable job by bounded type.",
            labels=("job_type",),
        )
        normalized_oldest: dict[str, float] = {}
        for raw_type, age in snapshot.oldest_ready_age_seconds.items():
            job_type = _job_type(raw_type)
            if job_type is not None:
                normalized_oldest[job_type] = max(
                    normalized_oldest.get(job_type, 0.0), max(0.0, age)
                )
        for job_type, age in normalized_oldest.items():
            oldest.add_metric((job_type,), age)
        yield oldest

        attempts = CounterMetricFamily(
            "ux09_job_attempts",
            "Persisted job attempts by bounded result and error class.",
            labels=("job_type", "result", "error_class"),
        )
        attempt_duration = CounterMetricFamily(
            "ux09_job_attempt_duration_seconds",
            "Cumulative persisted job-attempt duration.",
            labels=("job_type", "result", "error_class"),
        )
        normalized_attempts: dict[tuple[str, str, str], AttemptStats] = {}
        for (raw_type, raw_result, raw_error), stats in snapshot.attempt_stats.items():
            job_type = _job_type(raw_type)
            if job_type is None:
                continue
            labels = (job_type, _attempt_result(raw_result), _error_class(raw_error))
            previous = normalized_attempts.get(labels, AttemptStats(0, 0.0))
            normalized_attempts[labels] = AttemptStats(
                previous.count + stats.count,
                previous.duration_seconds + max(0.0, stats.duration_seconds),
            )
        for labels, stats in normalized_attempts.items():
            attempts.add_metric(labels, stats.count)
            attempt_duration.add_metric(labels, stats.duration_seconds)
        yield attempts
        yield attempt_duration

        expired = GaugeMetricFamily(
            "ux09_expired_leases",
            "Current expired leases by queue kind.",
            labels=("queue",),
        )
        for queue in ("job", "outbox"):
            expired.add_metric((queue,), snapshot.expired_leases.get(queue, 0))
        yield expired

        dead_letters = GaugeMetricFamily(
            "ux09_job_dead_letters",
            "Current dead-letter jobs by bounded type.",
            labels=("job_type",),
        )
        normalized_dead_letters: dict[str, int] = {}
        for raw_type, count in snapshot.dead_letters.items():
            job_type = _job_type(raw_type)
            if job_type is not None:
                normalized_dead_letters[job_type] = (
                    normalized_dead_letters.get(job_type, 0) + count
                )
        for job_type, count in normalized_dead_letters.items():
            dead_letters.add_metric((job_type,), count)
        yield dead_letters

        outbox = GaugeMetricFamily(
            "ux09_outbox_events",
            "Current outbox events by bounded topic and status.",
            labels=("topic", "status"),
        )
        normalized_outbox: dict[tuple[str, str], int] = {}
        for (raw_topic, raw_status), count in snapshot.outbox_counts.items():
            labels = (_outbox_topic(raw_topic), _outbox_status(raw_status))
            normalized_outbox[labels] = normalized_outbox.get(labels, 0) + count
        for labels, count in normalized_outbox.items():
            outbox.add_metric(labels, count)
        yield outbox

        outbox_age = GaugeMetricFamily(
            "ux09_outbox_oldest_ready_age_seconds",
            "Age of the oldest ready outbox event by bounded topic.",
            labels=("topic",),
        )
        normalized_outbox_age: dict[str, float] = {}
        for raw_topic, age in snapshot.oldest_outbox_age_seconds.items():
            topic = _outbox_topic(raw_topic)
            normalized_outbox_age[topic] = max(
                normalized_outbox_age.get(topic, 0.0), max(0.0, age)
            )
        for topic, age in normalized_outbox_age.items():
            outbox_age.add_metric((topic,), age)
        yield outbox_age


class PostgresQueueSnapshotProvider:
    def __init__(self, database_url: str, *, connect: Callable[..., Any] | None = None) -> None:
        if connect is None:
            from psycopg import connect as psycopg_connect

            connect = psycopg_connect
        self._database_url = database_url.replace("postgresql+asyncpg://", "postgresql://")
        self._connect = connect

    def __call__(self) -> QueueSnapshot:
        with self._connect(
            self._database_url, connect_timeout=3, row_factory=dict_row
        ) as connection:
            with connection.cursor() as cursor:
                return self._snapshot(cursor)

    @staticmethod
    def _rows(cursor, statement: str) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
        cursor.execute(statement)
        return list(cursor.fetchall())

    def _snapshot(self, cursor) -> QueueSnapshot:  # type: ignore[no-untyped-def]
        rows = self._rows(
            cursor,
            "SELECT metric_name, dimension_a, dimension_b, dimension_c, value "
            "FROM observability.queue_metrics",
        )
        job_counts: dict[tuple[str, str], int] = {}
        oldest_ready: dict[str, float] = {}
        attempt_values: dict[tuple[str, str, str], dict[str, float]] = {}
        expired_leases: dict[str, int] = {}
        dead_letters: dict[str, int] = {}
        outbox_counts: dict[tuple[str, str], int] = {}
        oldest_outbox: dict[str, float] = {}

        for row in rows:
            metric_name = str(row["metric_name"])
            dimension_a = str(row["dimension_a"] or "")
            dimension_b = str(row["dimension_b"] or "")
            dimension_c = str(row["dimension_c"] or "")
            value = float(row["value"] or 0)
            if metric_name == "job_count":
                job_counts[(dimension_a, dimension_b)] = int(value)
            elif metric_name == "job_oldest_ready_age":
                oldest_ready[dimension_a] = value
            elif metric_name in {"job_attempt", "job_attempt_duration"}:
                key = (dimension_a, dimension_b, dimension_c)
                values = attempt_values.setdefault(key, {"count": 0, "duration": 0.0})
                if metric_name == "job_attempt":
                    values["count"] = int(value)
                else:
                    values["duration"] = value
            elif metric_name == "expired_lease":
                expired_leases[dimension_a] = int(value)
            elif metric_name == "job_dead_letter":
                dead_letters[dimension_a] = int(value)
            elif metric_name == "outbox_count":
                outbox_counts[(dimension_a, dimension_b)] = int(value)
            elif metric_name == "outbox_oldest_ready_age":
                oldest_outbox[dimension_a] = value

        attempt_stats = {
            key: AttemptStats(int(values["count"]), float(values["duration"]))
            for key, values in attempt_values.items()
        }
        return QueueSnapshot(
            job_counts=job_counts,
            oldest_ready_age_seconds=oldest_ready,
            attempt_stats=attempt_stats,
            expired_leases=expired_leases,
            dead_letters=dead_letters,
            outbox_counts=outbox_counts,
            oldest_outbox_age_seconds=oldest_outbox,
        )
