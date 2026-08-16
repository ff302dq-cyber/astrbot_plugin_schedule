import asyncio
import random
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot_plugin_tiangan_schedule.config import load_settings
from astrbot_plugin_tiangan_schedule.models import (
    OfflineEvent,
    OfflineEventType,
    PresenceState,
)
from astrbot_plugin_tiangan_schedule.repository import Repository
from astrbot_plugin_tiangan_schedule.runtime import RuntimeService

TZ = ZoneInfo("Asia/Shanghai")


class FakeLLM:
    def __init__(self):
        self.group_calls = 0
        self.private_calls = 0

    async def group_return(self, _event, _umo, messages):
        self.group_calls += 1
        return {
            item.sender_id: f"回复{item.sender_id}"
            for item in messages
        }

    async def private_return(self, _event, _umo, messages):
        self.private_calls += 1
        return "合并回复：" + "、".join(item.plain_text for item in messages)

    async def pre_away(self, _event, _umo):
        return "我一会儿要出去。"

    async def record_private_return(self, _umo, _messages, _reply_text):
        return None


def make_event() -> OfflineEvent:
    now = datetime.now(TZ)
    return OfflineEvent(
        "event-return",
        "bot-1",
        now.date().isoformat(),
        OfflineEventType.DAYTIME_AWAY,
        "reading",
        "几分钟后去看书",
        now - timedelta(hours=1),
        now - timedelta(minutes=1),
        "暂时不在",
    )


def test_group_return_calls_llm_once_and_sends_per_user(tmp_path: Path):
    async def scenario():
        settings = load_settings(
            {
                "group_return": {"sample_rate": 1.0, "sample_fluctuation": 0.0},
                "return_queue": {
                    "first_reply_delay_min_seconds": 0,
                    "first_reply_delay_max_seconds": 0,
                    "between_reply_delay_min_seconds": 0,
                    "between_reply_delay_max_seconds": 0,
                },
            }
        )
        repo = Repository(tmp_path / "runtime.sqlite3")
        event = make_event()
        await repo.add_event(event)
        await repo.set_runtime(
            event.bot_id, PresenceState.RETURNING, event.id, datetime.now(TZ)
        )
        for index, sender in enumerate(("u1", "u1", "u2"), 1):
            await repo.save_mailbox(
                event.id,
                "aiocqhttp:GroupMessage:100",
                "group",
                "100",
                sender,
                sender,
                f"m{index}",
                datetime.now(TZ) + timedelta(seconds=index),
                f"消息{index}",
                "[]",
            )
        sent = []
        llm = FakeLLM()

        async def send_text(umo, text):
            sent.append(("text", umo, text))

        async def send_reply(umo, quote, sender, text):
            sent.append(("reply", umo, quote, sender, text))

        runtime = RuntimeService(
            "bot-1", settings, repo, llm, send_text, send_reply, random.Random(1)
        )
        await runtime._process_return(event.id)
        await runtime.process_due_replies(datetime.now(TZ) + timedelta(seconds=5))
        assert llm.group_calls == 1
        assert len(sent) == 2
        assert {item[3] for item in sent} == {"u1", "u2"}
        assert all(item[0] == "reply" for item in sent)
        state = await repo.get_runtime(event.bot_id)
        assert state.state == PresenceState.ONLINE
        await repo.close()

    asyncio.run(scenario())


def test_private_return_uses_all_messages_in_one_call(tmp_path: Path):
    async def scenario():
        settings = load_settings(
            {
                "return_queue": {
                    "first_reply_delay_min_seconds": 0,
                    "first_reply_delay_max_seconds": 0,
                }
            }
        )
        repo = Repository(tmp_path / "private.sqlite3")
        event = make_event()
        await repo.add_event(event)
        await repo.set_runtime(
            event.bot_id, PresenceState.RETURNING, event.id, datetime.now(TZ)
        )
        for index in range(3):
            await repo.save_mailbox(
                event.id,
                "aiocqhttp:FriendMessage:u1",
                "private",
                "",
                "u1",
                "用户",
                f"p{index}",
                datetime.now(TZ) + timedelta(seconds=index),
                f"私聊{index}",
                "[]",
            )
        sent = []
        llm = FakeLLM()

        async def send_text(umo, text):
            sent.append((umo, text))

        async def send_reply(*_args):
            raise AssertionError("私聊不应走群引用回复")

        runtime = RuntimeService(
            "bot-1", settings, repo, llm, send_text, send_reply, random.Random(2)
        )
        await runtime._process_return(event.id)
        await runtime.process_due_replies(datetime.now(TZ) + timedelta(seconds=5))
        assert llm.private_calls == 1
        assert len(sent) == 1
        assert all(f"私聊{index}" in sent[0][1] for index in range(3))
        await repo.close()

    asyncio.run(scenario())


def test_event_finished_during_long_shutdown_is_not_replayed(tmp_path: Path):
    async def scenario():
        settings = load_settings(
            {
                "wake_window": {"start": "08:00", "end": "08:01"},
                "sleep_window": {"start": "23:00", "end": "23:01"},
                "daytime_away": {"enabled": False},
                "scheduler_interval_seconds": 1,
            }
        )
        repo = Repository(tmp_path / "missed.sqlite3")
        now = datetime(2026, 8, 15, 12, tzinfo=TZ)
        event = OfflineEvent(
            "missed-event",
            "bot-1",
            now.date().isoformat(),
            OfflineEventType.DAYTIME_AWAY,
            "outside",
            "几分钟后出门",
            now - timedelta(hours=2),
            now - timedelta(hours=1),
            "暂时不在",
        )
        await repo.add_event(event)
        await repo.set_runtime(
            event.bot_id,
            PresenceState.AWAY,
            event.id,
            now - timedelta(hours=2),
        )
        await repo.save_mailbox(
            event.id,
            "aiocqhttp:FriendMessage:u1",
            "private",
            "",
            "u1",
            "用户",
            "old-message",
            now - timedelta(hours=1, minutes=30),
            "旧消息",
            "[]",
        )

        async def no_send(*_args):
            raise AssertionError("错过的事件不应补发")

        runtime = RuntimeService(
            "bot-1", settings, repo, FakeLLM(), no_send, no_send, random.Random(3)
        )
        state = await runtime.reconcile("bot-1", now)
        assert state == PresenceState.ONLINE
        assert not await repo.event_has_work(event.id)
        await repo.close()

    asyncio.run(scenario())


def test_adjust_future_schedule_replaces_pending_reasons_but_keeps_active(tmp_path: Path):
    async def scenario():
        settings = load_settings(
            {
                "wake_window": {"start": "08:00", "end": "08:01"},
                "sleep_window": {"start": "23:00", "end": "23:01"},
                "reasons": {
                    "daytime_json": """
                    [
                      {
                        "id": "read_book",
                        "pre_away_fact": "几分钟后去看书",
                        "monitor_messages": ["正在看书"]
                      }
                    ]
                    """
                },
            }
        )
        repo = Repository(tmp_path / "adjust.sqlite3")
        now = datetime(2026, 8, 15, 12, tzinfo=TZ)
        active = OfflineEvent(
            "active-old",
            "bot-1",
            "2026-08-15",
            OfflineEventType.DAYTIME_AWAY,
            "old_reason",
            "旧活动正在进行",
            now - timedelta(minutes=15),
            now + timedelta(minutes=30),
            "旧活动",
        )
        pending = OfflineEvent(
            "pending-old",
            "bot-1",
            "2026-08-15",
            OfflineEventType.DAYTIME_AWAY,
            "old_reason",
            "旧计划",
            now + timedelta(hours=2),
            now + timedelta(hours=3),
            "睡午觉去了",
        )
        await repo.add_event(active)
        await repo.add_event(pending)

        async def no_send(*_args):
            return None

        runtime = RuntimeService(
            "bot-1",
            settings,
            repo,
            FakeLLM(),
            no_send,
            no_send,
            random.Random(12),
        )
        await runtime.adjust_future_schedule("bot-1", now)

        assert await repo.get_event(active.id) == active
        assert await repo.get_event(pending.id) is None
        events = await repo.events_near(
            "bot-1", now, now + timedelta(days=2)
        )
        future_daytime = [
            event
            for event in events
            if event.event_type == OfflineEventType.DAYTIME_AWAY
            and event.start_at > now
        ]
        assert future_daytime
        assert {event.reason_id for event in future_daytime} == {"read_book"}
        assert all("睡午觉" not in event.fixed_monitor_text for event in events)
        await repo.close()

    asyncio.run(scenario())
