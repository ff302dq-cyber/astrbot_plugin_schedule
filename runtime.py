from __future__ import annotations

import asyncio
import random
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from astrbot.api import logger

from .config import PluginSettings
from .llm_service import LLMService, group_by_sender, normalize_notice
from .models import (
    DailySchedule,
    MailboxMessage,
    OfflineEvent,
    OfflineEventType,
    PresenceState,
)
from .repository import Repository
from .schedule import ScheduleGenerator, sample_group_messages

SendText = Callable[[str, str], Awaitable[None]]
SendReply = Callable[[str, str, str, str], Awaitable[None]]


class RuntimeService:
    def __init__(
        self,
        bot_id: str,
        settings: PluginSettings,
        repository: Repository,
        llm: LLMService,
        send_text: SendText,
        send_reply: SendReply,
        rng=None,
    ):
        self.bot_id = bot_id
        self.settings = settings
        self.repository = repository
        self.llm = llm
        self.send_text = send_text
        self.send_reply = send_reply
        self.rng = rng or random.SystemRandom()
        self.generator = ScheduleGenerator(settings, self.rng)
        self._bot_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._return_tasks: dict[str, asyncio.Task] = {}

    async def ensure_calendar(self, bot_id: str, now: datetime) -> None:
        for day in (now.date() - timedelta(days=1), now.date(), now.date() + timedelta(days=1)):
            if await self.repository.get_schedule(bot_id, day.isoformat()):
                continue
            schedule = self.generator.generate_day(bot_id, day)
            await self.repository.save_schedule(schedule, now)

        for day in (now.date() - timedelta(days=1), now.date()):
            current = await self.repository.get_schedule(bot_id, day.isoformat())
            following = await self.repository.get_schedule(
                bot_id, (day + timedelta(days=1)).isoformat()
            )
            if not current or not following:
                continue
            event_id = f"sleep-{bot_id}-{day.isoformat()}"
            if not await self.repository.get_event(event_id):
                await self.repository.add_event(
                    self.generator.make_sleep_event(current, following.wake_at)
                )

    async def adjust_future_schedule(
        self, bot_id: str, now: datetime
    ) -> DailySchedule:
        """Rebuild the global current/future plan without touching an active event."""
        async with self._bot_locks[bot_id]:
            events = await self.repository.events_near(
                bot_id, now - timedelta(days=1), now + timedelta(days=2)
            )
            active = next(
                (event for event in events if event.start_at <= now < event.end_at),
                None,
            )
            safe_start = active.end_at if active else now
            today = now.date()
            tomorrow = today + timedelta(days=1)
            await self.repository.clear_future_plans(
                bot_id,
                now,
                today.isoformat(),
                tomorrow.isoformat(),
            )

            rebuilt: list[DailySchedule] = []
            for day in (today, tomorrow):
                generated = self.generator.generate_day(bot_id, day)
                schedule = DailySchedule(
                    bot_id=generated.bot_id,
                    schedule_date=generated.schedule_date,
                    timezone=generated.timezone,
                    wake_at=generated.wake_at,
                    sleep_at=generated.sleep_at,
                    events=tuple(
                        event
                        for event in generated.events
                        if event.start_at >= safe_start
                    ),
                )
                await self.repository.save_schedule(schedule, now)
                rebuilt.append(schedule)

            sleep = self.generator.make_sleep_event(
                rebuilt[0], rebuilt[1].wake_at
            )
            if sleep.start_at >= safe_start:
                await self.repository.add_event(sleep)
            return rebuilt[0]

    async def reconcile(self, bot_id: str, now: datetime) -> PresenceState:
        async with self._bot_locks[bot_id]:
            await self.ensure_calendar(bot_id, now)
            runtime = await self.repository.get_runtime(bot_id)
            if runtime and runtime.state == PresenceState.RETURNING and runtime.current_event_id:
                if await self.repository.event_has_work(runtime.current_event_id):
                    self.start_return(runtime.current_event_id)
                    return PresenceState.RETURNING
                await self.repository.set_runtime(bot_id, PresenceState.ONLINE, None, now)

            events = await self.repository.events_near(
                bot_id, now - timedelta(days=1), now + timedelta(days=1)
            )
            active = next((event for event in events if event.start_at <= now < event.end_at), None)
            if active:
                state = (
                    PresenceState.SLEEPING
                    if active.event_type == OfflineEventType.NIGHT_SLEEP
                    else PresenceState.AWAY
                )
                if not runtime or runtime.state != state or runtime.current_event_id != active.id:
                    await self.repository.expire_notices(active.id)
                # 同状态也更新时间：既持久化当前状态，也作为重启恢复的心跳。
                await self.repository.set_runtime(bot_id, state, active.id, now)
                return state

            if self.settings.pre_away_enabled:
                advance = timedelta(minutes=self.settings.pre_away_advance_minutes)
                upcoming = next(
                    (
                        event
                        for event in events
                        if event.start_at - advance <= now < event.start_at
                    ),
                    None,
                )
                if upcoming:
                    await self.repository.set_runtime(
                        bot_id, PresenceState.PRE_AWAY, upcoming.id, now
                    )
                    await self._prepare_notices(upcoming, now)
                    return PresenceState.PRE_AWAY

            if runtime and runtime.current_event_id and runtime.state in {
                PresenceState.AWAY,
                PresenceState.SLEEPING,
            }:
                previous = await self.repository.get_event(runtime.current_event_id)
                if previous and previous.end_at <= now:
                    stale_after = timedelta(
                        seconds=max(15.0, self.settings.scheduler_interval_seconds * 4)
                    )
                    if (
                        now - runtime.updated_at > stale_after
                        and now - previous.end_at > stale_after
                    ):
                        await self.repository.abandon_event(previous.id)
                    elif await self.repository.event_has_work(previous.id):
                        await self.repository.set_runtime(
                            bot_id, PresenceState.RETURNING, previous.id, now
                        )
                        self.start_return(previous.id)
                        return PresenceState.RETURNING

            await self.repository.set_runtime(bot_id, PresenceState.ONLINE, None, now)
            return PresenceState.ONLINE

    async def _prepare_notices(self, event: OfflineEvent, now: datetime) -> None:
        since = now - timedelta(minutes=self.settings.active_private_window_minutes)
        sessions = await self.repository.active_pre_away_sessions(event.bot_id, since)
        due = min(
            now + timedelta(seconds=self.settings.pre_away_fallback_seconds),
            event.start_at - timedelta(seconds=2),
        )
        for umo in sessions:
            await self.repository.ensure_notice(event.id, umo, due)

    async def refresh_pre_away_session(
        self, bot_id: str, umo: str, now: datetime
    ) -> OfflineEvent | None:
        state = await self.reconcile(bot_id, now)
        if state != PresenceState.PRE_AWAY:
            return None
        runtime = await self.repository.get_runtime(bot_id)
        if not runtime or not runtime.current_event_id:
            return None
        event = await self.repository.get_event(runtime.current_event_id)
        if not event:
            return None
        due = min(
            now + timedelta(seconds=self.settings.pre_away_fallback_seconds),
            event.start_at - timedelta(seconds=2),
        )
        await self.repository.ensure_notice(event.id, umo, due)
        return event

    async def process_due_notices(self, now: datetime) -> None:
        for event_id, umo in await self.repository.due_notices(now, self.bot_id):
            event = await self.repository.get_event(event_id)
            if not event or now >= event.start_at:
                if event:
                    await self.repository.expire_notices(event.id)
                continue
            last_seen = await self.repository.session_last_seen(umo)
            active_since = now - timedelta(
                minutes=self.settings.active_private_window_minutes
            )
            if last_seen is None or last_seen < active_since:
                await self.repository.skip_notice(event_id, umo)
                continue
            if not await self.repository.claim_notice(event_id, umo):
                continue
            try:
                message_kind = await self.repository.session_kind(umo) or "private"
                text = normalize_notice(
                    await self.llm.pre_away(event, umo, message_kind),
                    "我等会要离开一下，可能不看手机了",
                )
                await self.send_text(umo, text)
                await self.repository.mark_notice_sent(event_id, umo, text, now)
            except Exception as exc:  # noqa: BLE001 - 单窗口失败不终止调度器
                logger.error(f"[角色作息] 私聊预告失败：{exc}", exc_info=True)
                await self.repository.release_notice(event_id, umo)

    def start_return(self, event_id: str) -> None:
        task = self._return_tasks.get(event_id)
        if task and not task.done():
            return
        task = asyncio.create_task(self._process_return(event_id))
        self._return_tasks[event_id] = task
        task.add_done_callback(lambda _task, key=event_id: self._return_tasks.pop(key, None))

    async def _process_return(self, event_id: str) -> None:
        event = await self.repository.get_event(event_id)
        if not event:
            return
        try:
            selected = await self.repository.selected_mailbox(event_id)
            if selected:
                await self._generate_selected(event, selected)

            pending = await self.repository.pending_mailbox(event_id)
            by_umo: dict[str, list[MailboxMessage]] = defaultdict(list)
            for message in pending:
                by_umo[message.umo].append(message)
            for messages in by_umo.values():
                if messages[0].message_kind == "private":
                    chosen = messages
                else:
                    chosen = sample_group_messages(
                        messages,
                        self.settings.sample_rate,
                        self.settings.sample_fluctuation,
                        self.rng,
                    )
                chosen_ids = {item.id for item in chosen}
                await self.repository.set_mailbox_states(
                    chosen_ids,
                    [item.id for item in messages if item.id not in chosen_ids],
                )
                if chosen:
                    await self._generate_selected(event, chosen)
        except Exception as exc:  # noqa: BLE001 - 生成异常由持久状态恢复
            logger.error(f"[角色作息] 回归生成失败 event={event_id}: {exc}", exc_info=True)

    async def _generate_selected(
        self, event: OfflineEvent, messages: list[MailboxMessage]
    ) -> None:
        by_umo: dict[str, list[MailboxMessage]] = defaultdict(list)
        for message in messages:
            by_umo[message.umo].append(message)
        scheduled = datetime.now(self.settings.tz)
        for umo, window_messages in by_umo.items():
            try:
                if window_messages[0].message_kind == "private":
                    text = await self.llm.private_return(event, umo, window_messages)
                    if not text:
                        raise RuntimeError("私聊回归模型返回空内容")
                    if not await self.repository.has_return_notice(event.id, umo):
                        notice_fallback = (
                            "醒了"
                            if event.event_type == OfflineEventType.NIGHT_SLEEP
                            else "刚回来"
                        )
                        notice = normalize_notice(
                            await self.llm.return_notice(event, umo), notice_fallback
                        )
                        scheduled += timedelta(
                            seconds=self._delay(True, window_messages, notice)
                        )
                        await self.repository.add_return_reply(
                            event.id,
                            umo,
                            "private_notice",
                            window_messages[-1].sender_id,
                            [],
                            "",
                            notice,
                            scheduled,
                        )
                        scheduled += timedelta(
                            seconds=self._delay(False, window_messages, text)
                        )
                    else:
                        scheduled += timedelta(
                            seconds=self._delay(True, window_messages, text)
                        )
                    await self.repository.add_return_reply(
                        event.id,
                        umo,
                        "private",
                        window_messages[-1].sender_id,
                        [item.message_id for item in window_messages],
                        "",
                        text,
                        scheduled,
                    )
                    await self.repository.mark_messages_generated(
                        item.id for item in window_messages
                    )
                    continue

                replies = await self.llm.group_return(event, umo, window_messages)
                grouped = group_by_sender(window_messages)
                first = True
                for sender_id, sender_messages in grouped.items():
                    text = replies.get(sender_id) or "刚看到你之前的消息，晚点再和你细说。"
                    scheduled += timedelta(
                        seconds=self._delay(first, sender_messages, text)
                    )
                    first = False
                    await self.repository.add_return_reply(
                        event.id,
                        umo,
                        "group",
                        sender_id,
                        [item.message_id for item in sender_messages],
                        sender_messages[-1].message_id,
                        text,
                        scheduled,
                    )
                    await self.repository.mark_messages_generated(
                        item.id for item in sender_messages
                    )
            except Exception as exc:  # noqa: BLE001 - 防止失败窗口反复调用模型
                await self.repository.mark_messages_failed(
                    item.id for item in window_messages
                )
                logger.error(
                    f"[角色作息] 窗口回归生成失败，已停止自动重试 umo={umo}: {exc}",
                    exc_info=True,
                )

    def _delay(self, first: bool, messages: list[MailboxMessage], reply: str) -> int:
        if first:
            delay = self.rng.randint(self.settings.first_delay_min, self.settings.first_delay_max)
        else:
            delay = self.rng.randint(
                self.settings.between_delay_min, self.settings.between_delay_max
            )
        total_length = len(reply) + sum(len(item.plain_text) for item in messages)
        if total_length >= self.settings.long_text_threshold:
            delay = max(delay, self.settings.long_reply_min_delay)
        return min(delay, self.settings.max_delay)

    async def process_due_replies(self, now: datetime) -> None:
        for reply in await self.repository.due_replies(now, self.bot_id):
            try:
                if reply.message_kind == "group":
                    await self.send_reply(
                        reply.umo,
                        reply.quote_message_id,
                        reply.sender_id,
                        reply.generated_text,
                    )
                elif reply.message_kind == "private":
                    await self.send_text(reply.umo, reply.generated_text)
                    messages = await self.repository.mailbox_by_message_ids(
                        reply.offline_event_id, reply.source_message_ids
                    )
                    await self.llm.record_private_return(
                        reply.umo, messages, reply.generated_text
                    )
                else:
                    await self.send_text(reply.umo, reply.generated_text)
                await self.repository.mark_reply_sent(reply.id, now)
                await self._finish_if_idle(reply.offline_event_id, now)
            except Exception as exc:  # noqa: BLE001 - 单条失败不阻断后续队列
                logger.error(f"[角色作息] 回归回复发送失败：{exc}", exc_info=True)
                await self.repository.mark_reply_failed(reply.id, str(exc))

    async def _finish_if_idle(self, event_id: str, now: datetime) -> None:
        if await self.repository.event_has_work(event_id):
            pending = await self.repository.pending_mailbox(event_id)
            if pending:
                self.start_return(event_id)
            return
        event = await self.repository.get_event(event_id)
        if event:
            await self.repository.set_runtime(event.bot_id, PresenceState.ONLINE, None, now)

    async def shutdown(self) -> None:
        tasks = list(self._return_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
