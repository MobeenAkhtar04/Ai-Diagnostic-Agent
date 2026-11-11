"""Agent database. SQLite for the demo; same tables move to Postgres in P4."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY, fingerprint TEXT, alert_name TEXT, service TEXT, severity TEXT,
  started_at REAL, status TEXT, scenario TEXT
);
CREATE TABLE IF NOT EXISTS investigations (
  id INTEGER PRIMARY KEY, incident_id INTEGER, model TEXT, status TEXT, started_at REAL, ended_at REAL,
  tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS steps (
  id TEXT, investigation_id INTEGER, seq INTEGER, tool TEXT, args_json TEXT, result_digest TEXT,
  result_json TEXT, latency_ms REAL, error TEXT, PRIMARY KEY (investigation_id, id)
);
CREATE TABLE IF NOT EXISTS diagnoses (
  id INTEGER PRIMARY KEY, investigation_id INTEGER, category TEXT, confidence REAL,
  evidence_step_ids TEXT, fix TEXT, report_md TEXT, diagnosis_json TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY, investigation_id INTEGER, tool TEXT, args_json TEXT, status TEXT,
  decided_by TEXT, decided_at REAL
);
CREATE TABLE IF NOT EXISTS scenario_runs (
  id INTEGER PRIMARY KEY, scenario TEXT, run_no INTEGER, investigation_id INTEGER,
  expected_category TEXT, got_category TEXT, correct INTEGER, ttd_seconds REAL
);
"""


class Store:
    def __init__(self, path: str | Path = ".data/diag.db"):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def _ins(self, sql: str, args: tuple) -> int:
        cur = self.db.execute(sql, args)
        self.db.commit()
        return int(cur.lastrowid or 0)

    def create_incident(self, alert: dict, scenario: str) -> int:
        fp = f"{alert['name']}|{alert['labels'].get('service', '')}"
        return self._ins(
            "INSERT INTO incidents (fingerprint, alert_name, service, severity, started_at, status, scenario)"
            " VALUES (?,?,?,?,?,?,?)",
            (fp, alert["name"], alert["labels"].get("service"), alert["severity"], alert["started_at"],
             "investigating", scenario),
        )

    def create_investigation(self, incident_id: int, model: str) -> int:
        return self._ins(
            "INSERT INTO investigations (incident_id, model, status, started_at) VALUES (?,?,?,?)",
            (incident_id, model, "running", time.time()),
        )

    def add_step(self, inv_id: int, step_id: str, seq: int, tool: str, args: dict, result: str,
                 digest: str, latency_ms: float, error: str | None) -> None:
        self.db.execute(
            "INSERT INTO steps VALUES (?,?,?,?,?,?,?,?,?)",
            (step_id, inv_id, seq, tool, json.dumps(args), digest, result, latency_ms, error),
        )
        self.db.commit()

    def steps(self, inv_id: int) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM steps WHERE investigation_id=? ORDER BY seq", (inv_id,)))

    def finish_investigation(self, inv_id: int, status: str, tokens_in: int, tokens_out: int,
                             cost: float) -> None:
        self.db.execute(
            "UPDATE investigations SET status=?, ended_at=?, tokens_in=?, tokens_out=?, cost_usd=? WHERE id=?",
            (status, time.time(), tokens_in, tokens_out, cost, inv_id),
        )
        self.db.commit()

    def save_diagnosis(self, inv_id: int, diag: dict, report_md: str) -> int:
        ids = sorted({s for c in diag.get("claims", []) for s in c.get("step_ids", [])})
        return self._ins(
            "INSERT INTO diagnoses (investigation_id, category, confidence, evidence_step_ids, fix, report_md,"
            " diagnosis_json) VALUES (?,?,?,?,?,?,?)",
            (inv_id, diag["category"], diag.get("confidence", 0), json.dumps(ids), diag.get("fix", ""),
             report_md, json.dumps(diag)),
        )

    def set_incident_status(self, incident_id: int, status: str) -> None:
        self.db.execute("UPDATE incidents SET status=? WHERE id=?", (status, incident_id))
        self.db.commit()

    def create_approval(self, inv_id: int, tool: str, args: dict) -> int:
        return self._ins(
            "INSERT INTO approvals (investigation_id, tool, args_json, status) VALUES (?,?,?,?)",
            (inv_id, tool, json.dumps(args), "pending"),
        )

    def approvals(self, inv_id: int) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM approvals WHERE investigation_id=?", (inv_id,)))

    def record_scenario_run(self, scenario: str, run_no: int, inv_id: int | None, expected: str, got: str,
                            ttd: float) -> None:
        self._ins(
            "INSERT INTO scenario_runs (scenario, run_no, investigation_id, expected_category, got_category,"
            " correct, ttd_seconds) VALUES (?,?,?,?,?,?,?)",
            (scenario, run_no, inv_id, expected, got, int(expected == got), ttd),
        )
