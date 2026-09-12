"""
Сбор и агрегация статистики канала и чата обсуждения.

Хранилище — SQLite (файл stats.db рядом с bot.py, без вложенных папок).
Модуль не зависит от aiogram: принимает простые типы, отдаёт готовые словари.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent / Path(os.getenv("STATS_DB", "stats.db")).name

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


# ──────────────────────────────────────────────────────────────
#  ПОДКЛЮЧЕНИЕ И СХЕМА
# ──────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
    return _conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    kind      TEXT    NOT NULL,
    user_id   INTEGER,
    name      TEXT,
    message_id INTEGER,
    thread_id INTEGER,
    value     INTEGER DEFAULT 0,
    meta      TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, ts);

CREATE TABLE IF NOT EXISTS posts (
    message_id INTEGER PRIMARY KEY,
    ts         INTEGER NOT NULL,
    kind       TEXT DEFAULT 'text',
    text_len   INTEGER DEFAULT 0,
    preview    TEXT DEFAULT '',
    thread_id  INTEGER,
    comments   INTEGER DEFAULT 0,
    reactions  INTEGER DEFAULT 0,
    commenters TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_posts_ts ON posts(ts);
CREATE INDEX IF NOT EXISTS idx_posts_thread ON posts(thread_id);

CREATE TABLE IF NOT EXISTS members (
    user_id    INTEGER PRIMARY KEY,
    first_name TEXT DEFAULT '',
    last_name  TEXT DEFAULT '',
    username   TEXT,
    joined_ts  INTEGER,
    left_ts    INTEGER,
    last_seen  INTEGER,
    messages   INTEGER DEFAULT 0,
    is_member  INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS snapshots (
    day          TEXT PRIMARY KEY,
    subscribers  INTEGER,
    chat_members INTEGER,
    ts           INTEGER
);
"""


def init() -> None:
    with _lock:
        conn = _connect()
        conn.executescript(SCHEMA)
        conn.commit()


def _now() -> int:
    return int(time.time())


# ──────────────────────────────────────────────────────────────
#  ЗАПИСЬ СОБЫТИЙ
# ──────────────────────────────────────────────────────────────

def record_event(
    kind: str,
    *,
    user_id: int | None = None,
    name: str | None = None,
    message_id: int | None = None,
    thread_id: int | None = None,
    value: int = 0,
    meta: dict[str, Any] | None = None,
    ts: int | None = None,
) -> None:
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO events (ts, kind, user_id, name, message_id, thread_id, value, meta) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                ts or _now(),
                kind,
                user_id,
                name,
                message_id,
                thread_id,
                value,
                json.dumps(meta, ensure_ascii=False) if meta else None,
            ),
        )
        conn.commit()


def record_post(
    message_id: int,
    *,
    ts: int | None = None,
    kind: str = "text",
    text_len: int = 0,
    preview: str = "",
) -> None:
    """Новая публикация в канале."""
    stamp = ts or _now()
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT OR IGNORE INTO posts (message_id, ts, kind, text_len, preview) VALUES (?,?,?,?,?)",
            (message_id, stamp, kind, text_len, preview[:160]),
        )
        conn.commit()
    record_event("post", message_id=message_id, ts=stamp, meta={"kind": kind})


def link_thread(post_id: int | None, thread_id: int) -> None:
    """Связывает пост канала с веткой обсуждения (копией в группе)."""
    if not post_id:
        return
    with _lock:
        conn = _connect()
        conn.execute("UPDATE posts SET thread_id=? WHERE message_id=?", (thread_id, post_id))
        conn.commit()


def record_comment(
    user_id: int,
    name: str,
    *,
    thread_id: int | None = None,
    message_id: int | None = None,
    text_len: int = 0,
) -> None:
    """Комментарий пользователя в чате обсуждения."""
    ts = _now()
    with _lock:
        conn = _connect()
        if thread_id:
            row = conn.execute(
                "SELECT message_id, commenters FROM posts WHERE thread_id=?", (thread_id,)
            ).fetchone()
            if row:
                try:
                    people = set(json.loads(row["commenters"] or "[]"))
                except (ValueError, TypeError):
                    people = set()
                people.add(user_id)
                conn.execute(
                    "UPDATE posts SET comments=comments+1, commenters=? WHERE message_id=?",
                    (json.dumps(sorted(people)), row["message_id"]),
                )
        conn.execute(
            "UPDATE members SET messages=messages+1, last_seen=? WHERE user_id=?", (ts, user_id)
        )
        conn.commit()
    record_event(
        "comment",
        user_id=user_id,
        name=name,
        message_id=message_id,
        thread_id=thread_id,
        value=text_len,
        ts=ts,
    )


def upsert_member(
    user_id: int,
    first_name: str = "",
    last_name: str = "",
    username: str | None = None,
    *,
    joined: bool = False,
) -> None:
    ts = _now()
    with _lock:
        conn = _connect()
        exists = conn.execute("SELECT user_id FROM members WHERE user_id=?", (user_id,)).fetchone()
        if exists:
            conn.execute(
                "UPDATE members SET first_name=?, last_name=?, username=?, last_seen=?, "
                "is_member=1, left_ts=NULL WHERE user_id=?",
                (first_name, last_name, username, ts, user_id),
            )
            if joined:
                conn.execute("UPDATE members SET joined_ts=? WHERE user_id=?", (ts, user_id))
        else:
            conn.execute(
                "INSERT INTO members (user_id, first_name, last_name, username, joined_ts, "
                "last_seen, is_member) VALUES (?,?,?,?,?,?,1)",
                (user_id, first_name, last_name, username, ts if joined else None, ts),
            )
        conn.commit()


def record_join(user_id: int, name: str, username: str | None = None) -> bool:
    """Возвращает False, если такой вход уже зафиксирован минуту назад (антидубль)."""
    ts = _now()
    with _lock:
        conn = _connect()
        dup = conn.execute(
            "SELECT id FROM events WHERE kind='join' AND user_id=? AND ts > ?",
            (user_id, ts - 60),
        ).fetchone()
        if dup:
            return False
    record_event("join", user_id=user_id, name=name, ts=ts, meta={"username": username})
    return True


def record_leave(user_id: int, name: str) -> None:
    ts = _now()
    with _lock:
        conn = _connect()
        conn.execute("UPDATE members SET is_member=0, left_ts=? WHERE user_id=?", (ts, user_id))
        conn.commit()
    record_event("leave", user_id=user_id, name=name, ts=ts)


def record_reactions(message_id: int, total: int) -> None:
    with _lock:
        conn = _connect()
        conn.execute("UPDATE posts SET reactions=? WHERE message_id=?", (total, message_id))
        conn.commit()
    record_event("reaction", message_id=message_id, value=total)


def save_snapshot(subscribers: int | None, chat_members: int | None) -> None:
    day = time.strftime("%Y-%m-%d", time.localtime())
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO snapshots (day, subscribers, chat_members, ts) VALUES (?,?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET subscribers=excluded.subscribers, "
            "chat_members=excluded.chat_members, ts=excluded.ts",
            (day, subscribers, chat_members, _now()),
        )
        conn.commit()


# ──────────────────────────────────────────────────────────────
#  ЧТЕНИЕ И АГРЕГАЦИЯ
# ──────────────────────────────────────────────────────────────

def _count(conn: sqlite3.Connection, kind: str, since: int = 0) -> int:
    row = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE kind=? AND ts>=?", (kind, since)
    ).fetchone()
    return int(row["c"] or 0)


def build_stats(days: int = 30, channel_short_id: str = "") -> dict[str, Any]:
    """Полный набор показателей для дашборда."""
    now = _now()
    since = now - days * 86400
    week = now - 7 * 86400
    prev_week = now - 14 * 86400

    with _lock:
        conn = _connect()

        totals = {
            "posts": _count(conn, "post"),
            "comments": _count(conn, "comment"),
            "joins": _count(conn, "join"),
            "leaves": _count(conn, "leave"),
            "greetings": _count(conn, "greeting"),
            "rules": _count(conn, "rules"),
            "starts": _count(conn, "start"),
        }
        period = {
            "posts": _count(conn, "post", since),
            "comments": _count(conn, "comment", since),
            "joins": _count(conn, "join", since),
            "leaves": _count(conn, "leave", since),
        }
        last7 = {k: _count(conn, k, week) for k in ("post", "comment", "join", "leave")}
        prev7 = {
            k: conn.execute(
                "SELECT COUNT(*) c FROM events WHERE kind=? AND ts>=? AND ts<?",
                (k, prev_week, week),
            ).fetchone()["c"]
            for k in ("post", "comment", "join", "leave")
        }

        members_row = conn.execute(
            "SELECT COUNT(*) total, SUM(is_member) active FROM members"
        ).fetchone()
        reactions_row = conn.execute("SELECT COALESCE(SUM(reactions),0) r FROM posts").fetchone()

        # Динамика по дням
        series_map: dict[str, dict[str, int]] = {}
        rows = conn.execute(
            "SELECT date(ts,'unixepoch','localtime') d, kind, COUNT(*) c "
            "FROM events WHERE ts>=? AND kind IN ('post','comment','join','leave') "
            "GROUP BY d, kind",
            (since,),
        ).fetchall()
        for row in rows:
            series_map.setdefault(row["d"], {})[row["kind"]] = row["c"]

        # Активность по часам
        hourly = [0] * 24
        for row in conn.execute(
            "SELECT CAST(strftime('%H', ts,'unixepoch','localtime') AS INT) h, COUNT(*) c "
            "FROM events WHERE kind='comment' AND ts>=? GROUP BY h",
            (since,),
        ):
            hourly[int(row["h"])] = row["c"]

        # Тепловая карта: день недели × час
        heat = [[0] * 24 for _ in range(7)]
        for row in conn.execute(
            "SELECT CAST(strftime('%w', ts,'unixepoch','localtime') AS INT) w, "
            "CAST(strftime('%H', ts,'unixepoch','localtime') AS INT) h, COUNT(*) c "
            "FROM events WHERE kind='comment' AND ts>=? GROUP BY w,h",
            (since,),
        ):
            heat[int(row["w"])][int(row["h"])] = row["c"]

        post_types = [
            {"kind": r["kind"] or "text", "count": r["c"]}
            for r in conn.execute(
                "SELECT kind, COUNT(*) c FROM posts GROUP BY kind ORDER BY c DESC"
            )
        ]

        top_posts = [
            {
                "message_id": r["message_id"],
                "ts": r["ts"],
                "kind": r["kind"],
                "preview": r["preview"],
                "comments": r["comments"],
                "reactions": r["reactions"],
                "people": len(json.loads(r["commenters"] or "[]")),
                "url": f"https://t.me/c/{channel_short_id}/{r['message_id']}"
                if channel_short_id
                else None,
            }
            for r in conn.execute(
                "SELECT * FROM posts ORDER BY comments DESC, ts DESC LIMIT 8"
            )
        ]

        recent_posts = [
            {
                "message_id": r["message_id"],
                "ts": r["ts"],
                "kind": r["kind"],
                "preview": r["preview"],
                "comments": r["comments"],
                "reactions": r["reactions"],
            }
            for r in conn.execute("SELECT * FROM posts ORDER BY ts DESC LIMIT 8")
        ]

        top_members = [
            {
                "user_id": r["user_id"],
                "name": (f"{r['first_name']} {r['last_name']}").strip() or "Без имени",
                "username": r["username"],
                "messages": r["messages"],
                "joined_ts": r["joined_ts"],
                "is_member": bool(r["is_member"]),
            }
            for r in conn.execute(
                "SELECT * FROM members ORDER BY messages DESC, last_seen DESC LIMIT 10"
            )
        ]

        recent = [
            {
                "ts": r["ts"],
                "kind": r["kind"],
                "name": r["name"],
                "value": r["value"],
                "message_id": r["message_id"],
            }
            for r in conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 25")
        ]

        snaps = [
            {"day": r["day"], "subscribers": r["subscribers"], "chat_members": r["chat_members"]}
            for r in conn.execute(
                "SELECT * FROM snapshots ORDER BY day DESC LIMIT ?", (days,)
            )
        ][::-1]

        first_row = conn.execute("SELECT MIN(ts) t FROM events").fetchone()

    # Сплошной ряд дат без пропусков
    timeseries = []
    for offset in range(days - 1, -1, -1):
        day = time.strftime("%Y-%m-%d", time.localtime(now - offset * 86400))
        bucket = series_map.get(day, {})
        timeseries.append(
            {
                "day": day,
                "posts": bucket.get("post", 0),
                "comments": bucket.get("comment", 0),
                "joins": bucket.get("join", 0),
                "leaves": bucket.get("leave", 0),
            }
        )

    def trend(now_value: int, prev_value: int) -> float:
        if not prev_value:
            return 100.0 if now_value else 0.0
        return round((now_value - prev_value) / prev_value * 100, 1)

    posts_total = totals["posts"] or 0
    comments_total = totals["comments"] or 0

    return {
        "generated_at": now,
        "period_days": days,
        "totals": {
            **totals,
            "reactions": int(reactions_row["r"] or 0),
            "members_tracked": int(members_row["total"] or 0),
            "members_active": int(members_row["active"] or 0),
            "avg_comments": round(comments_total / posts_total, 2) if posts_total else 0,
            "retention": round(
                (totals["joins"] - totals["leaves"]) / totals["joins"] * 100, 1
            )
            if totals["joins"]
            else 0,
        },
        "period": period,
        "trends": {
            "posts": trend(last7["post"], prev7["post"]),
            "comments": trend(last7["comment"], prev7["comment"]),
            "joins": trend(last7["join"], prev7["join"]),
            "leaves": trend(last7["leave"], prev7["leave"]),
        },
        "last7": last7,
        "timeseries": timeseries,
        "hourly": hourly,
        "heatmap": heat,
        "post_types": post_types,
        "top_posts": top_posts,
        "recent_posts": recent_posts,
        "top_members": top_members,
        "recent": recent,
        "snapshots": snaps,
        "tracking_since": first_row["t"] if first_row and first_row["t"] else now,
    }


async def build_stats_async(days: int = 30, channel_short_id: str = "") -> dict[str, Any]:
    return await asyncio.to_thread(build_stats, days, channel_short_id)
