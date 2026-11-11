import os
import time

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.common import telemetry as t

DATABASE_URL = os.environ["DATABASE_URL"]

engine = create_async_engine(
    DATABASE_URL,
    pool_size=int(os.environ.get("DB_POOL_SIZE", "5")),
    max_overflow=int(os.environ.get("DB_MAX_OVERFLOW", "0")),
    pool_timeout=float(os.environ.get("DB_POOL_TIMEOUT_S", "30")),
    connect_args={"server_settings": {"application_name": t.SERVICE}},
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "checkout")
def _on_checkout(dbapi_conn, record, proxy):
    t.POOL_OUT.labels(t.SERVICE).inc()


@event.listens_for(engine.sync_engine, "checkin")
def _on_checkin(dbapi_conn, record):
    t.POOL_OUT.labels(t.SERVICE).dec()


@event.listens_for(engine.sync_engine, "before_cursor_execute")
def _count_query(conn, cursor, statement, parameters, context, executemany):
    t.DB_QUERIES.labels(t.SERVICE).inc()


async def get_session():
    t0 = time.perf_counter()
    async with engine.connect():  # borrow and return a connection: the time this takes is the pool wait
        pass
    t.POOL_WAIT.labels(t.SERVICE).observe(time.perf_counter() - t0)
    async with SessionLocal() as session:
        yield session
