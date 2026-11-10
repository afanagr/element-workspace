"""Celery application for background indexing."""

from __future__ import annotations

from celery import Celery

from app.config import settings

celery_app = Celery(
    "ai_file_workspace",
    broker=settings.redis_url,
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=False,
    task_soft_time_limit=900,
    task_time_limit=1200,
    # Results are never read by the API: skip the result backend entirely so a
    # stopped Redis cannot stall request handling with backend reconnects.
    task_ignore_result=True,
    task_store_errors_even_if_ignored=False,
    # Short, bounded publish retries keep `send_task` fast when the broker is down
    # (the API then falls back to in-process indexing).
    broker_transport_options={
        "max_retries": 1,
        "interval_start": 0,
        "interval_step": 0.2,
        "interval_max": 0.5,
        "socket_connect_timeout": 1,
        "socket_timeout": 1,
    },
    redis_socket_connect_timeout=1,
    redis_socket_timeout=1,
)

