"""Pydantic argument schemas for every tool, plus the submit_diagnosis payload."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..taxonomy import RootCause

Service = Literal["shop_api", "payment_stub", "worker", "postgres", "redis"]
AppService = Literal["shop_api", "payment_stub", "worker"]


class QueryMetrics(BaseModel):
    promql: str = Field(description='Selector, e.g. http_latency_p95_seconds{service="shop_api"}')
    minutes: int = Field(15, ge=1, le=60)


class ListAlerts(BaseModel):
    pass


class SearchLogs(BaseModel):
    service: AppService | None = None
    level: Literal["INFO", "WARN", "ERROR"] | None = None
    contains: str | None = Field(None, max_length=200)
    minutes: int = Field(15, ge=1, le=60)
    limit: int = Field(30, ge=1, le=100)


class TopErrors(BaseModel):
    service: AppService | None = None
    minutes: int = Field(15, ge=1, le=60)
    limit: int = Field(8, ge=1, le=20)


class GetStackTrace(BaseModel):
    signature: str = Field(description="A signature string exactly as returned by top_errors", max_length=400)


class FindSlowTraces(BaseModel):
    service: AppService = "shop_api"
    min_duration_ms: float = Field(1000, ge=0)
    minutes: int = Field(15, ge=1, le=60)
    limit: int = Field(10, ge=1, le=30)


class GetTrace(BaseModel):
    trace_id: str = Field(pattern=r"^[0-9a-f]{16,32}$")


class NoArgs(BaseModel):
    pass


class DbTopQueries(BaseModel):
    order_by: Literal["mean_ms", "total_ms", "calls"] = "total_ms"
    limit: int = Field(5, ge=1, le=20)


class ContainerStats(BaseModel):
    service: Service


class RecentChanges(BaseModel):
    since_minutes: int = Field(60, ge=1, le=1440 * 3)


class GetConfig(BaseModel):
    service: AppService


class ReadSource(BaseModel):
    path: str = Field(pattern=r"^services/[\w/]+\.py$", description="e.g. services/shop_api/app/checkout.py")
    start_line: int = Field(1, ge=1)
    end_line: int = Field(80, ge=1)
    sha: str | None = Field(None, pattern=r"^[0-9a-f]{7,40}$", description="default: deployed commit")


class GitDiff(BaseModel):
    from_sha: str = Field(pattern=r"^[0-9a-f]{7,40}$")
    to_sha: str = Field(pattern=r"^[0-9a-f]{7,40}$")
    path: str | None = None


class RestartService(BaseModel):
    service: Service
    reason: str = Field(max_length=300)


class KillQuery(BaseModel):
    pid: int = Field(ge=1)
    reason: str = Field(max_length=300)


class Claim(BaseModel):
    text: str = Field(description="One factual statement about what was observed")
    step_ids: list[str] = Field(min_length=1, description="Step IDs (e.g. s3) whose output proves this claim")


class Hypothesis(BaseModel):
    category: RootCause
    confidence: float = Field(ge=0, le=1)
    evidence_step_ids: list[str] = Field(default_factory=list)
    why: str = ""


class Diagnosis(BaseModel):
    category: RootCause = Field(description="Root cause category; use none if there is no real failure")
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(description="Two or three sentences: what broke, why, and impact")
    claims: list[Claim] = Field(min_length=1)
    hypotheses: list[Hypothesis] = Field(min_length=1, max_length=3, description="Ranked, first = category")
    fix: str = Field(description="Concrete remediation steps")
