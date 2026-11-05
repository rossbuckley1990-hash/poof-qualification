"""POOF Notes — a tiny notes app with a real SQLite backend.

Standard library only. Serves a single-page UI and a JSON API.
Data lives in $POOF_NOTES_DATA (default /data) so it can sit on a persistent volume.
"""
import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("PORT", "8080"))
DATA_DIR = Path(os.environ.get("POOF_NOTES_DATA", "/data"))
DB_PATH = DATA_DIR / "notes.sqlite3"
STATIC = Path(__file__).with_name("index.html")
MAX_BODY = 256 * 1024

_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )"""
        )


def row(r):
    return {k: r[k] for k in r.keys()}


class Handler(BaseHTTPRequestHandler):
    server_version = "POOFNotes/1.0"

    def log_message(self, fmt, *args):
        print("%s %s" % (self.address_string(), fmt % args), flush=True)

    # --- helpers -----------------------------------------------------------
    def send_json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("body too large")
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("expected an object")
        return payload

    def note_id(self):
        parts = self.path.split("?")[0].strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "notes"] and parts[2].isdigit():
            return int(parts[2])
        return None

    # --- routes ------------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            with db() as conn:
                conn.execute("SELECT 1")
            return self.send_json(200, {"ok": True})
        if path == "/api/notes":
            with db() as conn:
                rows = conn.execute(
                    "SELECT * FROM notes ORDER BY updated_at DESC, id DESC"
                ).fetchall()
            return self.send_json(200, [row(r) for r in rows])
        if path in ("/", "/index.html"):
            data = STATIC.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] != "/api/notes":
            return self.send_json(404, {"error": "not found"})
        try:
            p = self.read_json()
        except (ValueError, json.JSONDecodeError) as e:
            return self.send_json(400, {"error": str(e)})
        now = time.time()
        with _lock, db() as conn:
            cur = conn.execute(
                "INSERT INTO notes (title, body, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (str(p.get("title", ""))[:500], str(p.get("body", ""))[:100000], now, now),
            )
            r = conn.execute("SELECT * FROM notes WHERE id = ?", (cur.lastrowid,)).fetchone()
        self.send_json(201, row(r))

    def do_PUT(self):
        nid = self.note_id()
        if nid is None:
            return self.send_json(404, {"error": "not found"})
        try:
            p = self.read_json()
        except (ValueError, json.JSONDecodeError) as e:
            return self.send_json(400, {"error": str(e)})
        with _lock, db() as conn:
            cur = conn.execute(
                "UPDATE notes SET title = ?, body = ?, updated_at = ? WHERE id = ?",
                (str(p.get("title", ""))[:500], str(p.get("body", ""))[:100000], time.time(), nid),
            )
            if cur.rowcount == 0:
                return self.send_json(404, {"error": "not found"})
            r = conn.execute("SELECT * FROM notes WHERE id = ?", (nid,)).fetchone()
        self.send_json(200, row(r))

    def do_DELETE(self):
        nid = self.note_id()
        if nid is None:
            return self.send_json(404, {"error": "not found"})
        with _lock, db() as conn:
            cur = conn.execute("DELETE FROM notes WHERE id = ?", (nid,))
        if cur.rowcount == 0:
            return self.send_json(404, {"error": "not found"})
        self.send_json(200, {"deleted": nid})


if __name__ == "__main__":
    init_db()
    print(f"POOF Notes listening on :{PORT}, data in {DB_PATH}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
