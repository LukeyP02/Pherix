"""Resource adapters — the per-backend ``snapshot/apply/restore`` triples.

Importing any adapter class here is dependency-free: each adapter lazy-imports
its driver (``psycopg``, ``pymysql``, ``pymongo``, ``boto3``, ``redis``,
``google-cloud-storage``, ``elasticsearch``) inside its own ``__init__``/methods,
never at module load. ``import pherix`` therefore works with zero third-party
packages; the driver is only needed to *instantiate* the matching adapter,
pulled via the optional extra (``pherix[postgres]`` etc.).
"""

from pherix.core.adapters.base import (
    ResourceAdapter,
    SnapshotHandle,
    StateDiffable,
    TransactionalResourceAdapter,
    VersionedResourceAdapter,
)
from pherix.core.adapters.dynamodb import DynamoDBAdapter
from pherix.core.adapters.elasticsearch import ElasticsearchAdapter
from pherix.core.adapters.filesystem import FilesystemAdapter
from pherix.core.adapters.gcs import GCSAdapter
from pherix.core.adapters.http import HTTPAdapter, IrreversibleAdapterError
from pherix.core.adapters.messagequeue import (
    Broker,
    MQAdapter,
    publish_tool,
    tombstone_compensator,
)
from pherix.core.adapters.mongodb import MongoAdapter
from pherix.core.adapters.mysql import MySQLAdapter
from pherix.core.adapters.postgres import PostgresAdapter
from pherix.core.adapters.redis import RedisAdapter
from pherix.core.adapters.rest import RESTAdapter, graphql_tool, rest_tool
from pherix.core.adapters.s3 import S3Adapter
from pherix.core.adapters.sql import SQLiteAdapter

__all__ = [
    # protocols
    "ResourceAdapter",
    "TransactionalResourceAdapter",
    "VersionedResourceAdapter",
    "StateDiffable",
    "SnapshotHandle",
    # reversible (snapshot / savepoint lane)
    "SQLiteAdapter",
    "PostgresAdapter",
    "MySQLAdapter",
    "FilesystemAdapter",
    "MongoAdapter",
    "S3Adapter",
    "RedisAdapter",
    "DynamoDBAdapter",
    "GCSAdapter",
    "ElasticsearchAdapter",
    # irreversible (staged / compensated lane)
    "HTTPAdapter",
    "RESTAdapter",
    "rest_tool",
    "graphql_tool",
    "MQAdapter",
    "Broker",
    "publish_tool",
    "tombstone_compensator",
    "IrreversibleAdapterError",
]
