import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot_plugin_tiangan_schedule.models import (
    DailySchedule,
    OfflineEvent,
    OfflineEventType,
    PresenceState,
)
from astrbot_plugin_tiangan_schedule.repository import Repository

TZ = ZoneInfo("Asia/Shanghai")


def run(coro):
    return asyncio.run(coro)


def make_event() -> OfflineEvent:
    start = datetime(2026, 8, 15, 12, tzinfo=TZ)
    return OfflineEvent(
        id="event-1",
        bot_id="bot-1",
        schedule_date="2026-08-15",
        event_type=OfflineEventType.DAYTIME_AWAY,
        reason_id="reading",
        pre_away_fact="几分钟后去看书",
        start_at=start,
        end_at=start + timedelta(minutes=30),
        fixed_monitor_text="不在",
    )


def test_schedule_is_not_replaced_on_same_day(tmp_path: Path):
    async def scenario():
        repo = Repository(tmp_path / "state.sqlite3")
        event = make_event()
        schedule = DailySchedule(
            "bot-1",
            "2026-08-15",
            "Asia/Shanghai",
            datetime(2026, 8, 15, 9, tzinfo=TZ),
            datetime(2026, 8, 15, 23, 30, tzinfo=TZ),
            (event,),
        )
        assert await repo.save_schedule(schedule, datetime.now(TZ)) is True
        changed = DailySchedule(
            "bot-1",
            "2026-08-15",
            "Asia/Shanghai",
            datetime(2026, 8, 15, 8, tzinfo=TZ),
            datetime(2026, 8, 15, 23, tzinfo=TZ),
            (),
        )
        assert await repo.save_schedule(changed, datetime.now(TZ)) is False
        loaded = await repo.get_schedule("bot-1", "2026-08-15")
        assert loaded.wake_at == schedule.wake_at
        assert loaded.events[0].id == event.id
        await repo.close()

    run(scenario())


def test_mailbox_deduplicates_and_persists_queue(tmp_path: Path):
    async def scenario():
        repo = Repository(tmp_path / "state.sqlite3")
        event = make_event()
        await repo.add_event(event)
        args = (
            event.id,
            "aiocqhttp:GroupMessage:1",
            "group",
            "1",
            "user-1",
            "用户",
            "message-1",
            event.start_at,
            "在吗",
            "[]",
        )
        assert await repo.save_mailbox(*args) is True
        assert await repo.save_mailbox(*args) is False
        pending = await repo.pending_mailbox(event.id)
        await repo.set_mailbox_states([pending[0].id], [])
        selected = await repo.selected_mailbox(event.id)
        assert len(selected) == 1
        reply_id = await repo.add_return_reply(
            event.id,
            selected[0].umo,
            "group",
            "user-1",
            ["message-1"],
            "message-1",
            "看见了",
            event.end_at,
        )
        await repo.mark_messages_generated([selected[0].id])
        assert await repo.event_has_work(event.id)
        await repo.mark_reply_sent(reply_id, event.end_at)
        assert not await repo.event_has_work(event.id)
        await repo.close()

    run(scenario())


def test_runtime_state_survives_reopen(tmp_path: Path):
    async def scenario():
        path = tmp_path / "state.sqlite3"
        repo = Repository(path)
        now = datetime.now(TZ)
        await repo.set_runtime("bot-1", PresenceState.SLEEPING, "sleep-1", now)
        await repo.close()
        reopened = Repository(path)
        state = await reopened.get_runtime("bot-1")
        assert state.state == PresenceState.SLEEPING
        assert state.current_event_id == "sleep-1"
        await reopened.close()

    run(scenario())


def test_private_offline_monitor_is_claimed_once_per_event_and_session(tmp_path: Path):
    async def scenario():
        repo = Repository(tmp_path / "state.sqlite3")
        now = datetime.now(TZ)
        assert await repo.claim_offline_monitor("event-1", "private-1") is True
        assert await repo.claim_offline_monitor("event-1", "private-1") is False
        await repo.mark_offline_monitor_sent("event-1", "private-1", now)
        assert await repo.claim_offline_monitor("event-1", "private-1") is False
        assert await repo.claim_offline_monitor("event-2", "private-1") is True
        assert await repo.claim_offline_monitor("event-1", "private-2") is True
        await repo.close()

    run(scenario())


def test_failed_private_monitor_claim_can_be_released_and_retried(tmp_path: Path):
    async def scenario():
        repo = Repository(tmp_path / "state.sqlite3")
        assert await repo.claim_offline_monitor("event-1", "private-1") is True
        await repo.release_offline_monitor("event-1", "private-1")
        assert await repo.claim_offline_monitor("event-1", "private-1") is True
        await repo.close()

    run(scenario())


def test_clear_future_plans_preserves_active_event_and_history(tmp_path: Path):
    async def scenario():
        repo = Repository(tmp_path / "state.sqlite3")
        now = datetime(2026, 8, 15, 12, 15, tzinfo=TZ)
        active = make_event()
        future = OfflineEvent(
            id="future-event",
            bot_id="bot-1",
            schedule_date="2026-08-15",
            event_type=OfflineEventType.DAYTIME_AWAY,
            reason_id="old",
            pre_away_fact="旧计划",
            start_at=now + timedelta(hours=2),
            end_at=now + timedelta(hours=3),
            fixed_monitor_text="旧提示",
        )
        tomorrow = OfflineEvent(
            id="tomorrow-event",
            bot_id="bot-1",
            schedule_date="2026-08-16",
            event_type=OfflineEventType.DAYTIME_AWAY,
            reason_id="old",
            pre_away_fact="旧计划",
            start_at=now + timedelta(days=1),
            end_at=now + timedelta(days=1, hours=1),
            fixed_monitor_text="旧提示",
        )
        await repo.add_event(active)
        await repo.add_event(future)
        await repo.add_event(tomorrow)

        removed = await repo.clear_future_plans(
            "bot-1", now, "2026-08-15", "2026-08-16"
        )
        assert removed == 2
        assert await repo.get_event(active.id) == active
        assert await repo.get_event(future.id) is None
        assert await repo.get_event(tomorrow.id) is None
        await repo.close()

    run(scenario())


def test_plugin_meta_persists_across_reopen(tmp_path: Path):
    async def scenario():
        path = tmp_path / "state.sqlite3"
        repo = Repository(path)
        await repo.set_meta("fingerprint", "abc")
        await repo.close()
        reopened = Repository(path)
        assert await reopened.get_meta("fingerprint") == "abc"
        await reopened.close()

    run(scenario())
