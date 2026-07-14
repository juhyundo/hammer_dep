"""Where each user's TOTP secret lives.

Two backends behind one small interface:

  SqliteTotpStore   - a single file, no server. The demo uses this so it runs
                      anywhere without a database.
  PostgresTotpStore - the hammer_poc.user_totp table in the same Postgres the
                      login whitelist and PD cache already use. This is what the
                      real Airflow deployment uses, so secrets sit beside the
                      rest of the auth state.

A row holds the base32 secret, whether the user finished enrollment (scanned the
QR and proved one code), the last time-step a code was accepted for them, and a
failed-attempt counter with a lockout window. The last-step value is the replay
guard: once a code is used, the login path refuses that step and any earlier one,
so the same code can't be replayed. The failed-attempt counter bounds online
guessing per user (not per session), so retrying the password can't reset it.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Enrollment:
    uid: str
    secret: str
    confirmed: bool
    last_step: Optional[int]


class TotpStore:
    """Interface every backend implements."""

    def get(self, uid: str) -> Optional[Enrollment]:
        raise NotImplementedError

    def start_enrollment(self, uid: str, secret: str) -> None:
        """Store a fresh, unconfirmed secret, replacing any earlier one."""
        raise NotImplementedError

    def confirm(self, uid: str) -> None:
        """Mark the user as enrolled once they've proven a code."""
        raise NotImplementedError

    def record_step(self, uid: str, step: int) -> None:
        """Remember the last accepted time-step (replay guard)."""
        raise NotImplementedError

    def locked_until(self, uid: str) -> Optional[float]:
        """Unix time the user is locked out until, or None."""
        raise NotImplementedError

    def note_failure(self, uid: str, threshold: int, cooldown: float,
                     at: float) -> Optional[float]:
        """Count one wrong code. After `threshold` of them, lock the account for
        `cooldown` seconds and reset the counter. Returns the lock expiry if it
        locked, else None. This lives in the store, not the session, so retrying
        the password can't reset it.
        """
        raise NotImplementedError

    def reset_failures(self, uid: str) -> None:
        """Clear the failed-code counter and any lock (called on success)."""
        raise NotImplementedError

    def delete(self, uid: str) -> None:
        """Drop the user's enrollment so they start over (admin reset)."""
        raise NotImplementedError

    def list_enrolled(self) -> List[Enrollment]:
        raise NotImplementedError

    # Conveniences shared by every backend.
    def is_enrolled(self, uid: str) -> bool:
        enr = self.get(uid)
        return bool(enr and enr.confirmed)

    def has_pending(self, uid: str) -> bool:
        enr = self.get(uid)
        return bool(enr and not enr.confirmed)


def _norm(uid: str) -> str:
    return (uid or "").strip().lower()


class SqliteTotpStore(TotpStore):
    """File-backed store for the demo and for testing."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.environ.get(
            "SLEDGE_2FA_DB",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_2fa.sqlite"),
        )
        self._lock = threading.Lock()
        self._init()

    def _conn(self):
        return sqlite3.connect(self.path)

    def _init(self) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS user_totp (
                    uid             TEXT PRIMARY KEY,
                    secret          TEXT NOT NULL,
                    confirmed       INTEGER NOT NULL DEFAULT 0,
                    last_step       INTEGER,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_until    REAL,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    confirmed_at    TEXT
                )
                """
            )

    def get(self, uid: str) -> Optional[Enrollment]:
        uid = _norm(uid)
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT uid, secret, confirmed, last_step FROM user_totp WHERE uid = ?",
                (uid,),
            ).fetchone()
        if not row:
            return None
        return Enrollment(uid=row[0], secret=row[1], confirmed=bool(row[2]), last_step=row[3])

    def start_enrollment(self, uid: str, secret: str) -> None:
        uid = _norm(uid)
        with self._lock, self._conn() as c:
            c.execute(
                """
                INSERT INTO user_totp (uid, secret, confirmed, last_step,
                                       failed_attempts, locked_until, created_at)
                VALUES (?, ?, 0, NULL, 0, NULL, datetime('now'))
                ON CONFLICT(uid) DO UPDATE SET
                    secret = excluded.secret,
                    confirmed = 0,
                    last_step = NULL,
                    failed_attempts = 0,
                    locked_until = NULL,
                    created_at = datetime('now'),
                    confirmed_at = NULL
                """,
                (uid, secret),
            )

    def confirm(self, uid: str) -> None:
        uid = _norm(uid)
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE user_totp SET confirmed = 1, confirmed_at = datetime('now') WHERE uid = ?",
                (uid,),
            )

    def record_step(self, uid: str, step: int) -> None:
        uid = _norm(uid)
        with self._lock, self._conn() as c:
            c.execute("UPDATE user_totp SET last_step = ? WHERE uid = ?", (int(step), uid))

    def locked_until(self, uid: str) -> Optional[float]:
        uid = _norm(uid)
        with self._lock, self._conn() as c:
            row = c.execute("SELECT locked_until FROM user_totp WHERE uid = ?", (uid,)).fetchone()
        return row[0] if row else None

    def note_failure(self, uid: str, threshold: int, cooldown: float,
                     at: float) -> Optional[float]:
        uid = _norm(uid)
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT failed_attempts FROM user_totp WHERE uid = ?", (uid,)
            ).fetchone()
            if not row:
                return None
            attempts = (row[0] or 0) + 1
            if attempts >= threshold:
                lock = at + cooldown
                c.execute(
                    "UPDATE user_totp SET failed_attempts = 0, locked_until = ? WHERE uid = ?",
                    (lock, uid),
                )
                return lock
            c.execute(
                "UPDATE user_totp SET failed_attempts = ? WHERE uid = ?", (attempts, uid)
            )
            return None

    def reset_failures(self, uid: str) -> None:
        uid = _norm(uid)
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE user_totp SET failed_attempts = 0, locked_until = NULL WHERE uid = ?",
                (uid,),
            )

    def delete(self, uid: str) -> None:
        uid = _norm(uid)
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM user_totp WHERE uid = ?", (uid,))

    def list_enrolled(self) -> List[Enrollment]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT uid, secret, confirmed, last_step FROM user_totp ORDER BY uid"
            ).fetchall()
        return [Enrollment(uid=r[0], secret=r[1], confirmed=bool(r[2]), last_step=r[3]) for r in rows]


# Postgres backend: same table, in the schema the rest of the deployment uses.
SCHEMA_NAME = "hammer_poc"
FQ_TOTP = f"{SCHEMA_NAME}.user_totp"

_PG_DDL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA_NAME};
CREATE TABLE IF NOT EXISTS {FQ_TOTP} (
    uid             TEXT PRIMARY KEY,
    secret          TEXT NOT NULL,
    confirmed       BOOLEAN NOT NULL DEFAULT FALSE,
    last_step       BIGINT,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until    DOUBLE PRECISION,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_at    TIMESTAMPTZ
);
-- Tolerate a table created by an earlier build without the lockout columns.
ALTER TABLE {FQ_TOTP} ADD COLUMN IF NOT EXISTS failed_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE {FQ_TOTP} ADD COLUMN IF NOT EXISTS locked_until DOUBLE PRECISION;
"""


class PostgresTotpStore(TotpStore):
    """The real deployment's store: hammer_poc.user_totp in sledgehammer_studio.

    Connections come from pd_store._connect so the secret store reads the same
    Postgres settings (host, db, password) the cache and whitelist already use.
    """

    def __init__(self):
        from hammer.vlsi import pd_store  # imported lazily so the demo doesn't need it
        self._connect = pd_store._connect
        self._ensure_table()

    def _ensure_table(self) -> None:
        conn = self._connect()
        try:
            try:
                with conn.cursor() as cur:
                    cur.execute(_PG_DDL)
                conn.commit()
            except Exception:
                # Only the schema owner can run this DDL. A teammate's
                # webserver connects as their own role, so the CREATE/ALTER
                # statements fail for them even though the table is already
                # there and fully usable; that must not take down their login.
                # Fall through if the table answers a probe, else re-raise.
                conn.rollback()
                with conn.cursor() as cur:
                    cur.execute(f"SELECT 1 FROM {FQ_TOTP} LIMIT 1")
        finally:
            conn.close()

    def get(self, uid: str) -> Optional[Enrollment]:
        uid = _norm(uid)
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT uid, secret, confirmed, last_step FROM {FQ_TOTP} WHERE uid = %s",
                    (uid,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return Enrollment(uid=row[0], secret=row[1], confirmed=bool(row[2]), last_step=row[3])

    def start_enrollment(self, uid: str, secret: str) -> None:
        uid = _norm(uid)
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {FQ_TOTP} (uid, secret, confirmed, last_step,
                                           failed_attempts, locked_until, created_at, confirmed_at)
                    VALUES (%s, %s, FALSE, NULL, 0, NULL, NOW(), NULL)
                    ON CONFLICT (uid) DO UPDATE SET
                        secret = EXCLUDED.secret,
                        confirmed = FALSE,
                        last_step = NULL,
                        failed_attempts = 0,
                        locked_until = NULL,
                        created_at = NOW(),
                        confirmed_at = NULL
                    """,
                    (uid, secret),
                )
            conn.commit()
        finally:
            conn.close()

    def confirm(self, uid: str) -> None:
        uid = _norm(uid)
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {FQ_TOTP} SET confirmed = TRUE, confirmed_at = NOW() WHERE uid = %s",
                    (uid,),
                )
            conn.commit()
        finally:
            conn.close()

    def record_step(self, uid: str, step: int) -> None:
        uid = _norm(uid)
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {FQ_TOTP} SET last_step = %s WHERE uid = %s",
                    (int(step), uid),
                )
            conn.commit()
        finally:
            conn.close()

    def locked_until(self, uid: str) -> Optional[float]:
        uid = _norm(uid)
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT locked_until FROM {FQ_TOTP} WHERE uid = %s", (uid,))
                row = cur.fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    def note_failure(self, uid: str, threshold: int, cooldown: float,
                     at: float) -> Optional[float]:
        uid = _norm(uid)
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT failed_attempts FROM {FQ_TOTP} WHERE uid = %s FOR UPDATE",
                    (uid,),
                )
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return None
                attempts = (row[0] or 0) + 1
                if attempts >= threshold:
                    lock = at + cooldown
                    cur.execute(
                        f"UPDATE {FQ_TOTP} SET failed_attempts = 0, locked_until = %s WHERE uid = %s",
                        (lock, uid),
                    )
                    result = lock
                else:
                    cur.execute(
                        f"UPDATE {FQ_TOTP} SET failed_attempts = %s WHERE uid = %s",
                        (attempts, uid),
                    )
                    result = None
            conn.commit()
        finally:
            conn.close()
        return result

    def reset_failures(self, uid: str) -> None:
        uid = _norm(uid)
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {FQ_TOTP} SET failed_attempts = 0, locked_until = NULL WHERE uid = %s",
                    (uid,),
                )
            conn.commit()
        finally:
            conn.close()

    def delete(self, uid: str) -> None:
        uid = _norm(uid)
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {FQ_TOTP} WHERE uid = %s", (uid,))
            conn.commit()
        finally:
            conn.close()

    def list_enrolled(self) -> List[Enrollment]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT uid, secret, confirmed, last_step FROM {FQ_TOTP} ORDER BY uid"
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        return [Enrollment(uid=r[0], secret=r[1], confirmed=bool(r[2]), last_step=r[3]) for r in rows]


def get_store(backend: Optional[str] = None) -> TotpStore:
    """Pick a backend. Defaults to SQLite so nothing here touches Postgres unless
    asked; the Airflow integration passes backend='postgres' explicitly.
    """
    backend = (backend or os.environ.get("SLEDGE_2FA_STORE", "sqlite")).lower()
    if backend in ("pg", "postgres", "postgresql"):
        return PostgresTotpStore()
    return SqliteTotpStore()
