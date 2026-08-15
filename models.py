from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class PresenceState(StrEnum):
    ONLINE = "ONLINE"
    PRE_AWAY = "PRE_AWAY"
    AWAY = "AWAY"
    SLEEPING = "SLEEPING"
    RETURNING = "RETURNING"


class OfflineEventType(StrEnum):
    DAYTIME_AWAY = "daytime_away"
    NIGHT_SLEEP = "night_sleep"


class QueueState(StrEnum):
    PENDING = "PENDING"
    SELECTED = "SELECTED"
    GENERATED = "GENERATED"
    SENT = "SENT"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class Reason:
    id: str
    pre_away_fact: str
    monitor_messages: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OfflineEvent:
    id: str
    bot_id: str
    schedule_date: str
    event_type: OfflineEventType
    reason_id: str
    pre_away_fact: str
    start_at: datetime
    end_at: datetime
    fixed_monitor_text: str
    state: str = "PLANNED"


@dataclass(frozen=True, slots=True)
class DailySchedule:
    bot_id: str
    schedule_date: str
    timezone: str
    wake_at: datetime
    sleep_at: datetime
    events: tuple[OfflineEvent, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class MailboxMessage:
    id: int
    offline_event_id: str
    umo: str
    message_kind: str
    group_id: str
    sender_id: str
    sender_name: str
    message_id: str
    timestamp: datetime
    plain_text: str
    components_json: str
    selection_state: QueueState


@dataclass(frozen=True, slots=True)
class ReturnReply:
    id: int
    offline_event_id: str
    umo: str
    message_kind: str
    sender_id: str
    source_message_ids: tuple[str, ...]
    quote_message_id: str
    generated_text: str
    send_state: QueueState
    scheduled_send_at: datetime


@dataclass(frozen=True, slots=True)
class RuntimePresence:
    bot_id: str
    state: PresenceState
    current_event_id: str | None
    updated_at: datetime
