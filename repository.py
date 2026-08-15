from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from .models import (
    DailySchedule,
    MailboxMessage,
    OfflineEvent,
    OfflineEventType,
    PresenceState,
    QueueState,
    ReturnReply,
    RuntimePresence,
)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Repository:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS bots (
                bot_id TEXT PRIMARY KEY, platform TEXT NOT NULL, last_seen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                umo TEXT PRIMARY KEY, bot_id TEXT NOT NULL, message_kind TEXT NOT NULL,
                group_id TEXT NOT NULL DEFAULT '', last_seen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daily_schedule (
                bot_id TEXT NOT NULL, schedule_date TEXT NOT NULL, timezone TEXT NOT NULL,
                wake_at TEXT NOT NULL, sleep_at TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(bot_id, schedule_date)
            );
            CREATE TABLE IF NOT EXISTS offline_event (
                id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, schedule_date TEXT NOT NULL,
                event_type TEXT NOT NULL, reason_id TEXT NOT NULL, pre_away_fact TEXT NOT NULL,
                planned_start_at TEXT NOT NULL, planned_end_at TEXT NOT NULL,
                fixed_monitor_text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PLANNED'
            );
            CREATE INDEX IF NOT EXISTS idx_offline_event_time
                ON offline_event(bot_id, planned_start_at, planned_end_at);
            CREATE TABLE IF NOT EXISTS runtime_state (
                bot_id TEXT PRIMARY KEY, state TEXT NOT NULL, current_event_id TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pre_away_notice (
                offline_event_id TEXT NOT NULL, umo TEXT NOT NULL, state TEXT NOT NULL,
                due_at TEXT NOT NULL, generated_text TEXT NOT NULL DEFAULT '', sent_at TEXT,
                PRIMARY KEY(offline_event_id, umo)
            );
            CREATE TABLE IF NOT EXISTS mailbox_message (
                id INTEGER PRIMARY KEY AUTOINCREMENT, offline_event_id TEXT NOT NULL,
                umo TEXT NOT NULL, message_kind TEXT NOT NULL, group_id TEXT NOT NULL DEFAULT '',
                sender_id TEXT NOT NULL, sender_name TEXT NOT NULL, message_id TEXT NOT NULL,
                timestamp TEXT NOT NULL, plain_text TEXT NOT NULL, components_json TEXT NOT NULL,
                selection_state TEXT NOT NULL DEFAULT 'PENDING',
                UNIQUE(umo, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_mailbox_event_state
                ON mailbox_message(offline_event_id, selection_state, umo);
            CREATE TABLE IF NOT EXISTS return_reply (
                id INTEGER PRIMARY KEY AUTOINCREMENT, offline_event_id TEXT NOT NULL,
                umo TEXT NOT NULL, message_kind TEXT NOT NULL, sender_id TEXT NOT NULL,
                source_message_ids TEXT NOT NULL, quote_message_id TEXT NOT NULL DEFAULT '',
                generated_text TEXT NOT NULL, send_state TEXT NOT NULL,
                scheduled_send_at TEXT NOT NULL, sent_at TEXT, error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_return_due
                ON return_reply(send_state, scheduled_send_at);
            CREATE TABLE IF NOT EXISTS group_context (
                id INTEGER PRIMARY KEY AUTOINCREMENT, umo TEXT NOT NULL, sender_id TEXT NOT NULL,
                sender_name TEXT NOT NULL, message_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                plain_text TEXT NOT NULL, UNIQUE(umo, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_group_context_umo ON group_context(umo, id);
            """
        )
        self._conn.commit()

    async def close(self) -> None:
        async with self._lock:
            self._conn.commit()
            self._conn.close()

    async def register_bot(self, bot_id: str, platform: str, now: datetime) -> None:
        async with self._lock:
            self._conn.execute(
                "INSERT INTO bots(bot_id, platform, last_seen_at) VALUES(?,?,?) "
                "ON CONFLICT(bot_id) DO UPDATE SET platform=excluded.platform,last_seen_at=excluded.last_seen_at",
                (bot_id, platform, _iso(now)),
            )
            self._conn.commit()

    async def bot_ids(self) -> list[str]:
        async with self._lock:
            return [row[0] for row in self._conn.execute("SELECT bot_id FROM bots")]

    async def touch_session(
        self, umo: str, bot_id: str, message_kind: str, group_id: str, now: datetime
    ) -> None:
        async with self._lock:
            self._conn.execute(
                "INSERT INTO sessions(umo,bot_id,message_kind,group_id,last_seen_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(umo) DO UPDATE SET bot_id=excluded.bot_id,message_kind=excluded.message_kind,"
                "group_id=excluded.group_id,last_seen_at=excluded.last_seen_at",
                (umo, bot_id, message_kind, group_id, _iso(now)),
            )
            self._conn.commit()

    async def active_private_sessions(
        self, bot_id: str, since: datetime
    ) -> list[str]:
        async with self._lock:
            rows = self._conn.execute(
                "SELECT umo FROM sessions WHERE bot_id=? AND message_kind='private' AND last_seen_at>=?",
                (bot_id, _iso(since)),
            ).fetchall()
            return [row[0] for row in rows]

    async def save_schedule(self, schedule: DailySchedule, created_at: datetime) -> bool:
        async with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO daily_schedule(bot_id,schedule_date,timezone,wake_at,sleep_at,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    schedule.bot_id,
                    schedule.schedule_date,
                    schedule.timezone,
                    _iso(schedule.wake_at),
                    _iso(schedule.sleep_at),
                    _iso(created_at),
                ),
            )
            if cursor.rowcount:
                self._insert_events(schedule.events)
            self._conn.commit()
            return bool(cursor.rowcount)

    def _insert_events(self, events: Iterable[OfflineEvent]) -> None:
        self._conn.executemany(
            "INSERT OR IGNORE INTO offline_event(id,bot_id,schedule_date,event_type,reason_id,"
            "pre_away_fact,planned_start_at,planned_end_at,fixed_monitor_text,state) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    event.id,
                    event.bot_id,
                    event.schedule_date,
                    event.event_type.value,
                    event.reason_id,
                    event.pre_away_fact,
                    _iso(event.start_at),
                    _iso(event.end_at),
                    event.fixed_monitor_text,
                    event.state,
                )
                for event in events
            ],
        )

    async def add_event(self, event: OfflineEvent) -> None:
        async with self._lock:
            self._insert_events([event])
            self._conn.commit()

    async def get_schedule(self, bot_id: str, schedule_date: str) -> DailySchedule | None:
        async with self._lock:
            row = self._conn.execute(
                "SELECT * FROM daily_schedule WHERE bot_id=? AND schedule_date=?",
                (bot_id, schedule_date),
            ).fetchone()
            if not row:
                return None
            events = self._conn.execute(
                "SELECT * FROM offline_event WHERE bot_id=? AND schedule_date=? ORDER BY planned_start_at",
                (bot_id, schedule_date),
            ).fetchall()
            return DailySchedule(
                bot_id=row["bot_id"],
                schedule_date=row["schedule_date"],
                timezone=row["timezone"],
                wake_at=_dt(row["wake_at"]),
                sleep_at=_dt(row["sleep_at"]),
                events=tuple(self._event(item) for item in events),
            )

    @staticmethod
    def _event(row: sqlite3.Row) -> OfflineEvent:
        return OfflineEvent(
            id=row["id"],
            bot_id=row["bot_id"],
            schedule_date=row["schedule_date"],
            event_type=OfflineEventType(row["event_type"]),
            reason_id=row["reason_id"],
            pre_away_fact=row["pre_away_fact"],
            start_at=_dt(row["planned_start_at"]),
            end_at=_dt(row["planned_end_at"]),
            fixed_monitor_text=row["fixed_monitor_text"],
            state=row["state"],
        )

    async def get_event(self, event_id: str) -> OfflineEvent | None:
        async with self._lock:
            row = self._conn.execute(
                "SELECT * FROM offline_event WHERE id=?", (event_id,)
            ).fetchone()
            return self._event(row) if row else None

    async def events_near(self, bot_id: str, start: datetime, end: datetime) -> list[OfflineEvent]:
        async with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM offline_event WHERE bot_id=? AND planned_end_at>=? "
                "AND planned_start_at<=? ORDER BY planned_start_at",
                (bot_id, _iso(start), _iso(end)),
            ).fetchall()
            return [self._event(row) for row in rows]

    async def set_runtime(
        self, bot_id: str, state: PresenceState, event_id: str | None, now: datetime
    ) -> None:
        async with self._lock:
            self._conn.execute(
                "INSERT INTO runtime_state(bot_id,state,current_event_id,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(bot_id) DO UPDATE SET state=excluded.state,current_event_id=excluded.current_event_id,"
                "updated_at=excluded.updated_at",
                (bot_id, state.value, event_id, _iso(now)),
            )
            if event_id:
                self._conn.execute(
                    "UPDATE offline_event SET state=? WHERE id=?",
                    (state.value, event_id),
                )
            self._conn.commit()

    async def get_runtime(self, bot_id: str) -> RuntimePresence | None:
        async with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runtime_state WHERE bot_id=?", (bot_id,)
            ).fetchone()
            if not row:
                return None
            return RuntimePresence(
                bot_id=bot_id,
                state=PresenceState(row["state"]),
                current_event_id=row["current_event_id"],
                updated_at=_dt(row["updated_at"]),
            )

    async def ensure_notice(self, event_id: str, umo: str, due_at: datetime) -> None:
        async with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO pre_away_notice(offline_event_id,umo,state,due_at) VALUES(?,?,'PENDING',?)",
                (event_id, umo, _iso(due_at)),
            )
            self._conn.commit()

    async def claim_notice(self, event_id: str, umo: str) -> bool:
        async with self._lock:
            cursor = self._conn.execute(
                "UPDATE pre_away_notice SET state='CLAIMED' WHERE offline_event_id=? AND umo=? AND state='PENDING'",
                (event_id, umo),
            )
            self._conn.commit()
            return bool(cursor.rowcount)

    async def mark_notice_sent(
        self, event_id: str, umo: str, text: str, now: datetime
    ) -> None:
        async with self._lock:
            self._conn.execute(
                "UPDATE pre_away_notice SET state='SENT',generated_text=?,sent_at=? "
                "WHERE offline_event_id=? AND umo=?",
                (text, _iso(now), event_id, umo),
            )
            self._conn.commit()

    async def due_notices(
        self, now: datetime, bot_id: str | None = None
    ) -> list[tuple[str, str]]:
        async with self._lock:
            sql = (
                "SELECT n.offline_event_id,n.umo FROM pre_away_notice n "
                "JOIN offline_event e ON e.id=n.offline_event_id "
                "WHERE n.state='PENDING' AND n.due_at<=?"
            )
            params: list[str] = [_iso(now)]
            if bot_id is not None:
                sql += " AND e.bot_id=?"
                params.append(bot_id)
            rows = self._conn.execute(sql, params).fetchall()
            return [(row[0], row[1]) for row in rows]

    async def release_notice(self, event_id: str, umo: str) -> None:
        async with self._lock:
            self._conn.execute(
                "UPDATE pre_away_notice SET state='PENDING' WHERE offline_event_id=? AND umo=? AND state='CLAIMED'",
                (event_id, umo),
            )
            self._conn.commit()

    async def expire_notices(self, event_id: str) -> None:
        async with self._lock:
            self._conn.execute(
                "UPDATE pre_away_notice SET state='SKIPPED' WHERE offline_event_id=? AND state='PENDING'",
                (event_id,),
            )
            self._conn.commit()

    async def save_mailbox(
        self,
        event_id: str,
        umo: str,
        message_kind: str,
        group_id: str,
        sender_id: str,
        sender_name: str,
        message_id: str,
        timestamp: datetime,
        plain_text: str,
        components_json: str,
    ) -> bool:
        async with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO mailbox_message(offline_event_id,umo,message_kind,group_id,"
                "sender_id,sender_name,message_id,timestamp,plain_text,components_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    umo,
                    message_kind,
                    group_id,
                    sender_id,
                    sender_name,
                    message_id,
                    _iso(timestamp),
                    plain_text,
                    components_json,
                ),
            )
            self._conn.commit()
            return bool(cursor.rowcount)

    async def pending_mailbox(self, event_id: str) -> list[MailboxMessage]:
        async with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM mailbox_message WHERE offline_event_id=? AND selection_state='PENDING' "
                "ORDER BY timestamp,id",
                (event_id,),
            ).fetchall()
            return [self._mailbox(row) for row in rows]

    async def selected_mailbox(self, event_id: str) -> list[MailboxMessage]:
        async with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM mailbox_message WHERE offline_event_id=? AND selection_state='SELECTED' "
                "ORDER BY timestamp,id",
                (event_id,),
            ).fetchall()
            return [self._mailbox(row) for row in rows]

    async def mailbox_by_message_ids(
        self, event_id: str, message_ids: Iterable[str]
    ) -> list[MailboxMessage]:
        ids = list(message_ids)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        async with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM mailbox_message WHERE offline_event_id=? "
                f"AND message_id IN ({placeholders}) ORDER BY timestamp,id",
                [event_id, *ids],
            ).fetchall()
            return [self._mailbox(row) for row in rows]

    async def abandon_event(self, event_id: str) -> None:
        """服务器离线期间事件已结束时，不补演旧信箱和未生成回复。"""
        async with self._lock:
            self._conn.execute(
                "UPDATE mailbox_message SET selection_state='SKIPPED' WHERE offline_event_id=? "
                "AND selection_state IN ('PENDING','SELECTED')",
                (event_id,),
            )
            self._conn.execute(
                "UPDATE pre_away_notice SET state='SKIPPED' WHERE offline_event_id=? "
                "AND state IN ('PENDING','CLAIMED')",
                (event_id,),
            )
            self._conn.execute(
                "UPDATE offline_event SET state='MISSED' WHERE id=?", (event_id,)
            )
            self._conn.commit()

    @staticmethod
    def _mailbox(row: sqlite3.Row) -> MailboxMessage:
        return MailboxMessage(
            id=row["id"],
            offline_event_id=row["offline_event_id"],
            umo=row["umo"],
            message_kind=row["message_kind"],
            group_id=row["group_id"],
            sender_id=row["sender_id"],
            sender_name=row["sender_name"],
            message_id=row["message_id"],
            timestamp=_dt(row["timestamp"]),
            plain_text=row["plain_text"],
            components_json=row["components_json"],
            selection_state=QueueState(row["selection_state"]),
        )

    async def set_mailbox_states(
        self, selected_ids: Iterable[int], skipped_ids: Iterable[int]
    ) -> None:
        async with self._lock:
            self._conn.executemany(
                "UPDATE mailbox_message SET selection_state='SELECTED' WHERE id=? AND selection_state='PENDING'",
                [(item,) for item in selected_ids],
            )
            self._conn.executemany(
                "UPDATE mailbox_message SET selection_state='SKIPPED' WHERE id=? AND selection_state='PENDING'",
                [(item,) for item in skipped_ids],
            )
            self._conn.commit()

    async def mark_messages_generated(self, ids: Iterable[int]) -> None:
        async with self._lock:
            self._conn.executemany(
                "UPDATE mailbox_message SET selection_state='GENERATED' WHERE id=? AND selection_state='SELECTED'",
                [(item,) for item in ids],
            )
            self._conn.commit()

    async def mark_messages_failed(self, ids: Iterable[int]) -> None:
        async with self._lock:
            self._conn.executemany(
                "UPDATE mailbox_message SET selection_state='FAILED' WHERE id=? "
                "AND selection_state IN ('SELECTED','GENERATED')",
                [(item,) for item in ids],
            )
            self._conn.commit()

    async def add_return_reply(
        self,
        event_id: str,
        umo: str,
        message_kind: str,
        sender_id: str,
        source_ids: list[str],
        quote_id: str,
        text: str,
        scheduled_at: datetime,
    ) -> int:
        async with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO return_reply(offline_event_id,umo,message_kind,sender_id,source_message_ids,"
                "quote_message_id,generated_text,send_state,scheduled_send_at) VALUES(?,?,?,?,?,?,?,'GENERATED',?)",
                (
                    event_id,
                    umo,
                    message_kind,
                    sender_id,
                    json.dumps(source_ids, ensure_ascii=False),
                    quote_id,
                    text,
                    _iso(scheduled_at),
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    async def due_replies(
        self, now: datetime, bot_id: str | None = None
    ) -> list[ReturnReply]:
        async with self._lock:
            sql = (
                "SELECT r.* FROM return_reply r JOIN offline_event e ON e.id=r.offline_event_id "
                "WHERE r.send_state='GENERATED' AND r.scheduled_send_at<=?"
            )
            params: list[str] = [_iso(now)]
            if bot_id is not None:
                sql += " AND e.bot_id=?"
                params.append(bot_id)
            sql += " ORDER BY r.scheduled_send_at,r.id"
            rows = self._conn.execute(sql, params).fetchall()
            return [self._reply(row) for row in rows]

    @staticmethod
    def _reply(row: sqlite3.Row) -> ReturnReply:
        return ReturnReply(
            id=row["id"],
            offline_event_id=row["offline_event_id"],
            umo=row["umo"],
            message_kind=row["message_kind"],
            sender_id=row["sender_id"],
            source_message_ids=tuple(json.loads(row["source_message_ids"])),
            quote_message_id=row["quote_message_id"],
            generated_text=row["generated_text"],
            send_state=QueueState(row["send_state"]),
            scheduled_send_at=_dt(row["scheduled_send_at"]),
        )

    async def mark_reply_sent(self, reply_id: int, now: datetime) -> None:
        async with self._lock:
            row = self._conn.execute(
                "SELECT offline_event_id,source_message_ids FROM return_reply WHERE id=?",
                (reply_id,),
            ).fetchone()
            self._conn.execute(
                "UPDATE return_reply SET send_state='SENT',sent_at=? WHERE id=?",
                (_iso(now), reply_id),
            )
            if row:
                ids = json.loads(row["source_message_ids"])
                self._conn.executemany(
                    "UPDATE mailbox_message SET selection_state='SENT' WHERE offline_event_id=? AND message_id=?",
                    [(row["offline_event_id"], item) for item in ids],
                )
            self._conn.commit()

    async def mark_reply_failed(self, reply_id: int, error: str) -> None:
        async with self._lock:
            row = self._conn.execute(
                "SELECT offline_event_id,source_message_ids FROM return_reply WHERE id=?",
                (reply_id,),
            ).fetchone()
            self._conn.execute(
                "UPDATE return_reply SET send_state='FAILED',error=? WHERE id=?",
                (error[:1000], reply_id),
            )
            if row:
                ids = json.loads(row["source_message_ids"])
                self._conn.executemany(
                    "UPDATE mailbox_message SET selection_state='FAILED' WHERE offline_event_id=? AND message_id=?",
                    [(row["offline_event_id"], item) for item in ids],
                )
            self._conn.commit()

    async def event_has_work(self, event_id: str) -> bool:
        async with self._lock:
            mailbox = self._conn.execute(
                "SELECT 1 FROM mailbox_message WHERE offline_event_id=? AND selection_state IN "
                "('PENDING','SELECTED','GENERATED') LIMIT 1",
                (event_id,),
            ).fetchone()
            replies = self._conn.execute(
                "SELECT 1 FROM return_reply WHERE offline_event_id=? AND send_state='GENERATED' LIMIT 1",
                (event_id,),
            ).fetchone()
            return bool(mailbox or replies)

    async def save_group_context(
        self,
        umo: str,
        sender_id: str,
        sender_name: str,
        message_id: str,
        timestamp: datetime,
        text: str,
        max_count: int = 100,
    ) -> None:
        if not text.strip():
            return
        async with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO group_context(umo,sender_id,sender_name,message_id,timestamp,plain_text) "
                "VALUES(?,?,?,?,?,?)",
                (umo, sender_id, sender_name, message_id, _iso(timestamp), text),
            )
            self._conn.execute(
                "DELETE FROM group_context WHERE umo=? AND id NOT IN "
                "(SELECT id FROM group_context WHERE umo=? ORDER BY id DESC LIMIT ?)",
                (umo, umo, max_count),
            )
            self._conn.commit()

    async def recent_group_context(self, umo: str, limit: int = 30) -> list[dict[str, str]]:
        async with self._lock:
            rows = self._conn.execute(
                "SELECT sender_name,timestamp,plain_text FROM group_context WHERE umo=? "
                "ORDER BY id DESC LIMIT ?",
                (umo, limit),
            ).fetchall()
            return [dict(row) for row in reversed(rows)]
