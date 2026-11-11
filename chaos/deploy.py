"""Code-level injection: a real git commit with a bug, a real image rebuild, a real redeploy, a row in `deploys`."""

from __future__ import annotations

import subprocess

from . import util
from .util import ROOT, ChaosError, compose, load_state, psql, recreate, save_state

CHECKOUT = "services/shop_api/app/checkout.py"
OLD_A = '    shipping = body.get("shipping_address") or {}\n'
OLD_B = '        "ship_to": shipping.get("city", "pickup"),'
NEW_B = '        "ship_to": body["shipping_address"]["city"],'
COMMIT_MSG = "checkout: read shipping city directly"


def git(*args: str) -> str:
    p = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise ChaosError(f"git {' '.join(args)} failed: {(p.stderr or p.stdout).strip()[-300:]}")
    return p.stdout.strip()


def short_head() -> str:
    return git("rev-parse", "--short=7", "HEAD")


def patch_text(text: str) -> str:
    if OLD_A not in text or OLD_B not in text:
        raise ChaosError("checkout.py no longer has the expected lines; update chaos/deploy.py")
    return text.replace(OLD_A, "").replace(OLD_B, NEW_B)


def _redeploy(sha: str | None) -> None:
    compose("build", "shop_api", timeout=600)
    st = load_state()
    if sha:
        st["interp"]["GIT_SHA"] = sha
    else:
        st["interp"].pop("GIT_SHA", None)
    save_state(st)
    recreate("shop_api")


def bad_deploy() -> dict:
    if git("status", "--porcelain", "--", CHECKOUT):
        raise ChaosError(f"{CHECKOUT} has uncommitted changes; commit or stash them first")
    prev = short_head()
    path = ROOT / CHECKOUT
    path.write_text(patch_text(path.read_text()))
    git("add", CHECKOUT)
    git("commit", "-m", COMMIT_MSG, "--", CHECKOUT)
    sha = short_head()
    _redeploy(sha)
    psql("INSERT INTO deploys(kind, service, git_sha, previous_sha, detail, \"by\") VALUES "
         f"('deploy', 'shop_api', '{sha}', '{prev}', '{COMMIT_MSG}', 'ci')")
    return {"commit": sha, "previous": prev}


def rollback(info: dict) -> None:
    git("revert", "--no-edit", info["commit"])
    _redeploy(None)
    psql(f"DELETE FROM deploys WHERE git_sha = '{info['commit']}'")


__all__ = ["bad_deploy", "rollback", "util"]
