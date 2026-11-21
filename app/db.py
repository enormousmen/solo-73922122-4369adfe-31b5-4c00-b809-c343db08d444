"""SQLite 持久层：盘点、标签、对象、引用边、预演结果。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .oci import Graph, ParsedInventory, RehearsalView

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT,
    imported_at TEXT NOT NULL,
    complete INTEGER NOT NULL,
    missing_json TEXT NOT NULL DEFAULT '[]',
    repo_count INTEGER NOT NULL,
    tag_count INTEGER NOT NULL,
    object_count INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS inv_tags (
    inventory_id INTEGER NOT NULL,
    repo TEXT NOT NULL,
    tag TEXT NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY (inventory_id, repo, tag)
);
CREATE TABLE IF NOT EXISTS inv_objects (
    inventory_id INTEGER NOT NULL,
    digest TEXT NOT NULL,
    otype TEXT NOT NULL,
    size INTEGER NOT NULL,
    PRIMARY KEY (inventory_id, digest)
);
CREATE TABLE IF NOT EXISTS inv_edges (
    inventory_id INTEGER NOT NULL,
    src TEXT NOT NULL,
    dst TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON inv_edges (inventory_id, src);
CREATE TABLE IF NOT EXISTS inv_retained (
    inventory_id INTEGER NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY (inventory_id, digest)
);
CREATE TABLE IF NOT EXISTS rehearsals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inventory_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    total_bytes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rehearsal_tags (
    rehearsal_id INTEGER NOT NULL,
    repo TEXT NOT NULL,
    tag TEXT NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY (rehearsal_id, repo, tag)
);
CREATE TABLE IF NOT EXISTS rehearsal_deletes (
    rehearsal_id INTEGER NOT NULL,
    digest TEXT NOT NULL,
    otype TEXT NOT NULL,
    size INTEGER NOT NULL,
    PRIMARY KEY (rehearsal_id, digest)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    # 每个请求独立连接；FastAPI 依赖与路由可能运行在不同线程，关闭线程检查
    con = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    # WAL：复核（读）期间值班员仍可导入盘点（写），互不阻塞
    if path != ":memory:":
        con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 5000")
    return con


def init_db(path: str) -> None:
    with connect(path) as con:
        con.executescript(SCHEMA)


def insert_inventory(con: sqlite3.Connection, parsed: ParsedInventory) -> int:
    """把解析好的盘点整体写入（单事务），返回 inventory id。"""
    import json as _json

    tag_count = sum(len(tm) for tm in parsed.tags.values())
    total_bytes = sum(m["size"] for m in parsed.objects.values())
    cur = con.execute(
        """INSERT INTO inventories
           (captured_at, imported_at, complete, missing_json,
            repo_count, tag_count, object_count, total_bytes)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            parsed.captured_at,
            utcnow(),
            1 if not parsed.missing else 0,
            _json.dumps(parsed.missing, ensure_ascii=False),
            len(parsed.tags),
            tag_count,
            len(parsed.objects),
            total_bytes,
        ),
    )
    inv_id = cur.lastrowid
    con.executemany(
        "INSERT INTO inv_tags (inventory_id, repo, tag, digest) VALUES (?,?,?,?)",
        [(inv_id, repo, tag, digest) for repo, tm in parsed.tags.items() for tag, digest in tm.items()],
    )
    con.executemany(
        "INSERT INTO inv_objects (inventory_id, digest, otype, size) VALUES (?,?,?,?)",
        [(inv_id, d, m["type"], m["size"]) for d, m in parsed.objects.items()],
    )
    con.executemany(
        "INSERT INTO inv_edges (inventory_id, src, dst) VALUES (?,?,?)",
        [(inv_id, src, dst) for src, dsts in parsed.edges.items() for dst in dsts],
    )
    con.executemany(
        "INSERT INTO inv_retained (inventory_id, digest) VALUES (?,?)",
        [(inv_id, d) for d in sorted(parsed.retained)],
    )
    return inv_id


def current_inventory(con: sqlite3.Connection) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM inventories ORDER BY id DESC LIMIT 1").fetchone()


def latest_inventory_id(con: sqlite3.Connection) -> int | None:
    """当前最新且已成功导入（整事务提交后才可见）的盘点号。"""
    row = con.execute("SELECT MAX(id) AS mid FROM inventories").fetchone()
    return None if row is None or row["mid"] is None else int(row["mid"])


def list_inventories(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM inventories ORDER BY id DESC").fetchall()


def load_graph(con: sqlite3.Connection, inv_id: int) -> Graph:
    objects = {
        r["digest"]: {"type": r["otype"], "size": r["size"]}
        for r in con.execute("SELECT digest, otype, size FROM inv_objects WHERE inventory_id=?", (inv_id,))
    }
    edges: dict[str, list[str]] = {}
    for r in con.execute("SELECT src, dst FROM inv_edges WHERE inventory_id=?", (inv_id,)):
        edges.setdefault(r["src"], []).append(r["dst"])
    tags = {
        (r["repo"], r["tag"]): r["digest"]
        for r in con.execute("SELECT repo, tag, digest FROM inv_tags WHERE inventory_id=?", (inv_id,))
    }
    retained = {
        r["digest"]
        for r in con.execute("SELECT digest FROM inv_retained WHERE inventory_id=?", (inv_id,))
    }
    return Graph(objects=objects, edges=edges, tags=tags, retained=retained)


def tags_grouped(con: sqlite3.Connection, inv_id: int) -> dict[str, list[tuple[str, str]]]:
    """repo -> [(tag, digest)]，按仓库名、标签名排序。"""
    rows = con.execute(
        "SELECT repo, tag, digest FROM inv_tags WHERE inventory_id=? ORDER BY repo, tag",
        (inv_id,),
    ).fetchall()
    grouped: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        grouped.setdefault(r["repo"], []).append((r["tag"], r["digest"]))
    return grouped


def create_rehearsal(
    con: sqlite3.Connection,
    inv_id: int,
    selected_tags: list[dict],
    deletes: list[dict],
    total_bytes: int,
) -> int:
    cur = con.execute(
        "INSERT INTO rehearsals (inventory_id, created_at, total_bytes) VALUES (?,?,?)",
        (inv_id, utcnow(), total_bytes),
    )
    rid = cur.lastrowid
    con.executemany(
        "INSERT INTO rehearsal_tags (rehearsal_id, repo, tag, digest) VALUES (?,?,?,?)",
        [(rid, t["repo"], t["tag"], t["digest"]) for t in selected_tags],
    )
    con.executemany(
        "INSERT INTO rehearsal_deletes (rehearsal_id, digest, otype, size) VALUES (?,?,?,?)",
        [(rid, d["digest"], d["type"], d["size"]) for d in deletes],
    )
    return rid


def get_rehearsal(con: sqlite3.Connection, rid: int) -> RehearsalView | None:
    head = con.execute("SELECT * FROM rehearsals WHERE id=?", (rid,)).fetchone()
    if head is None:
        return None
    tags = [
        {"repo": r["repo"], "tag": r["tag"], "digest": r["digest"]}
        for r in con.execute(
            "SELECT repo, tag, digest FROM rehearsal_tags WHERE rehearsal_id=? ORDER BY repo, tag",
            (rid,),
        )
    ]
    deletes = [
        {"digest": r["digest"], "type": r["otype"], "size": r["size"]}
        for r in con.execute(
            "SELECT digest, otype, size FROM rehearsal_deletes WHERE rehearsal_id=? ORDER BY digest",
            (rid,),
        )
    ]
    return RehearsalView(
        rehearsal_id=head["id"],
        inventory_id=head["inventory_id"],
        tags=tags,
        deletes=deletes,
    )


def get_rehearsal_head(con: sqlite3.Connection, rid: int) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM rehearsals WHERE id=?", (rid,)).fetchone()


def list_rehearsals(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        """SELECT r.*,
                  (SELECT COUNT(*) FROM rehearsal_tags t WHERE t.rehearsal_id = r.id) AS tag_count,
                  (SELECT COUNT(*) FROM rehearsal_deletes d WHERE d.rehearsal_id = r.id) AS delete_count
           FROM rehearsals r ORDER BY r.id DESC"""
    ).fetchall()
