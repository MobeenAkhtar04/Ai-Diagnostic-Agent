"""Root cause taxonomy: the fixed enum every diagnosis must use."""

from enum import StrEnum


class RootCause(StrEnum):
    db_pool_exhaustion = "db_pool_exhaustion"
    db_connection_limit = "db_connection_limit"
    slow_query = "slow_query"
    n_plus_one = "n_plus_one"
    lock_contention = "lock_contention"
    deadlock = "deadlock"
    disk_full = "disk_full"
    memory_leak = "memory_leak"
    cpu_saturation = "cpu_saturation"
    event_loop_blocked = "event_loop_blocked"
    bad_deploy = "bad_deploy"
    race_condition = "race_condition"
    resource_leak = "resource_leak"
    dependency_latency = "dependency_latency"
    dependency_down = "dependency_down"
    network_flaky = "network_flaky"
    retry_storm = "retry_storm"
    cache_failure = "cache_failure"
    cache_stampede = "cache_stampede"
    config_error = "config_error"
    bad_migration = "bad_migration"
    auth_failure = "auth_failure"
    feature_flag = "feature_flag"
    resource_limit = "resource_limit"
    clock_skew = "clock_skew"
    none = "none"
    unknown = "unknown"  # inconclusive runs only


FAMILY = {
    "db_pool_exhaustion": "database", "db_connection_limit": "database", "slow_query": "database",
    "n_plus_one": "database", "lock_contention": "database", "deadlock": "database", "disk_full": "database",
    "memory_leak": "application", "cpu_saturation": "application", "event_loop_blocked": "application",
    "bad_deploy": "application", "race_condition": "application", "resource_leak": "application",
    "dependency_latency": "dependency", "dependency_down": "dependency", "network_flaky": "dependency",
    "retry_storm": "dependency", "cache_failure": "dependency", "cache_stampede": "dependency",
    "config_error": "config", "bad_migration": "config", "auth_failure": "config", "feature_flag": "config",
    "resource_limit": "infrastructure", "clock_skew": "infrastructure",
    "none": "control", "unknown": "unknown",
}

CATEGORIES = [c.value for c in RootCause]


DEFINITIONS = {
    "db_pool_exhaustion": "every connection in the app's own pool is checked out and requests wait for the pool, "
                          "while no slow query or lock explains it: the app holds connections itself (e.g. a transaction "
                          "kept open across a slow non-database step such as an external call), and the app is not "
                          "CPU-starved or throttled itself",
    "db_connection_limit": "Postgres itself refuses new connections (too many clients, max_connections reached)",
    "slow_query": "one or a few queries became slow (missing index, sequential scan, bad plan) and load the database; "
                  "a pool that fills up because queries got slow is a symptom of this, not db_pool_exhaustion",
    "n_plus_one": "a request issues many small repeated queries instead of one (query count per request jumps)",
    "lock_contention": "sessions wait on row or table locks held by another long-running transaction",
    "deadlock": "Postgres detected a deadlock and aborted transactions that locked the same rows in opposite order",
    "disk_full": "a disk or volume ran out of space (e.g. the database cannot extend a file)",
    "memory_leak": "memory grows steadily over time at constant load, usually ending in OOM kills and restarts",
    "cpu_saturation": "CPU is pinned by computation inside the app, not by waiting on I/O",
    "event_loop_blocked": "a blocking synchronous call stalls the async event loop, so every endpoint slows at once",
    "bad_deploy": "a code change shipped in a recent deploy introduced the failure (new exceptions begin after it)",
    "race_condition": "concurrent or duplicated operations corrupt or duplicate data (oversell, a double charge from a retry "
                      "with no idempotency key); the damage is duplicated or inconsistent state",
    "resource_leak": "handles such as file descriptors or threads are opened and never released",
    "dependency_latency": "a dependency answers every call but consistently slowly; no resets, no errors, not down",
    "dependency_down": "an external dependency other than the cache (e.g. payment_stub) is stopped or unreachable "
                       "(connection refused, container not running)",
    "network_flaky": "intermittent connection resets or drops hit only a fraction of calls, giving partial failures",
    "retry_storm": "retries without backoff multiply outbound load against an already struggling dependency",
    "cache_failure": "Redis (the cache) is stopped or erroring and its load falls through to Postgres; use this, "
                     "not dependency_down, whenever the dead component is Redis",
    "cache_stampede": "many requests miss the same hot key at once and hit the database with identical queries",
    "config_error": "a wrong or changed configuration value (env var, URL, timeout) is the cause, not code",
    "bad_migration": "a schema migration broke code that still expects the old schema (undefined column or table)",
    "auth_failure": "credentials are rejected (401/403), e.g. a rotated key that the caller still uses old",
    "feature_flag": "a flag change routes a share of traffic to a slow or broken code path",
    "resource_limit": "a container limit (memory or CPU quota) is too low for normal load; no growth trend, "
                      "OOM kills or throttling at baseline usage",
    "clock_skew": "services disagree about the time (e.g. tokens reported as not yet valid)",
    "none": "no real failure: metrics are within SLO and only background noise is present",
}
