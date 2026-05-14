"""
Proof-of-concept Postgres-backed store for PD artifacts.

This module provides a minimal round-trip write/read path for Hammer
PD artifacts (currently just ``par-input.json``) into Postgres. Content
is keyed by the SHA256 of its canonical JSON representation, so identical
inputs deduplicate naturally.

Connection settings are resolved with this precedence (first hit wins per
field):

    1. HAMMER_PG_* environment variables
         HAMMER_PG_HOST / HAMMER_PG_PORT / HAMMER_PG_DB /
         HAMMER_PG_USER / HAMMER_PG_PASSWORD
    2. ``sql_alchemy_conn`` from ``airflow.cfg``
         (the same connection string Airflow uses for its metadata DB)
    3. Hardcoded defaults
         host=barney.eecs.berkeley.edu, port=5433,
         db=airflow_lawrence, user=$USER, password=<none>

So if your ``airflow.cfg`` already contains a valid
``sql_alchemy_conn = postgresql+psycopg2://user:pass@host:port/db`` line,
``pd_store`` will use it automatically with no env vars required.

Which ``airflow.cfg`` is used:
    ``$AIRFLOW_HOME/airflow.cfg`` if AIRFLOW_HOME is set, else
    ``<cwd>/airflow.cfg`` if present, else
    ``~/airflow/airflow.cfg``.

All data lives in the ``hammer_poc`` schema so it stays isolated from
Airflow's own tables.
"""

from __future__ import annotations

import configparser
import getpass
import gzip
import hashlib
import io
import json
import os
import tarfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import psycopg2
from psycopg2.extras import Json

from hammer.config import HammerJSONEncoder

__all__ = [
    "store_par_input",
    "store_artifact",
    "load_artifact",
    "list_artifacts",
    "ensure_schema",
    "compute_sha256",
    "compute_stage_key",
    "compute_rtl_fingerprint",
    "grant_access",
    "revoke_access",
    "store_master_database",
    "load_master_database",
    "store_stage_blob",
    "load_stage_blob",
    "list_stage_blobs",
    "tar_directory",
    "untar_to_directory",
    "KNOWN_STAGE_TAGS",
]


SCHEMA_NAME = "hammer_poc"
TABLE_NAME = "pd_artifacts"
FQ_TABLE = f"{SCHEMA_NAME}.{TABLE_NAME}"

MASTER_TABLE = "master_databases"
BLOB_TABLE = "pd_blobs"
FQ_MASTER = f"{SCHEMA_NAME}.{MASTER_TABLE}"
FQ_BLOB = f"{SCHEMA_NAME}.{BLOB_TABLE}"

# Everyone with access to the SledgeHammer Studio tables is in this role.
# Nobody gets direct table grants; access is purely group membership.
# A DBA has to create the role once on the cluster:
#   CREATE ROLE sledgehammer_users NOLOGIN;
#   GRANT sledgehammer_users TO lawrencejrhee WITH ADMIN OPTION;
SLEDGEHAMMER_GROUP = "sledgehammer_users"

KNOWN_STAGE_TAGS = (
    "synthesis", "par", "drc", "lvs",
    "sram_generator", "sim", "power", "formal", "timing", "pcb",
)


def _find_airflow_cfg() -> Optional[Path]:
    """Locate an ``airflow.cfg`` to use as a fallback settings source."""
    candidates = []
    if os.environ.get("AIRFLOW_HOME"):
        candidates.append(Path(os.environ["AIRFLOW_HOME"]) / "airflow.cfg")
    candidates.append(Path.cwd() / "airflow.cfg")
    candidates.append(Path.home() / "airflow" / "airflow.cfg")
    for p in candidates:
        if p.is_file():
            return p
    return None


def _parse_airflow_cfg_conn() -> Dict[str, Any]:
    """
    Pull host/port/db/user/password from ``sql_alchemy_conn`` in airflow.cfg.

    Returns an empty dict on any failure - this is a best-effort fallback,
    not a required source.
    """
    cfg_path = _find_airflow_cfg()
    if cfg_path is None:
        return {}
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(cfg_path)
    except Exception:
        return {}
    if not parser.has_option("database", "sql_alchemy_conn"):
        return {}
    conn_str = parser.get("database", "sql_alchemy_conn")
    # Strip SQLAlchemy driver prefix (e.g. "postgresql+psycopg2://") so
    # urlparse sees a recognized scheme.
    if "+" in conn_str.split("://", 1)[0]:
        scheme, rest = conn_str.split("://", 1)
        conn_str = scheme.split("+", 1)[0] + "://" + rest
    try:
        url = urlparse(conn_str)
    except Exception:
        return {}
    if url.scheme not in ("postgres", "postgresql"):
        return {}
    out: Dict[str, Any] = {}
    if url.hostname:
        out["host"] = url.hostname
    if url.port:
        out["port"] = url.port
    if url.username:
        out["user"] = unquote(url.username)
    if url.password:
        out["password"] = unquote(url.password)
    dbname = url.path.lstrip("/") if url.path else ""
    if dbname:
        out["dbname"] = dbname
    return out


def _pg_settings() -> Dict[str, Any]:
    """
    Gather Postgres connection settings.

    Precedence (first hit wins per field):
        1. HAMMER_PG_* environment variables
        2. sql_alchemy_conn from airflow.cfg
        3. Hardcoded defaults
    """
    cfg = _parse_airflow_cfg_conn()
    try:
        default_user = getpass.getuser()
    except Exception:
        default_user = "postgres"

    host = (
        os.environ.get("HAMMER_PG_HOST")
        or cfg.get("host")
        or "barney.eecs.berkeley.edu"
    )
    port = int(
        os.environ.get("HAMMER_PG_PORT")
        or cfg.get("port")
        or 5433
    )
    dbname = (
        os.environ.get("HAMMER_PG_DB")
        or cfg.get("dbname")
        or "airflow_lawrence"
    )
    user = (
        os.environ.get("HAMMER_PG_USER")
        or cfg.get("user")
        or default_user
    )
    password = (
        os.environ.get("HAMMER_PG_PASSWORD")
        or cfg.get("password")
    )
    if not password:
        raise RuntimeError(
            "No Postgres password found. Set HAMMER_PG_PASSWORD in the "
            "environment, or ensure airflow.cfg's sql_alchemy_conn "
            "contains a password."
        )
    return {
        "host": host,
        "port": port,
        "dbname": dbname,
        "user": user,
        "password": password,
    }


def _connect():
    """Open a new psycopg2 connection using env-var config."""
    return psycopg2.connect(**_pg_settings())


_DDL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA_NAME};

CREATE TABLE IF NOT EXISTS {FQ_TABLE} (
    sha256      TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    top_module  TEXT,
    data        JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS {FQ_MASTER} (
    design     TEXT PRIMARY KEY,
    db         JSONB NOT NULL,
    owner      TEXT NOT NULL DEFAULT current_user,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS {FQ_BLOB} (
    sha256     TEXT PRIMARY KEY,
    stage      TEXT NOT NULL,
    data       BYTEA NOT NULL,
    size_bytes BIGINT NOT NULL,
    owner      TEXT NOT NULL DEFAULT current_user,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Backfill the owner column for tables that already exist from earlier inits.
ALTER TABLE {FQ_MASTER} ADD COLUMN IF NOT EXISTS owner TEXT NOT NULL DEFAULT current_user;
ALTER TABLE {FQ_BLOB}   ADD COLUMN IF NOT EXISTS owner TEXT NOT NULL DEFAULT current_user;

CREATE INDEX IF NOT EXISTS idx_{BLOB_TABLE}_stage ON {FQ_BLOB} (stage);
CREATE INDEX IF NOT EXISTS idx_{BLOB_TABLE}_owner ON {FQ_BLOB} (owner);
CREATE INDEX IF NOT EXISTS idx_{MASTER_TABLE}_owner ON {FQ_MASTER} (owner);

-- Nobody gets access by default. The group role is the only way in.
REVOKE ALL ON SCHEMA {SCHEMA_NAME} FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA {SCHEMA_NAME} FROM PUBLIC;

-- Hand read+write on the schema to the group role, if it exists yet.
-- If a DBA hasn't created the role, this block just no-ops and we re-run init later.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{SLEDGEHAMMER_GROUP}') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA {SCHEMA_NAME} TO {SLEDGEHAMMER_GROUP}';
        EXECUTE 'GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA {SCHEMA_NAME} TO {SLEDGEHAMMER_GROUP}';
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA_NAME} GRANT SELECT, INSERT ON TABLES TO {SLEDGEHAMMER_GROUP}';
    END IF;
END $$;
"""


def _ensure_schema(conn) -> None:
    """Create the schema + table if they don't exist. Safe to call repeatedly."""
    with conn.cursor() as cur:
        cur.execute(_DDL)
    conn.commit()


def ensure_schema() -> None:
    """Public entry point to create the schema + table (idempotent)."""
    conn = _connect()
    try:
        _ensure_schema(conn)
    finally:
        conn.close()


def grant_access(role: str) -> None:
    """
    Add a Postgres role to the sledgehammer_users group.

    The user then inherits read and write on every SledgeHammer table. You
    need ADMIN OPTION on the group role for this to work; the DBA hands that
    over when they first create the role.
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"GRANT {SLEDGEHAMMER_GROUP} TO {role}")
        conn.commit()
    finally:
        conn.close()


def revoke_access(role: str) -> None:
    """Remove a Postgres role from the sledgehammer_users group."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"REVOKE {SLEDGEHAMMER_GROUP} FROM {role}")
        conn.commit()
    finally:
        conn.close()


def _canonical_json(data: Dict[str, Any]) -> str:
    """
    Produce a deterministic JSON string for hashing.

    Uses sorted keys and no whitespace so logically equal dicts
    produce identical bytes across runs/machines.
    """
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        cls=HammerJSONEncoder,
        ensure_ascii=False,
    )


def compute_sha256(data: Dict[str, Any]) -> str:
    """Return the hex SHA256 of the canonical JSON form of ``data``."""
    return hashlib.sha256(_canonical_json(data).encode("utf-8")).hexdigest()


def _extract_top_module(data: Dict[str, Any]) -> Optional[str]:
    """Best-effort extraction of the top module name for convenience."""
    for key in ("synthesis.inputs.top_module", "par.inputs.top_module", "vlsi.inputs.top_module"):
        val = data.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def store_artifact(data: Dict[str, Any], kind: str) -> str:
    """
    Store ``data`` as an artifact of the given ``kind`` and return its SHA256 hex.

    Uses INSERT ... ON CONFLICT DO NOTHING so identical content is deduplicated
    and repeated calls are idempotent.
    """
    sha = compute_sha256(data)
    top_module = _extract_top_module(data)
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {FQ_TABLE} (sha256, kind, top_module, data)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (sha256) DO NOTHING
                """,
                (sha, kind, top_module, Json(data)),
            )
        conn.commit()
    finally:
        conn.close()
    return sha


def store_par_input(data: Dict[str, Any]) -> str:
    """Convenience wrapper: store a par-input dict and return its SHA256 hex."""
    return store_artifact(data, kind="par-input")


def load_artifact(sha256: str) -> Optional[Dict[str, Any]]:
    """Fetch an artifact by SHA256. Returns the JSON payload as a dict, or None."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT data FROM {FQ_TABLE} WHERE sha256 = %s",
                (sha256,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return row[0]


def list_artifacts(limit: int = 20) -> List[Tuple[str, str, Optional[str], Any]]:
    """
    List the most recent artifacts.

    Returns tuples of (sha256, kind, top_module, created_at).
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT sha256, kind, top_module, created_at
                FROM {FQ_TABLE}
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return list(cur.fetchall())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SledgeHammer Studio: master_database + per-stage blob store.
#
# A stage's cache key is the SHA256 of the slice of master_database that
# stage_change_check actually compares: every key whose prefix is the stage tag
# (excluding outputs), plus every "global" key that is not owned by any stage.
# Stage-internal flags like *.needsToRerun are excluded since they are run
# bookkeeping, not cache inputs.
# ---------------------------------------------------------------------------


def _stage_relevant_keys(master_db: Dict[str, Any], stage_tag: str) -> Dict[str, Any]:
    own_prefix = stage_tag + "."
    output_prefix = stage_tag + ".outputs"
    other_prefixes = tuple(
        f"{tag}." for tag in KNOWN_STAGE_TAGS if tag != stage_tag
    )
    out: Dict[str, Any] = {}
    for k, v in master_db.items():
        if k.endswith(".needsToRerun"):
            continue
        if k.startswith(own_prefix):
            if not k.startswith(output_prefix):
                out[k] = v
        elif not k.startswith(other_prefixes):
            out[k] = v
    return out


def compute_stage_key(master_db: Dict[str, Any], stage_tag: str) -> str:
    """SHA256 over the master_database slice that determines this stage's output."""
    if stage_tag not in KNOWN_STAGE_TAGS:
        raise ValueError(
            f"Unknown stage tag {stage_tag!r}. Expected one of {KNOWN_STAGE_TAGS}."
        )
    return compute_sha256(_stage_relevant_keys(master_db, stage_tag))


def compute_rtl_fingerprint(file_paths: List[str]) -> str:
    """Hash the contents of the given RTL files in sorted order. Missing files get a placeholder."""
    h = hashlib.sha256()
    for path in sorted(file_paths):
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    h.update(chunk)
        except FileNotFoundError:
            h.update(f"MISSING:{path}".encode("utf-8"))
    return h.hexdigest()


def store_master_database(design: str, master_db: Dict[str, Any]) -> None:
    """Upsert the master_database for ``design``. Latest write wins."""
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {FQ_MASTER} (design, db, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (design) DO UPDATE
                  SET db = EXCLUDED.db,
                      updated_at = EXCLUDED.updated_at
                """,
                (design, Json(master_db)),
            )
        conn.commit()
    finally:
        conn.close()


def load_master_database(design: str) -> Optional[Dict[str, Any]]:
    """Fetch the master_database for ``design``, or None if not present."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT db FROM {FQ_MASTER} WHERE design = %s",
                (design,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def store_stage_blob(stage_tag: str, sha256: str, data: bytes) -> None:
    """Store a tarball under ``sha256``. Idempotent; same sha never re-inserts."""
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {FQ_BLOB} (sha256, stage, data, size_bytes)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (sha256) DO NOTHING
                """,
                (sha256, stage_tag, psycopg2.Binary(data), len(data)),
            )
        conn.commit()
    finally:
        conn.close()


def load_stage_blob(sha256: str) -> Optional[Tuple[str, bytes]]:
    """Fetch a tarball by hash. Returns ``(stage, bytes)`` or None."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT stage, data FROM {FQ_BLOB} WHERE sha256 = %s",
                (sha256,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    stage, data = row
    return stage, bytes(data)


def list_stage_blobs(
    stage_tag: Optional[str] = None,
    limit: int = 20,
) -> List[Tuple[str, str, int, Any]]:
    """List recent blobs. Returns ``(sha256, stage, size_bytes, created_at)``."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if stage_tag is None:
                cur.execute(
                    f"""
                    SELECT sha256, stage, size_bytes, created_at
                    FROM {FQ_BLOB}
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
            else:
                cur.execute(
                    f"""
                    SELECT sha256, stage, size_bytes, created_at
                    FROM {FQ_BLOB}
                    WHERE stage = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (stage_tag, limit),
                )
            return list(cur.fetchall())
    finally:
        conn.close()


def tar_directory(path: Path, arcname: Optional[str] = None) -> bytes:
    """gzip-compressed tar of ``path``. ``arcname`` controls the root entry."""
    path = Path(path)
    if not path.is_dir():
        raise FileNotFoundError(f"Not a directory: {path}")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(str(path), arcname=arcname or path.name, recursive=True)
    return buf.getvalue()


def untar_to_directory(data: bytes, dest: Path) -> None:
    """Extract a gzip tar into ``dest``. ``dest`` is created if it doesn't exist."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(path=str(dest))
