"""Simulated target system.

Stands in for the real Docker Compose stack (shop_api, payment_stub, worker, Postgres, Redis) so the
agent, tools, validator and eval can be built and tested before P1. Every tool reads through
`diag.tools.backend.Backend`; in P1/P3 a `RealBackend` replaces `SimBackend` with Prometheus, Loki,
Tempo, pg_stat_* and the Docker API. The ground-truth label lives on the World and is never exposed
through any tool.
"""
