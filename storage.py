"""Paikallinen tallennus: kyselyt SQLite-tietokantaan, liitetiedostot levylle."""
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

DATA_DIR = Path(os.environ.get("JURISTI_DATA", Path(__file__).resolve().parent / "data"))
DB_PATH = DATA_DIR / "kyselyt.db"
FILES_DIR = DATA_DIR / "tiedostot"

SCHEMA = """
CREATE TABLE IF NOT EXISTS queries (
    id          TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    finished_at REAL,
    query       TEXT NOT NULL,
    status      TEXT NOT NULL,            -- running | done | error
    answer      TEXT,
    error       TEXT,
    progress    TEXT NOT NULL DEFAULT '[]',
    cost_usd    REAL
);
CREATE INDEX IF NOT EXISTS idx_queries_created ON queries(created_at);
CREATE TABLE IF NOT EXISTS files (
    query_id TEXT NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    size     INTEGER NOT NULL,
    PRIMARY KEY (query_id, name)
);
"""


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # SQLiten oma lower() ei käsittele ä/ö-kirjaimia, joten haku käyttää Pythonin versiota.
    conn.create_function("pylower", 1, lambda s: s.lower() if isinstance(s, str) else s)
    return conn


def init():
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        # Palvelimen kaatuessa kesken jääneitä ajoja ei voi jatkaa.
        conn.execute(
            "UPDATE queries SET status = 'error', error = ?, finished_at = ? WHERE status = 'running'",
            ("Käsittely keskeytyi, koska palvelin pysähtyi.", time.time()),
        )


WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_filename(name):
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f/:*?\"<>|]", "_", name).strip(" .")
    if name.split(".")[0].upper() in WINDOWS_RESERVED:
        name = "_" + name
    # Lyhyt nimi pitää polut Windowsin 260 merkin rajan alla; pääte säilytetään.
    stem, ext = os.path.splitext(name)
    if len(name) > 100:
        name = stem[: 100 - len(ext)] + ext
    return name or "tiedosto"


def query_dir(query_id):
    return FILES_DIR / query_id


def create_query(query, files):
    """files: lista (nimi, bytes). Palauttaa uuden kyselyn id:n."""
    query_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    folder = query_dir(query_id)
    folder.mkdir(parents=True)
    stored = []
    for name, data in files:
        name = safe_filename(name)
        stem, ext = os.path.splitext(name)
        n = 2
        while (folder / name).exists():
            name = f"{stem} ({n}){ext}"
            n += 1
        (folder / name).write_bytes(data)
        stored.append((query_id, name, len(data)))
    with _connect() as conn:
        conn.execute(
            "INSERT INTO queries (id, created_at, query, status) VALUES (?, ?, ?, 'running')",
            (query_id, time.time(), query),
        )
        conn.executemany("INSERT INTO files (query_id, name, size) VALUES (?, ?, ?)", stored)
    return query_id


def _files_for(conn, query_id):
    rows = conn.execute(
        "SELECT name, size FROM files WHERE query_id = ? ORDER BY name", (query_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def list_queries(search="", order="desc"):
    direction = "ASC" if order == "asc" else "DESC"
    sql = "SELECT id, created_at, finished_at, query, status FROM queries q"
    params = []
    if search.strip():
        pattern = "%" + re.sub(r"([\\%_])", r"\\\1", search.strip().lower()) + "%"
        sql += """ WHERE pylower(q.query) LIKE ? ESCAPE '\\'
                   OR pylower(q.answer) LIKE ? ESCAPE '\\'
                   OR EXISTS (SELECT 1 FROM files f WHERE f.query_id = q.id
                              AND pylower(f.name) LIKE ? ESCAPE '\\')"""
        params = [pattern] * 3
    sql += f" ORDER BY q.created_at {direction}"
    with _connect() as conn:
        result = []
        for row in conn.execute(sql, params).fetchall():
            item = dict(row)
            item["files"] = [f["name"] for f in _files_for(conn, row["id"])]
            result.append(item)
        return result


def get_query(query_id):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM queries WHERE id = ?", (query_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["progress"] = json.loads(item["progress"])
        item["files"] = _files_for(conn, query_id)
        return item


def append_progress(query_id, text):
    with _connect() as conn:
        row = conn.execute("SELECT progress FROM queries WHERE id = ?", (query_id,)).fetchone()
        if row:
            steps = json.loads(row["progress"])
            steps.append({"t": time.time(), "text": text})
            conn.execute(
                "UPDATE queries SET progress = ? WHERE id = ?",
                (json.dumps(steps, ensure_ascii=False), query_id),
            )


def finish_query(query_id, answer=None, error=None, cost_usd=None):
    with _connect() as conn:
        conn.execute(
            "UPDATE queries SET status = ?, answer = ?, error = ?, cost_usd = ?, finished_at = ? WHERE id = ?",
            ("error" if error else "done", answer, error, cost_usd, time.time(), query_id),
        )


def delete_query(query_id):
    with _connect() as conn:
        deleted = conn.execute("DELETE FROM queries WHERE id = ?", (query_id,)).rowcount
    shutil.rmtree(query_dir(query_id), ignore_errors=True)
    return deleted > 0


def file_path(query_id, name):
    """Palauttaa ladatun tiedoston polun vain, jos se on kyselyn tallennettu liite."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM files WHERE query_id = ? AND name = ?", (query_id, name)
        ).fetchone()
    return query_dir(query_id) / name if row else None
