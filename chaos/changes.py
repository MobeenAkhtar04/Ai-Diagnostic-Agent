"""Change records: what an ops team's change log would show (config edits, secret rotations, limit changes, flags,
migrations). The agent reads them through recent_changes, exactly like deploy rows."""

from __future__ import annotations

from .util import psql


def _q(v: str | None) -> str:
    return "NULL" if v is None else "'" + v.replace("'", "''") + "'"


def record(kind: str, service: str, detail: str, by: str, git_sha: str | None = None) -> int:
    out = psql(f'INSERT INTO deploys(kind, service, git_sha, detail, "by") VALUES '
               f"({_q(kind)}, {_q(service)}, {_q(git_sha)}, {_q(detail)}, {_q(by)}) RETURNING id")
    return int(out.splitlines()[0])


def delete(change_id: int | None) -> None:
    if change_id:
        psql(f"DELETE FROM deploys WHERE id = {int(change_id)}")
