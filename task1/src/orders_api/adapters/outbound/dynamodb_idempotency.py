"""Idempotency records in their own DynamoDB table (ADRs 0005 and 0009)."""

from aws_lambda_powertools.utilities.idempotency import (
    BasePersistenceLayer,
    DynamoDBPersistenceLayer,
)


def dynamodb_store(table_name: str) -> BasePersistenceLayer:
    """The idempotency table in Powertools' default layout: key `id`, TTL `expiration`."""
    return DynamoDBPersistenceLayer(table_name=table_name)
