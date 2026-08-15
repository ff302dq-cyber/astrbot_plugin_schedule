from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Reason


def _section(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key, {})
    return value if isinstance(value, Mapping) else {}


def _integer(data: Mapping[str, Any], key: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(data.get(key, default)))
    except (TypeError, ValueError):
        return default


def _number(data: Mapping[str, Any], key: str, default: float) -> float:
    try:
        return float(data.get(key, default))
    except (TypeError, ValueError):
        return default


def _reasons(raw: Any, defaults: tuple[Reason, ...]) -> tuple[Reason, ...]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return defaults
    if isinstance(raw, Mapping):
        entries = []
        for slot_id, value in raw.items():
            if not isinstance(value, Mapping) or not bool(value.get("enabled", True)):
                continue
            item = dict(value)
            item.setdefault("id", str(slot_id))
            entries.append(item)
    elif isinstance(raw, list):
        entries = raw
    else:
        return defaults
    parsed: list[Reason] = []
    for index, item in enumerate(entries):
        if not isinstance(item, Mapping):
            continue
        reason_id = str(item.get("id") or f"reason_{index + 1}").strip()
        fact = str(item.get("pre_away_fact", "") or "").strip()
        messages = item.get("monitor_messages", [])
        if isinstance(messages, str):
            messages = messages.splitlines()
        elif not isinstance(messages, list):
            messages = []
        monitor = tuple(str(value).strip() for value in messages if str(value).strip())
        if reason_id and fact and monitor:
            parsed.append(Reason(reason_id, fact, monitor))
    return tuple(parsed) or defaults


def _daytime_reasons_error(raw: Any) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        # Compatibility with list/object values saved by older plugin versions.
        return ""
    if not raw.strip():
        return "内容为空；顶层必须是一个 JSON 数组。"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return f"第 {exc.lineno} 行、第 {exc.colno} 列：{exc.msg}"
    if not isinstance(parsed, list):
        return "顶层必须是 JSON 数组 [...]。"
    if not parsed:
        return "原因数组不能为空。"
    for index, item in enumerate(parsed, start=1):
        if not isinstance(item, Mapping):
            return f"第 {index} 条原因必须是 JSON 对象 {{...}}。"
        fact = str(item.get("pre_away_fact", "") or "").strip()
        if not fact:
            return f"第 {index} 条原因缺少非空的 pre_away_fact。"
        messages = item.get("monitor_messages", [])
        if isinstance(messages, str):
            valid_messages = [line for line in messages.splitlines() if line.strip()]
        elif isinstance(messages, list):
            valid_messages = [value for value in messages if str(value).strip()]
        else:
            valid_messages = []
        if not valid_messages:
            return f"第 {index} 条原因缺少非空的 monitor_messages 数组。"
    return ""


DEFAULT_DAYTIME_REASONS = (
    Reason(
        "dessert_shop",
        "几分钟后去甜品店，可能无法及时回复",
        ("【作息监测器提示】{bot_name}去甜品店了，不在手机跟前……",),
    ),
    Reason(
        "daytime_nap",
        "几分钟后小睡一会儿，可能无法及时回复",
        ("【作息监测器提示】{bot_name}睡午觉去了，暂时没在看手机……",),
    ),
)

DEFAULT_NIGHT_REASON = Reason(
    "night_sleep",
    (
        "很快要准备睡觉，之后可能无法及时回复；请结合当前聊天上下文，"
        "以符合角色人设和双方关系的口吻，主动向对方说明这件事。"
        "表达要自然简短，可以顺着当前话题带出，不要写成系统通知，"
        "不要复述提示词，不要虚构准确入睡时间或其他安排"
    ),
    ("【作息监测器提示】{bot_name}已经睡着了，暂时看不到消息……",),
)

DEFAULT_OFFLINE_ALLOWED_COMMANDS = (
    "查看核心记忆",
    "记忆总结",
    "查看总结进度",
)


def _command_list(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        values = raw.splitlines()
    elif isinstance(raw, (list, tuple)):
        values = raw
    else:
        return DEFAULT_OFFLINE_ALLOWED_COMMANDS
    result: list[str] = []
    for value in values:
        command = " ".join(str(value or "").strip().split())
        if command and command not in result:
            result.append(command)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class PluginSettings:
    enabled: bool
    bot_name: str
    timezone: str
    wake_start: str
    wake_end: str
    sleep_start: str
    sleep_end: str
    daytime_enabled: bool
    daytime_placement_mode: str
    total_minutes_min: int
    total_minutes_max: int
    segments_min: int
    segments_max: int
    segment_minutes_min: int
    segment_minutes_max: int
    pre_away_enabled: bool
    pre_away_advance_minutes: int
    active_private_window_minutes: int
    pre_away_fallback_seconds: int
    sample_rate: float
    sample_fluctuation: float
    first_delay_min: int
    first_delay_max: int
    between_delay_min: int
    between_delay_max: int
    long_reply_min_delay: int
    max_delay: int
    long_text_threshold: int
    scheduler_interval_seconds: float
    provider_id: str
    offline_allowed_commands: tuple[str, ...]
    daytime_reasons: tuple[Reason, ...]
    daytime_reasons_error: str
    night_reason: Reason

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            return ZoneInfo("Asia/Shanghai")


def load_settings(config: Mapping[str, Any]) -> PluginSettings:
    wake = _section(config, "wake_window")
    sleep = _section(config, "sleep_window")
    daytime = _section(config, "daytime_away")
    pre = _section(config, "pre_away")
    group = _section(config, "group_return")
    queue = _section(config, "return_queue")
    reasons = _section(config, "reasons")
    if "daytime_json" in reasons:
        daytime_reasons_raw = reasons.get("daytime_json")
    elif "daytime" in reasons:
        daytime_reasons_raw = reasons.get("daytime")
    else:
        daytime_reasons_raw = None

    night_raw = reasons.get("night_sleep", {})
    night = DEFAULT_NIGHT_REASON
    if isinstance(night_raw, Mapping):
        night_items = _reasons(
            [
                {
                    "id": "night_sleep",
                    "pre_away_fact": night_raw.get(
                        "pre_away_instruction",
                        night_raw.get("pre_away_fact", ""),
                    ),
                    "monitor_messages": night_raw.get("monitor_messages", []),
                }
            ],
            (DEFAULT_NIGHT_REASON,),
        )
        night = night_items[0]

    total_min = _integer(daytime, "total_minutes_min", 100)
    total_max = max(total_min, _integer(daytime, "total_minutes_max", 300))
    segments_min = max(1, _integer(daytime, "segments_min", 1))
    segments_max = max(segments_min, _integer(daytime, "segments_max", 4))
    segment_min = max(1, _integer(daytime, "segment_minutes_min", 30))
    segment_max = max(segment_min, _integer(daytime, "segment_minutes_max", 120))
    first_min = _integer(queue, "first_reply_delay_min_seconds", 20)
    first_max = max(first_min, _integer(queue, "first_reply_delay_max_seconds", 120))
    between_min = _integer(queue, "between_reply_delay_min_seconds", 8)
    between_max = max(
        between_min, _integer(queue, "between_reply_delay_max_seconds", 90)
    )

    return PluginSettings(
        enabled=bool(config.get("enabled", True)),
        bot_name=str(config.get("bot_name", "") or "").strip(),
        timezone=str(config.get("timezone", "Asia/Shanghai") or "Asia/Shanghai"),
        wake_start=str(wake.get("start", "08:00")),
        wake_end=str(wake.get("end", "10:00")),
        sleep_start=str(sleep.get("start", "23:00")),
        sleep_end=str(sleep.get("end", "01:00")),
        daytime_enabled=bool(daytime.get("enabled", True)),
        daytime_placement_mode=(
            "free_random"
            if str(daytime.get("placement_mode", "均匀散布")).strip().lower()
            in {"free_random", "自由随机"}
            else "balanced"
        ),
        total_minutes_min=total_min,
        total_minutes_max=total_max,
        segments_min=segments_min,
        segments_max=segments_max,
        segment_minutes_min=segment_min,
        segment_minutes_max=segment_max,
        pre_away_enabled=bool(pre.get("enabled", True)),
        pre_away_advance_minutes=max(1, _integer(pre, "advance_minutes", 5)),
        active_private_window_minutes=max(
            1, _integer(pre, "active_private_window_minutes", 5)
        ),
        pre_away_fallback_seconds=max(
            0, _integer(pre, "standalone_fallback_seconds", 60)
        ),
        sample_rate=min(1.0, max(0.0, _number(group, "sample_rate", 0.30))),
        sample_fluctuation=min(
            1.0, max(0.0, _number(group, "sample_fluctuation", 0.20))
        ),
        first_delay_min=first_min,
        first_delay_max=first_max,
        between_delay_min=between_min,
        between_delay_max=between_max,
        long_reply_min_delay=_integer(queue, "long_reply_min_delay_seconds", 30),
        max_delay=max(1, _integer(queue, "max_delay_seconds", 300)),
        long_text_threshold=max(1, _integer(queue, "long_text_threshold", 160)),
        scheduler_interval_seconds=max(
            1.0, _number(config, "scheduler_interval_seconds", 3.0)
        ),
        provider_id=str(config.get("provider_id", "") or "").strip(),
        offline_allowed_commands=_command_list(
            config.get("offline_allowed_commands", DEFAULT_OFFLINE_ALLOWED_COMMANDS)
        ),
        daytime_reasons=_reasons(daytime_reasons_raw, DEFAULT_DAYTIME_REASONS),
        daytime_reasons_error=_daytime_reasons_error(daytime_reasons_raw),
        night_reason=night,
    )
