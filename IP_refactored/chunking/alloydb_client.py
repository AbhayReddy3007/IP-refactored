"""
alloydb_client.py
──────────────────
AlloyDB (PostgreSQL + pgvector) backed storage layer for patent chunk
embeddings. Mimics a subset of the ChromaDB collection interface
(add / get / query / update / delete) so the rest of the chunking package
can treat it like a vector-store collection.

Schema (auto-created / auto-migrated on first use):
    embeddings (
        unique_id   TEXT PRIMARY KEY,
        sub_id      TEXT,
        collection  TEXT NOT NULL,
        text        TEXT,
        embedding   vector(EMBEDDING_DIM),
        metadata    JSONB DEFAULT '{}'
    )

All connection parameters come from IP_refactored.config (ALLOYDB_HOST,
ALLOYDB_PASSWORD, ALLOYDB_USER, ALLOYDB_DB, ALLOYDB_PORT, EMBEDDING_DIM).

Usage:
    from IP_refactored.chunking.alloydb_client import alloydb_client

    client = alloydb_client()
    collection = client.get_or_create_collection("patents_semaglutide")
"""

import atexit
import json
import logging
import time
import urllib.parse
from typing import Dict, List, Optional

import psycopg2
import psycopg2.extras

from .. import config

logger = logging.getLogger(__name__)

_open_connections: list = []


# ─────────────────────────────────────────────
# Connection management
# ─────────────────────────────────────────────

def _database_url() -> str:
    if not config.ALLOYDB_PASSWORD or not config.ALLOYDB_HOST:
        raise EnvironmentError(
            "ALLOYDB_PASSWORD and ALLOYDB_HOST must be set (see config.py) "
            "before connecting to AlloyDB."
        )
    encoded_password = urllib.parse.quote_plus(config.ALLOYDB_PASSWORD)
    return (
        f"postgresql://{config.ALLOYDB_USER}:{encoded_password}"
        f"@{config.ALLOYDB_HOST}:{config.ALLOYDB_PORT}/{config.ALLOYDB_DB}"
    )


def _get_conn(retries: int = 3, backoff: float = 2.0):
    """Get a new psycopg2 connection with retry logic for transient failures."""
    last_err = None
    url = _database_url()
    for attempt in range(retries):
        try:
            conn = psycopg2.connect(url, connect_timeout=30)
            _open_connections.append(conn)
            return conn
        except psycopg2.OperationalError as e:
            last_err = e
            if attempt < retries - 1:
                wait = backoff * (2 ** attempt)
                logger.warning(
                    "[ALLOYDB] Connection attempt %d/%d failed: %s — retrying in %ss",
                    attempt + 1, retries, e, wait,
                )
                time.sleep(wait)
            else:
                logger.error("[ALLOYDB] All %d connection attempts failed.", retries)
    raise last_err


def _cleanup_connections():
    """Close all open connections before Python teardown."""
    for conn in _open_connections:
        try:
            if conn and not conn.closed:
                conn.close()
        except Exception:
            pass
    _open_connections.clear()


atexit.register(_cleanup_connections)


# ─────────────────────────────────────────────
# Schema management — lazy, never runs at import time
# ─────────────────────────────────────────────

_schema_ready = False


def _ensure_schema():
    conn = _get_conn()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")

            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables WHERE table_name = 'embeddings'
                )
            """)
            table_exists = cur.fetchone()[0]

            if not table_exists:
                cur.execute(f"""
                    CREATE TABLE embeddings (
                        unique_id   TEXT PRIMARY KEY,
                        sub_id      TEXT DEFAULT '',
                        collection  TEXT NOT NULL DEFAULT '',
                        text        TEXT DEFAULT '',
                        embedding   vector({config.EMBEDDING_DIM}),
                        metadata    JSONB DEFAULT '{{}}'
                    )
                """)
                logger.info("[ALLOYDB] Created embeddings table")
            else:
                cur.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'embeddings'
                """)
                existing_cols = {row[0] for row in cur.fetchall()}

                if "collection" not in existing_cols:
                    cur.execute("ALTER TABLE embeddings ADD COLUMN collection TEXT NOT NULL DEFAULT ''")
                    logger.info("[ALLOYDB] Added 'collection' column")

                if "metadata" not in existing_cols:
                    cur.execute("ALTER TABLE embeddings ADD COLUMN metadata JSONB DEFAULT '{}'")
                    logger.info("[ALLOYDB] Added 'metadata' column")

            cur.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_collection ON embeddings (collection)")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_embeddings_metadata_filename "
                "ON embeddings ((metadata->>'filename'))"
            )

            # Drop legacy indexes — full-precision HNSW/IVFFlat cap at 2000
            # dims, but our embeddings are EMBEDDING_DIM (3072 by default).
            cur.execute("DROP INDEX IF EXISTS idx_embeddings_hnsw")
            cur.execute("DROP INDEX IF EXISTS idx_embeddings_ivfflat")

            try:
                cur.execute(f"""
                    CREATE INDEX IF NOT EXISTS idx_embeddings_hnsw_halfvec
                    ON embeddings USING hnsw
                    ((embedding::halfvec({config.EMBEDDING_DIM})) halfvec_cosine_ops)
                """)
                logger.info("[ALLOYDB] HNSW halfvec index created/verified")
            except Exception as e:
                logger.warning(
                    "[ALLOYDB] HNSW halfvec index skipped: %s — queries will use "
                    "exact (sequential) scan, still correct, just slower.", e,
                )
    finally:
        conn.close()

    logger.info("[ALLOYDB] Schema verified / migrated")


def _lazy_ensure_schema():
    global _schema_ready
    if not _schema_ready:
        _ensure_schema()
        _schema_ready = True


# ─────────────────────────────────────────────
# Collection abstraction
# ─────────────────────────────────────────────

class AlloyDBCollection:
    """Mimics the ChromaDB collection interface, backed by the shared
    `embeddings` table filtered by a `collection` column."""

    def __init__(self, name: str):
        self.name = name

    def add(self, ids: List[str], documents: List[str], embeddings: List[List[float]], metadatas: List[dict]):
        """Insert rows. Uses ON CONFLICT to upsert."""
        _lazy_ensure_schema()
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                for uid, doc, emb, meta in zip(ids, documents, embeddings, metadatas):
                    emb_str = "[" + ",".join(str(v) for v in emb) + "]"
                    cur.execute("""
                        INSERT INTO embeddings (unique_id, sub_id, collection, text, embedding, metadata)
                        VALUES (%s, %s, %s, %s, %s::vector, %s::jsonb)
                        ON CONFLICT (unique_id) DO UPDATE SET
                            sub_id     = EXCLUDED.sub_id,
                            collection = EXCLUDED.collection,
                            text       = EXCLUDED.text,
                            embedding  = EXCLUDED.embedding,
                            metadata   = EXCLUDED.metadata
                    """, (uid, meta.get("sub_id", ""), self.name, doc, emb_str, json.dumps(meta)))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get(self, ids: Optional[List[str]] = None, where: Optional[dict] = None,
            include: Optional[List[str]] = None) -> dict:
        """Retrieve rows by ids or by metadata filter."""
        _lazy_ensure_schema()
        include = include or []
        conn = _get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if ids is not None:
                    placeholders = ",".join(["%s"] * len(ids))
                    cur.execute(f"""
                        SELECT unique_id, text, embedding, metadata FROM embeddings
                        WHERE collection = %s AND unique_id IN ({placeholders})
                    """, [self.name] + ids)
                elif where is not None:
                    sql, params = _build_where_clause(where)
                    cur.execute(f"""
                        SELECT unique_id, text, embedding, metadata FROM embeddings
                        WHERE collection = %s AND {sql}
                    """, [self.name] + params)
                else:
                    cur.execute("""
                        SELECT unique_id, text, embedding, metadata FROM embeddings
                        WHERE collection = %s
                    """, [self.name])
                rows = cur.fetchall()
        finally:
            conn.close()

        result = {
            "ids": [r["unique_id"] for r in rows],
            "documents": [r["text"] for r in rows] if "documents" in include else [],
            "metadatas": [r["metadata"] for r in rows] if "metadatas" in include else [],
            "embeddings": [],
        }
        if "embeddings" in include:
            result["embeddings"] = [_parse_vector(r["embedding"]) for r in rows]
        if not include:
            # Sentinel/existence checks rely on metadatas being present even
            # when include isn't specified.
            result["metadatas"] = [r["metadata"] for r in rows]
        return result

    def query(self, query_embeddings: List[List[float]], n_results: int = 5,
              where: Optional[dict] = None, include: Optional[List[str]] = None) -> dict:
        """Vector similarity search using pgvector cosine distance (<=>)."""
        _lazy_ensure_schema()
        include = include or ["documents"]
        emb = query_embeddings[0]
        emb_str = "[" + ",".join(str(v) for v in emb) + "]"

        conn = _get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                where_sql = "collection = %s"
                params = [self.name]
                if where:
                    extra_sql, extra_params = _build_where_clause(where)
                    where_sql += f" AND {extra_sql}"
                    params += extra_params

                dim = config.EMBEDDING_DIM
                cur.execute(f"""
                    SELECT unique_id, text, embedding, metadata,
                           embedding::halfvec({dim}) <=> %s::halfvec({dim}) AS distance
                    FROM embeddings
                    WHERE {where_sql}
                    ORDER BY distance ASC
                    LIMIT %s
                """, [emb_str] + params + [n_results])
                rows = cur.fetchall()
        finally:
            conn.close()

        return {
            "ids": [[r["unique_id"] for r in rows]],
            "documents": [[r["text"] for r in rows]] if "documents" in include else [[]],
            "metadatas": [[r["metadata"] for r in rows]] if "metadatas" in include else [[]],
            "distances": [[r["distance"] for r in rows]] if "distances" in include else [[]],
        }

    def update(self, ids: List[str], metadatas: List[dict]):
        """Update metadata for existing rows."""
        _lazy_ensure_schema()
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                for uid, meta in zip(ids, metadatas):
                    cur.execute("""
                        UPDATE embeddings SET metadata = %s::jsonb
                        WHERE unique_id = %s AND collection = %s
                    """, (json.dumps(meta), uid, self.name))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def delete(self, ids: Optional[List[str]] = None, where: Optional[dict] = None):
        """Delete rows by ids or metadata filter."""
        _lazy_ensure_schema()
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                if ids is not None:
                    placeholders = ",".join(["%s"] * len(ids))
                    cur.execute(f"""
                        DELETE FROM embeddings WHERE collection = %s AND unique_id IN ({placeholders})
                    """, [self.name] + ids)
                elif where is not None:
                    sql, params = _build_where_clause(where)
                    cur.execute(f"""
                        DELETE FROM embeddings WHERE collection = %s AND {sql}
                    """, [self.name] + params)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ─────────────────────────────────────────────
# AlloyDB client (replaces chromadb.PersistentClient)
# ─────────────────────────────────────────────

class AlloyDBClient:
    """Mimics chromadb.PersistentClient interface. Collections are logical
    namespaces within the shared `embeddings` table."""

    def get_collection(self, name: str) -> AlloyDBCollection:
        _lazy_ensure_schema()
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM embeddings WHERE collection = %s LIMIT 1", (name,))
                if not cur.fetchone():
                    raise ValueError(f"Collection '{name}' does not exist")
        finally:
            conn.close()
        return AlloyDBCollection(name)

    def create_collection(self, name: str, metadata: dict = None) -> AlloyDBCollection:
        """Collections are just a column value — this is a no-op; the
        collection starts existing once rows are inserted."""
        logger.info("[ALLOYDB] Collection ready: %s", name)
        return AlloyDBCollection(name)

    def get_or_create_collection(self, name: str, metadata: dict = None) -> AlloyDBCollection:
        return AlloyDBCollection(name)

    def delete_collection(self, name: str):
        _lazy_ensure_schema()
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM embeddings WHERE collection = %s", (name,))
            conn.commit()
            logger.info("[ALLOYDB] Deleted collection '%s'", name)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_collections(self) -> List:
        """List all distinct collection names. Returns objects with a .name attr."""
        _lazy_ensure_schema()
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT collection FROM embeddings WHERE collection LIKE 'patents_%'")
                rows = cur.fetchall()
        finally:
            conn.close()

        class _ColRef:
            def __init__(self, name):
                self.name = name

        return [_ColRef(r[0]) for r in rows]


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _parse_vector(val) -> List[float]:
    """Parse a pgvector string like '[0.1,0.2,...]' into a list of floats."""
    if val is None:
        return []
    if isinstance(val, list):
        return val
    s = str(val).strip("[]")
    return [float(x) for x in s.split(",")] if s else []


def _build_where_clause(where: dict) -> tuple:
    """Convert a ChromaDB-style where filter to SQL (supports $and / $or / ops)."""
    if "$and" in where:
        clauses, params = [], []
        for sub in where["$and"]:
            c, p = _build_where_clause(sub)
            clauses.append(c)
            params.extend(p)
        return "(" + " AND ".join(clauses) + ")", params

    if "$or" in where:
        clauses, params = [], []
        for sub in where["$or"]:
            c, p = _build_where_clause(sub)
            clauses.append(c)
            params.extend(p)
        return "(" + " OR ".join(clauses) + ")", params

    for key, val in where.items():
        if isinstance(val, dict):
            for op, operand in val.items():
                sql_op = {"$eq": "=", "$ne": "!=", "$gt": ">", "$gte": ">=",
                          "$lt": "<", "$lte": "<="}.get(op, "=")
                if isinstance(operand, (int, float)):
                    return f"(metadata->>'{key}')::float {sql_op} %s", [operand]
                return f"metadata->>'{key}' {sql_op} %s", [str(operand)]
        else:
            return f"metadata->>'{key}' = %s", [str(val)]

    return "TRUE", []


# ─────────────────────────────────────────────
# Public entry point — singleton accessor
# ─────────────────────────────────────────────

_client_singleton: Optional[AlloyDBClient] = None


def alloydb_client() -> AlloyDBClient:
    """Return the shared (singleton) AlloyDBClient instance.

    Connection details are read lazily from config.py on first actual use —
    nothing connects to AlloyDB at import time.
    """
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = AlloyDBClient()
    return _client_singleton
