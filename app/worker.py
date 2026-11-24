import logging
import time
from functools import cache
from uuid import UUID

import dramatiq
import httpx
from dramatiq.brokers.redis import RedisBroker
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from app.adapters import memory_jobs, memory_store, postgres
from app.settings import Settings

settings = Settings()
if settings.redis_url is None:
    raise ValueError("CONTEXT_REDIS_URL is required for the memory worker")
broker = RedisBroker(
    client=Redis.from_url(
        settings.redis_url.get_secret_value(),
        socket_connect_timeout=2,
        socket_timeout=2,
    )
)
dramatiq.set_broker(broker)
logger = logging.getLogger(__name__)


@cache
def database():
    return postgres.connect_database(settings.database_url.get_secret_value())


@dramatiq.actor(queue_name="memory", max_retries=8)
def apply_memory_job(tenant: str, job_id: str) -> None:
    enabled = frozenset(
        name
        for name, app in settings.applications.items()
        if app.memory_enabled and tenant in app.tenants
    )
    with httpx.Client(follow_redirects=False, trust_env=False) as client:
        memory_jobs.process(
            database(), client, tenant, UUID(job_id), settings.embedding, enabled
        )


def main() -> None:
    tenants = sorted(
        {tenant for app in settings.applications.values() for tenant in app.tenants}
    )
    while True:
        for tenant in tenants:
            try:
                for name, application in settings.applications.items():
                    if tenant in application.tenants:
                        with postgres.tenant_transaction(database(), tenant) as conn:
                            memory_store.expire_due(conn, tenant, name)
                with postgres.tenant_transaction(database(), tenant) as conn:
                    memory_jobs.dispatch(
                        conn,
                        tenant,
                        settings.embedding is not None,
                        apply_memory_job.send,
                    )
            except (SQLAlchemyError, RedisError):
                logger.warning(
                    "Memory dispatcher could not reach its dependency. Retrying."
                )
        time.sleep(2)


if __name__ == "__main__":
    main()
