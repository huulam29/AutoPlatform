"""Event sources that turn external streams into runs (Kafka in v0.1)."""

from agentline.sources.consumer import (
    ConfluentConsumer,
    Consumer,
    FakeBroker,
    FakeConsumer,
    KafkaConsumerError,
    Message,
    TopicPartition,
)
from agentline.sources.kafka import KafkaSource, dedupe_key, json_payload

__all__ = [
    "ConfluentConsumer",
    "Consumer",
    "FakeBroker",
    "FakeConsumer",
    "KafkaConsumerError",
    "KafkaSource",
    "Message",
    "TopicPartition",
    "dedupe_key",
    "json_payload",
]
