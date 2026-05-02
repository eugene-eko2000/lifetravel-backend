import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Cfg:
    endpoint_port: int
    amqp_host: str
    amqp_port: str
    amqp_user: str
    amqp_password: str
    rabbitmq_exchange: str
    rabbitmq_routing_key: str
    rabbitmq_missing_info_routing_key: str
    rabbitmq_missing_info_queue: str
    rabbitmq_ranked_routing_key: str
    rabbitmq_ranked_queue: str
    rabbitmq_empty_trip_routing_key: str
    rabbitmq_empty_trip_queue: str
    rabbitmq_debug_routing_key: str
    rabbitmq_debug_queue: str
    rabbitmq_status_routing_key: str
    rabbitmq_status_queue: str

    @classmethod
    def from_env(cls) -> "Cfg":
        return cls(
            endpoint_port=int(os.getenv("PORT", "8080")),
            amqp_host=os.getenv("AMQP_HOST", "localhost"),
            amqp_port=os.getenv("AMQP_PORT", "5672"),
            amqp_user=os.getenv("AMQP_USER", "guest"),
            amqp_password=os.getenv("AMQP_PASSWORD", "guest"),
            rabbitmq_exchange=os.getenv("RABBITMQ_EXCHANGE", "lifetravel_agent"),
            rabbitmq_routing_key=os.getenv(
                "RABBITMQ_TRIP_REQUEST_ROUTING_KEY",
                "trip:user_request",
            ),
            rabbitmq_missing_info_routing_key=os.getenv(
                "RABBITMQ_MISSING_INFO_ROUTING_KEY",
                "trip:missing_info",
            ),
            rabbitmq_missing_info_queue=os.getenv(
                "RABBITMQ_MISSING_INFO_QUEUE",
                "endpoint_api_missing_info_queue",
            ),
            rabbitmq_ranked_routing_key=os.getenv(
                "RABBITMQ_RANKED_ROUTING_KEY",
                "trip:ranked",
            ),
            rabbitmq_ranked_queue=os.getenv(
                "RABBITMQ_RANKED_QUEUE",
                "endpoint_api_ranked_queue",
            ),
            rabbitmq_empty_trip_routing_key=os.getenv(
                "RABBITMQ_EMPTY_TRIP_ROUTING_KEY",
                "trip:empty",
            ),
            rabbitmq_empty_trip_queue=os.getenv(
                "RABBITMQ_EMPTY_TRIP_QUEUE",
                "endpoint_api_empty_trip_queue",
            ),
            rabbitmq_debug_routing_key=os.getenv(
                "RABBITMQ_DEBUG_ROUTING_KEY",
                "debug:message",
            ),
            rabbitmq_debug_queue=os.getenv(
                "RABBITMQ_DEBUG_QUEUE",
                "endpoint_api_debug_queue",
            ),
            rabbitmq_status_routing_key=os.getenv(
                "RABBITMQ_STATUS_ROUTING_KEY",
                "status:message",
            ),
            rabbitmq_status_queue=os.getenv(
                "RABBITMQ_STATUS_QUEUE",
                "endpoint_api_status_queue",
            ),
        )

    @property
    def amqp_url(self) -> str:
        return (
            f"amqp://{self.amqp_user}:"
            f"{self.amqp_password}@{self.amqp_host}:{self.amqp_port}/"
        )
