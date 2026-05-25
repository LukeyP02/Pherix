/**
 * Adapter barrel — the public surface of the adapters axis.
 *
 * Every adapter re-exports through here so `src/index.ts` pulls the whole axis
 * with a single `export * from "./adapters/index.js"`. New adapters append their
 * export lines below; this is the one shared seam between parallel adapter
 * streams, so keep additions to single lines that union cleanly.
 */

// Protocol
export { isTransactionalAdapter, isStateDiffable } from "./base.js";
export type {
  ResourceAdapter,
  TransactionalResourceAdapter,
  StateDiffable,
  ToolFn,
} from "./base.js";

// Built-in adapters
export { SqliteAdapter, executeIsolated } from "./sql.js";
export type { SqliteDatabase } from "./sql.js";
export { HttpAdapter, IrreversibleAdapterError } from "./http.js";
export { FilesystemAdapter, FsHandle } from "./fs.js";
export { PostgresAdapter } from "./postgres.js";
export type { PgClient, PgResult } from "./postgres.js";
export { DynamoDbAdapter } from "./dynamodb.js";
export type { DynamoDbClient } from "./dynamodb.js";
export { GcsAdapter } from "./gcs.js";
export type { GcsClient, GcsBucket, GcsBlob } from "./gcs.js";
export { ElasticsearchAdapter } from "./elasticsearch.js";
export type { EsClient } from "./elasticsearch.js";
export { RestAdapter, restTool, graphqlTool } from "./rest.js";
export type { Transport, RestToolOptions, GraphqlToolOptions } from "./rest.js";
export { MqAdapter, publishTool, tombstoneCompensator } from "./messagequeue.js";
export type { Broker, PublishToolOptions, TombstoneCompensatorOptions } from "./messagequeue.js";
export { GitAdapter, GitHandle, GitError, shlexSplit } from "./git.js";
export { S3Adapter } from "./s3.js";
export type { S3Client } from "./s3.js";
export { RedisAdapter } from "./redis.js";
export type { RedisClient, RedisPipeline } from "./redis.js";
export { MongoAdapter } from "./mongodb.js";
export type { MongoDatabase, MongoCollection, MongoDoc } from "./mongodb.js";
export { MySQLAdapter } from "./mysql.js";
export type { MySQLConnection } from "./mysql.js";
