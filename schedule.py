from __future__ import annotations

import math
import random
from datetime import date, datetime, time, timedelta
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from .config import PluginSettings
from .models import DailySchedule, OfflineEvent, OfflineEventType


class RandomSource(Protocol):
    def randint(self, a: int, b: int) -> int: ...
    def choice(self, seq): ...


def parse_clock(value: str) -> time:
    try:
        hour_text, minute_text = value.strip().split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"无效时间：{value!r}，应使用 HH:MM") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"无效时间：{value!r}，应使用 HH:MM")
    return time(hour=hour, minute=minute)


def sample_window(
    day: date,
    start_text: str,
    end_text: str,
    tz: ZoneInfo,
    rng: RandomSource,
) -> datetime:
    start = datetime.combine(day, parse_clock(start_text), tzinfo=tz)
    end = datetime.combine(day, parse_clock(end_text), tzinfo=tz)
    if end <= start:
        end += timedelta(days=1)
    seconds = max(0, int((end - start).total_seconds()))
    return start + timedelta(seconds=rng.randint(0, seconds))


def _split_total(total: int, count: int, minimum: int, maximum: int, rng: RandomSource) -> list[int]:
    durations = [minimum] * count
    remaining = total - minimum * count
    order = list(range(count))
    while remaining > 0:
        candidates = [i for i in order if durations[i] < maximum]
        if not candidates:
            break
        index = rng.choice(candidates)
        addition = rng.randint(1, min(remaining, maximum - durations[index]))
        durations[index] += addition
        remaining -= addition
    return durations


def _place_segments(
    start: datetime,
    end: datetime,
    durations: list[int],
    rng: RandomSource,
) -> list[tuple[datetime, datetime]]:
    occupied = sum(durations)
    available = max(0, int((end - start).total_seconds() // 60))
    slack = max(0, available - occupied)
    gaps = [0] * (len(durations) + 1)
    for _ in range(slack):
        gaps[rng.randint(0, len(gaps) - 1)] += 1
    cursor = start + timedelta(minutes=gaps[0])
    result = []
    for index, duration in enumerate(durations):
        segment_end = cursor + timedelta(minutes=duration)
        result.append((cursor, segment_end))
        cursor = segment_end + timedelta(minutes=gaps[index + 1])
    return result


class ScheduleGenerator:
    def __init__(self, settings: PluginSettings, rng: RandomSource | None = None):
        self.settings = settings
        self.rng = rng or random.SystemRandom()

    def generate_day(self, bot_id: str, day: date) -> DailySchedule:
        tz = self.settings.tz
        wake_at = sample_window(
            day, self.settings.wake_start, self.settings.wake_end, tz, self.rng
        )
        sleep_at = sample_window(
            day, self.settings.sleep_start, self.settings.sleep_end, tz, self.rng
        )
        events: list[OfflineEvent] = []
        if self.settings.daytime_enabled:
            events.extend(self._daytime_events(bot_id, day, wake_at, sleep_at))
        return DailySchedule(
            bot_id=bot_id,
            schedule_date=day.isoformat(),
            timezone=self.settings.timezone,
            wake_at=wake_at,
            sleep_at=sleep_at,
            events=tuple(events),
        )

    def make_sleep_event(
        self, schedule: DailySchedule, next_wake_at: datetime
    ) -> OfflineEvent:
        reason = self.settings.night_reason
        monitor = self._monitor_text(self.rng.choice(reason.monitor_messages))
        return OfflineEvent(
            id=f"sleep-{schedule.bot_id}-{schedule.schedule_date}",
            bot_id=schedule.bot_id,
            schedule_date=schedule.schedule_date,
            event_type=OfflineEventType.NIGHT_SLEEP,
            reason_id=reason.id,
            pre_away_fact=reason.pre_away_fact,
            start_at=schedule.sleep_at,
            end_at=next_wake_at,
            fixed_monitor_text=monitor,
        )

    def _daytime_events(
        self, bot_id: str, day: date, wake_at: datetime, sleep_at: datetime
    ) -> list[OfflineEvent]:
        start = wake_at + timedelta(minutes=self.settings.pre_away_advance_minutes)
        end = sleep_at - timedelta(minutes=self.settings.pre_away_advance_minutes)
        available = max(0, int((end - start).total_seconds() // 60))
        if available < self.settings.segment_minutes_min:
            return []

        wanted = self.rng.randint(
            self.settings.total_minutes_min, self.settings.total_minutes_max
        )
        wanted = min(wanted, available)
        min_count = max(
            self.settings.segments_min,
            math.ceil(wanted / self.settings.segment_minutes_max),
        )
        max_count = min(
            self.settings.segments_max,
            wanted // self.settings.segment_minutes_min,
        )
        if min_count > max_count:
            max_feasible_total = min(
                available,
                self.settings.segments_max * self.settings.segment_minutes_max,
            )
            wanted = max(
                self.settings.segment_minutes_min,
                min(wanted, max_feasible_total),
            )
            min_count = max(1, math.ceil(wanted / self.settings.segment_minutes_max))
            max_count = max(1, min(self.settings.segments_max, wanted // self.settings.segment_minutes_min))
        count = self.rng.randint(min_count, max_count)
        wanted = max(
            count * self.settings.segment_minutes_min,
            min(wanted, count * self.settings.segment_minutes_max),
        )
        durations = _split_total(
            wanted,
            count,
            self.settings.segment_minutes_min,
            self.settings.segment_minutes_max,
            self.rng,
        )
        segments = _place_segments(start, end, durations, self.rng)
        events: list[OfflineEvent] = []
        for segment_start, segment_end in segments:
            reason = self.rng.choice(self.settings.daytime_reasons)
            monitor = self._monitor_text(self.rng.choice(reason.monitor_messages))
            events.append(
                OfflineEvent(
                    id=f"away-{bot_id}-{day.isoformat()}-{uuid4().hex[:12]}",
                    bot_id=bot_id,
                    schedule_date=day.isoformat(),
                    event_type=OfflineEventType.DAYTIME_AWAY,
                    reason_id=reason.id,
                    pre_away_fact=reason.pre_away_fact,
                    start_at=segment_start,
                    end_at=segment_end,
                    fixed_monitor_text=monitor,
                )
            )
        return events

    def _monitor_text(self, template: str) -> str:
        return template.replace("{bot_name}", self.settings.bot_name)


def sample_group_messages(messages: list, rate: float, fluctuation: float, rng=None) -> list:
    rng = rng or random.SystemRandom()
    count = len(messages)
    target = round(count * rate)
    delta = round(target * fluctuation)
    minimum = max(0, target - delta)
    maximum = min(count, target + delta)
    actual = rng.randint(minimum, maximum) if maximum >= minimum else minimum
    return rng.sample(messages, actual) if actual else []
