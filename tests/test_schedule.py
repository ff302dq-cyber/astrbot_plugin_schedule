import random
from datetime import date, datetime
from zoneinfo import ZoneInfo

from astrbot_plugin_tiangan_schedule.config import load_settings
from astrbot_plugin_tiangan_schedule.schedule import (
    ScheduleGenerator,
    sample_group_messages,
    sample_window,
)

TZ = ZoneInfo("Asia/Shanghai")


def test_cross_midnight_window():
    value = sample_window(date(2026, 8, 15), "23:00", "01:00", TZ, random.Random(7))
    assert datetime(2026, 8, 15, 23, tzinfo=TZ) <= value
    assert value <= datetime(2026, 8, 16, 1, tzinfo=TZ)


def test_schedule_segments_are_valid_and_non_overlapping():
    settings = load_settings({})
    schedule = ScheduleGenerator(settings, random.Random(42)).generate_day(
        "bot-1", date(2026, 8, 15)
    )
    events = sorted(schedule.events, key=lambda item: item.start_at)
    total = 0
    for index, event in enumerate(events):
        duration = int((event.end_at - event.start_at).total_seconds() // 60)
        assert settings.segment_minutes_min <= duration <= settings.segment_minutes_max
        total += duration
        if index:
            assert events[index - 1].end_at <= event.start_at
    assert settings.total_minutes_min <= total <= settings.total_minutes_max


def test_both_placement_modes_produce_valid_non_overlapping_segments():
    for mode in ("均匀散布", "自由随机"):
        settings = load_settings({"daytime_away": {"placement_mode": mode}})
        for seed in range(20):
            schedule = ScheduleGenerator(settings, random.Random(seed)).generate_day(
                "bot-1", date(2026, 8, 15)
            )
            events = sorted(schedule.events, key=lambda item: item.start_at)
            for index, event in enumerate(events):
                assert schedule.wake_at <= event.start_at < event.end_at
                assert event.end_at <= schedule.sleep_at
                if index:
                    assert events[index - 1].end_at <= event.start_at


def test_sleep_event_ends_at_next_day_wake():
    settings = load_settings({})
    generator = ScheduleGenerator(settings, random.Random(11))
    today = generator.generate_day("bot-1", date(2026, 8, 15))
    tomorrow = generator.generate_day("bot-1", date(2026, 8, 16))
    sleep = generator.make_sleep_event(today, tomorrow.wake_at)
    assert sleep.start_at == today.sleep_at
    assert sleep.end_at == tomorrow.wake_at
    assert sleep.end_at > sleep.start_at


def test_empty_configured_name_keeps_placeholder_for_runtime_bot_name():
    settings = load_settings({})
    generator = ScheduleGenerator(settings, random.Random(11))
    today = generator.generate_day("bot-1", date(2026, 8, 15))
    tomorrow = generator.generate_day("bot-1", date(2026, 8, 16))
    sleep = generator.make_sleep_event(today, tomorrow.wake_at)
    assert "{bot_name}" in sleep.fixed_monitor_text


def test_configured_name_is_baked_into_monitor_text():
    settings = load_settings({"bot_name": "王bot"})
    generator = ScheduleGenerator(settings, random.Random(11))
    today = generator.generate_day("bot-1", date(2026, 8, 15))
    tomorrow = generator.generate_day("bot-1", date(2026, 8, 16))
    sleep = generator.make_sleep_event(today, tomorrow.wake_at)
    assert "王bot" in sleep.fixed_monitor_text
    assert "{bot_name}" not in sleep.fixed_monitor_text


def test_group_sampling_100_messages_is_between_24_and_36():
    messages = list(range(100))
    for seed in range(100):
        result = sample_group_messages(messages, 0.30, 0.20, random.Random(seed))
        assert 24 <= len(result) <= 36
        assert len(result) == len(set(result))


def test_zero_sample_never_selects_messages():
    assert sample_group_messages(list(range(10)), 0.0, 0.2, random.Random(1)) == []


def test_invalid_reason_json_generates_no_daytime_events():
    settings = load_settings({"reasons": {"daytime_json": "[invalid"}})
    schedule = ScheduleGenerator(settings, random.Random(42)).generate_day(
        "bot-1", date(2026, 8, 15)
    )
    assert settings.daytime_reasons_error
    assert schedule.events == ()
