from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ScheduleAvailability:
    bot_id: str
    state: str
    can_send_proactive: bool
    next_online_at: datetime | None = None


AvailabilityProvider = Callable[
    [str | None], Awaitable[ScheduleAvailability | None]
]

_provider: AvailabilityProvider | None = None
_provider_token: object | None = None


def register_availability_provider(provider: AvailabilityProvider) -> object:
    global _provider, _provider_token
    token = object()
    _provider = provider
    _provider_token = token
    return token


def unregister_availability_provider(token: object | None) -> None:
    global _provider, _provider_token
    if token is not None and token is _provider_token:
        _provider = None
        _provider_token = None


async def query_schedule_availability(
    bot_id: str | None,
) -> ScheduleAvailability | None:
    provider = _provider
    if provider is None:
        return None
    return await provider(bot_id)
