import json
import logging
from typing import Any

import aio_pika
from aio_pika import DeliveryMode, ExchangeType, Message

from cfg import Cfg

logger = logging.getLogger("endpoint_api.rabbitmq_publisher")


async def send_trip_request(payload: dict[str, Any]) -> None:
    cfg = Cfg.from_env()
    logger.info(
        "Publishing trip request to exchange=%s routing_key=%s",
        cfg.rabbitmq_exchange,
        cfg.rabbitmq_routing_key,
    )
    connection = await aio_pika.connect_robust(cfg.amqp_url)
    try:
        channel = await connection.channel()
        exchange = await channel.declare_exchange(
            cfg.rabbitmq_exchange,
            ExchangeType.DIRECT,
            durable=True,
        )

        body = json.dumps(payload).encode("utf-8")
        message = Message(body=body, delivery_mode=DeliveryMode.PERSISTENT)

        await exchange.publish(message, routing_key=cfg.rabbitmq_routing_key)
        logger.info("Trip request published successfully")
    finally:
        await connection.close()
