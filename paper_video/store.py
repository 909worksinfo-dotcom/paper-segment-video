"""Small durable store. Each operation owns its SQLite connection."""

import json
import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(
    os.environ.get(
        "PAPER_VIDEO_DATA", Path.home() / ".local/share/paper-segment-video/data"
    )
)


def connect():
    ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ROOT / "studio.sqlite", timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    with connect() as db:
        db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS quotes(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
          state TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
        """)


def document(doc_id):
    with connect() as db:
        row = db.execute(
            "SELECT payload FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
    if not row:
        raise KeyError("论文不存在")
    return json.loads(row["payload"])


def documents():
    with connect() as db:
        return [
            json.loads(r[0])
            for r in db.execute("SELECT payload FROM documents ORDER BY rowid DESC")
        ]


def save_document(doc):
    with connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO documents VALUES(?,?)",
            (doc["id"], json.dumps(doc, ensure_ascii=False)),
        )


def job(job_id):
    with connect() as db:
        row = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise KeyError("视频任务不存在")
    return json.loads(row[0])


def save_job(value):
    with connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?)",
            (
                value["id"],
                value["document_id"],
                value["state"],
                json.dumps(value, ensure_ascii=False),
                value["created"],
            ),
        )


def update_job(job_id, **changes):
    # A write transaction prevents lost updates when cancellation races a worker
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        value = json.loads(row[0])
        value.update(changes, updated=time.time())
        db.execute(
            "UPDATE jobs SET state=?,payload=? WHERE id=?",
            (value["state"], json.dumps(value, ensure_ascii=False), job_id),
        )
    return value


def jobs():
    with connect() as db:
        return [
            json.loads(r[0])
            for r in db.execute(
                "SELECT payload FROM jobs ORDER BY created DESC LIMIT 100"
            )
        ]


def save_quote(value):
    with connect() as db:
        db.execute("INSERT INTO quotes VALUES(?,?)", (value["id"], json.dumps(value, ensure_ascii=False)))


def quote(quote_id):
    with connect() as db:
        row = db.execute("SELECT payload FROM quotes WHERE id=?", (quote_id,)).fetchone()
    if not row:
        raise KeyError("论文引用不存在")
    return json.loads(row[0])
