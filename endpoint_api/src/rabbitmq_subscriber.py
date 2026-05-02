import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

import aio_pika
from aio_pika import ExchangeType

from cfg import Cfg

logger = logging.getLogger("endpoint_api.rabbitmq_subscriber")

_RECONNECT_DELAY_SEC = 0.1


def _log_background_task(task: asyncio.Task[None]) -> None:
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        logger.error(
            "Background subscriber message task failed",
            exc_info=(type(exc), exc, exc.__traceback__),
        )


async def _run_subscriber_message(
    incoming: aio_pika.abc.AbstractIncomingMessage,
    on_message: Callable[[dict[str, Any]], Awaitable[None]],
    log_label: str,
) -> None:
    async with incoming.process():
        try:
            payload: dict[str, Any] = json.loads(incoming.body.decode("utf-8"))
        except Exception:
            logger.exception("Invalid JSON in %s message", log_label)
            return
        try:
            await on_message(payload)
        except Exception:
            logger.exception("Failed handling %s message", log_label)


async def run_missing_info_subscriber(
    on_message: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    cfg = Cfg.from_env()
    logger.info(
        "Starting missing-info subscriber exchange=%s routing_key=%s queue=%s",
        cfg.rabbitmq_exchange,
        cfg.rabbitmq_missing_info_routing_key,
        cfg.rabbitmq_missing_info_queue,
    )

    while True:
        try:
            connection = await aio_pika.connect_robust(cfg.amqp_url)
            async with connection:
                channel = await connection.channel()
                exchange = await channel.declare_exchange(
                    cfg.rabbitmq_exchange,
                    ExchangeType.DIRECT,
                    durable=True,
                )
                queue = await channel.declare_queue(cfg.rabbitmq_missing_info_queue, durable=True)
                await queue.bind(exchange, routing_key=cfg.rabbitmq_missing_info_routing_key)

                async with queue.iterator() as queue_iter:
                    async for incoming in queue_iter:
                        task = asyncio.create_task(
                            _run_subscriber_message(
                                incoming, on_message, "missing-info"
                            )
                        )
                        task.add_done_callback(_log_background_task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "missing-info subscriber connection or consumer loop failed; reconnecting in %s s",
                _RECONNECT_DELAY_SEC,
            )
            await asyncio.sleep(_RECONNECT_DELAY_SEC)


async def run_ranked_subscriber(
    on_message: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    cfg = Cfg.from_env()
    logger.info(
        "Starting ranked subscriber exchange=%s routing_key=%s queue=%s",
        cfg.rabbitmq_exchange,
        cfg.rabbitmq_ranked_routing_key,
        cfg.rabbitmq_ranked_queue,
    )

    while True:
        try:
            connection = await aio_pika.connect_robust(cfg.amqp_url)
            async with connection:
                channel = await connection.channel()
                exchange = await channel.declare_exchange(
                    cfg.rabbitmq_exchange,
                    ExchangeType.DIRECT,
                    durable=True,
                )
                queue = await channel.declare_queue(cfg.rabbitmq_ranked_queue, durable=True)
                await queue.bind(exchange, routing_key=cfg.rabbitmq_ranked_routing_key)

                async with queue.iterator() as queue_iter:
                    async for incoming in queue_iter:
                        task = asyncio.create_task(
                            _run_subscriber_message(incoming, on_message, "ranked")
                        )
                        task.add_done_callback(_log_background_task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "ranked subscriber connection or consumer loop failed; reconnecting in %s s",
                _RECONNECT_DELAY_SEC,
            )
            await asyncio.sleep(_RECONNECT_DELAY_SEC)


async def run_empty_trip_subscriber(
    on_message: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    cfg = Cfg.from_env()
    logger.info(
        "Starting empty-trip subscriber exchange=%s routing_key=%s queue=%s",
        cfg.rabbitmq_exchange,
        cfg.rabbitmq_empty_trip_routing_key,
        cfg.rabbitmq_empty_trip_queue,
    )

    while True:
        try:
            connection = await aio_pika.connect_robust(cfg.amqp_url)
            async with connection:
                channel = await connection.channel()
                exchange = await channel.declare_exchange(
                    cfg.rabbitmq_exchange,
                    ExchangeType.DIRECT,
                    durable=True,
                )
                queue = await channel.declare_queue(cfg.rabbitmq_empty_trip_queue, durable=True)
                await queue.bind(exchange, routing_key=cfg.rabbitmq_empty_trip_routing_key)

                async with queue.iterator() as queue_iter:
                    async for incoming in queue_iter:
                        task = asyncio.create_task(
                            _run_subscriber_message(
                                incoming, on_message, "empty-trip"
                            )
                        )
                        task.add_done_callback(_log_background_task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "empty-trip subscriber connection or consumer loop failed; reconnecting in %s s",
                _RECONNECT_DELAY_SEC,
            )
            await asyncio.sleep(_RECONNECT_DELAY_SEC)


async def run_debug_subscriber(
    on_message: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    cfg = Cfg.from_env()
    logger.info(
        "Starting debug subscriber exchange=%s routing_key=%s queue=%s",
        cfg.rabbitmq_exchange,
        cfg.rabbitmq_debug_routing_key,
        cfg.rabbitmq_debug_queue,
    )

    while True:
        try:
            connection = await aio_pika.connect_robust(cfg.amqp_url)
            async with connection:
                channel = await connection.channel()
                exchange = await channel.declare_exchange(
                    cfg.rabbitmq_exchange,
                    ExchangeType.DIRECT,
                    durable=True,
                )
                queue = await channel.declare_queue(cfg.rabbitmq_debug_queue, durable=True)
                await queue.bind(exchange, routing_key=cfg.rabbitmq_debug_routing_key)

                async with queue.iterator() as queue_iter:
                    async for incoming in queue_iter:
                        task = asyncio.create_task(
                            _run_subscriber_message(incoming, on_message, "debug")
                        )
                        task.add_done_callback(_log_background_task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "debug subscriber connection or consumer loop failed; reconnecting in %s s",
                _RECONNECT_DELAY_SEC,
            )
            await asyncio.sleep(_RECONNECT_DELAY_SEC)


async def run_status_subscriber(
    on_message: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    cfg = Cfg.from_env()
    logger.info(
        "Starting status subscriber exchange=%s routing_key=%s queue=%s",
        cfg.rabbitmq_exchange,
        cfg.rabbitmq_status_routing_key,
        cfg.rabbitmq_status_queue,
    )

    while True:
        try:
            connection = await aio_pika.connect_robust(cfg.amqp_url)
            async with connection:
                channel = await connection.channel()
                exchange = await channel.declare_exchange(
                    cfg.rabbitmq_exchange,
                    ExchangeType.DIRECT,
                    durable=True,
                )
                queue = await channel.declare_queue(cfg.rabbitmq_status_queue, durable=True)
                await queue.bind(exchange, routing_key=cfg.rabbitmq_status_routing_key)

                async with queue.iterator() as queue_iter:
                    async for incoming in queue_iter:
                        task = asyncio.create_task(
                            _run_subscriber_message(incoming, on_message, "status")
                        )
                        task.add_done_callback(_log_background_task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "status subscriber connection or consumer loop failed; reconnecting in %s s",
                _RECONNECT_DELAY_SEC,
            )
            await asyncio.sleep(_RECONNECT_DELAY_SEC)
