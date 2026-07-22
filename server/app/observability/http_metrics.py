from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import perf_counter

from fastapi import Request
from prometheus_client import CollectorRegistry, Counter, Histogram
from starlette.routing import Match

from server.app.core.probes import ReadinessProbe


HTTP_DURATION_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
READINESS_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5)
ALLOWED_HTTP_METHODS = {"CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"}


def route_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str):
        return path
    for candidate in request.app.router.routes:
        match, _ = candidate.matches(request.scope)
        if match in {Match.FULL, Match.PARTIAL}:
            candidate_path = getattr(candidate, "path", None)
            if isinstance(candidate_path, str):
                return candidate_path
    return "unmatched"


def status_class(status_code: int) -> str:
    if 100 <= status_code <= 599:
        return f"{status_code // 100}xx"
    return "unknown"


def method_label(method: str) -> str:
    normalized = method.upper()
    return normalized if normalized in ALLOWED_HTTP_METHODS else "OTHER"


def login_failure_reason(status_code: int) -> str:
    return {
        401: "invalid_credentials",
        403: "request_rejected",
        422: "validation_failed",
        429: "rate_limited",
    }.get(status_code, "internal" if status_code >= 500 else "other")


class HttpMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        self.requests = Counter(
            "ux09_http_requests",
            "Completed HTTP requests by bounded route dimensions.",
            ("method", "route", "status_class"),
            registry=self.registry,
        )
        self.duration = Histogram(
            "ux09_http_request_duration_seconds",
            "HTTP request duration by bounded route dimensions.",
            ("method", "route", "status_class"),
            buckets=HTTP_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.readiness = Counter(
            "ux09_readiness_checks",
            "Readiness checks by fixed dependency and result.",
            ("dependency", "result"),
            registry=self.registry,
        )
        self.readiness_duration = Histogram(
            "ux09_readiness_duration_seconds",
            "Readiness check duration by fixed dependency.",
            ("dependency",),
            buckets=READINESS_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.login_failures = Counter(
            "ux09_login_failures",
            "Login failures by fixed safe reason.",
            ("reason",),
            registry=self.registry,
        )

    def observe_request(
        self, *, method: str, route: str, status_code: int, duration_seconds: float
    ) -> None:
        labels = (method_label(method), route, status_class(status_code))
        self.requests.labels(*labels).inc()
        self.duration.labels(*labels).observe(max(0.0, duration_seconds))
        if route == "/api/v1/auth/login" and status_code >= 400:
            self.login_failures.labels(login_failure_reason(status_code)).inc()

    def observe_readiness(
        self, *, dependency: str, result: str, duration_seconds: float
    ) -> None:
        self.readiness.labels(dependency, result).inc()
        self.readiness_duration.labels(dependency).observe(max(0.0, duration_seconds))


@dataclass(frozen=True)
class InstrumentedReadinessProbe:
    dependency: str
    probe: ReadinessProbe
    metrics: HttpMetrics

    async def check(self) -> None:
        started = perf_counter()
        result = "failed"
        try:
            await self.probe.check()
            result = "ready"
        except asyncio.CancelledError:
            result = "cancelled"
            raise
        finally:
            self.metrics.observe_readiness(
                dependency=self.dependency,
                result=result,
                duration_seconds=perf_counter() - started,
            )
