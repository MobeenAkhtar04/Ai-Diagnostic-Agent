"""Background HTTP load profiles used by the chaos scenarios (run as `python -m chaos.load <profile>`)."""

from __future__ import annotations

import asyncio
import os
import random
import signal
import subprocess
import sys

import httpx

from .util import API, STATE_DIR


async def _loop(client: httpx.AsyncClient, make, sleep: float, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            method, url, body = make()
            await client.request(method, url, json=body)
        except httpx.HTTPError:
            pass
        await asyncio.sleep(sleep * (0.5 + random.random()))


def _cust() -> str:
    return f"cust-{random.randint(1, 5000)}"


def _any_cust() -> str:
    """Mostly customers with no orders (new accounts): without ix_orders_customer_id each lookup scans the whole table."""
    return f"cust-{random.randint(1, 200000)}"


def _checkout(sku: str):
    return lambda: ("POST", "/checkout", {"customer_id": _cust(), "items": [{"sku": sku, "qty": 1, "price_cents": 1500}]})


def _bundle():
    """Bundle promo: two hot SKUs per cart, in whatever order the customer added them."""
    pair = ["SKU-1", "SKU-2"]
    random.shuffle(pair)
    return "POST", "/checkout", {"customer_id": _cust(),
                                 "items": [{"sku": s, "qty": 1, "price_cents": 1500} for s in pair]}


def _surge():
    r = random.random()
    if r < 0.5:
        return "GET", f"/products/SKU-{random.randint(1, 200)}", None
    if r < 0.7:
        return "GET", f"/orders?customer_id={_cust()}", None
    return _checkout(f"SKU-{random.randint(1, 200)}")()


def _noise():
    r = random.random()
    if r < 0.6:
        return "GET", f"/products/DISCONTINUED-{random.randint(1, 50)}", None
    if r < 0.9:
        return "GET", "/orders?customer_id=", None
    return "GET", f"/receipts/{random.randint(900000000, 999999999)}", None


# name -> (concurrency, request factory, sleep between requests per worker)
PROFILES = {
    "receipts": (25, lambda: ("GET", f"/receipts/{random.randint(1, 200000)}", None), 0.0),
    "orders": (40, lambda: ("GET", f"/orders?customer_id={_any_cust()}", None), 0.0),
    "hot_checkout": (12, _checkout("SKU-HOT-1"), 0.05),
    "recs": (20, lambda: ("GET", f"/recs/{_cust()}", None), 0.1),
    "surge": (40, _surge, 0.1),
    "bundle": (6, _bundle, 0.05),
    "checkout": (8, lambda: _checkout(f"SKU-{random.randint(3, 200)}")(), 0.2),
    "noise": (2, _noise, 1.0),
}


async def run(profile: str) -> None:
    conc, make, sleep = PROFILES[profile]
    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    async with httpx.AsyncClient(base_url=API, timeout=20, limits=httpx.Limits(max_connections=conc + 5)) as c:
        await asyncio.gather(*[_loop(c, make, sleep, stop) for _ in range(conc)])


def start(profile: str) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    stop(profile)
    log = open(STATE_DIR / f"load-{profile}.log", "w")  # noqa: SIM115
    p = subprocess.Popen([sys.executable, "-m", "chaos.load", profile], stdout=log, stderr=log,
                         start_new_session=True, cwd=STATE_DIR.parent)
    (STATE_DIR / f"load-{profile}.pid").write_text(str(p.pid))


def stop(profile: str) -> None:
    f = STATE_DIR / f"load-{profile}.pid"
    if not f.exists():
        return
    try:
        os.kill(int(f.read_text()), signal.SIGTERM)
    except (ProcessLookupError, ValueError):
        pass
    f.unlink()


def stop_all() -> None:
    for name in PROFILES:
        stop(name)


def running() -> list[str]:
    out = []
    for name in PROFILES:
        f = STATE_DIR / f"load-{name}.pid"
        if f.exists():
            try:
                os.kill(int(f.read_text()), 0)
                out.append(name)
            except (ProcessLookupError, ValueError):
                f.unlink()
    return out


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1]))
