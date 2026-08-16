import asyncio

from astrbot_plugin_tiangan_schedule.availability import (
    ScheduleAvailability,
    query_schedule_availability,
    register_availability_provider,
    unregister_availability_provider,
)


def test_availability_provider_registers_and_unregisters():
    async def run():
        async def provider(bot_id):
            return ScheduleAvailability(
                bot_id=bot_id or "bot-1",
                state="ONLINE",
                can_send_proactive=True,
            )

        token = register_availability_provider(provider)
        snapshot = await query_schedule_availability("bot-1")
        assert snapshot is not None
        assert snapshot.can_send_proactive is True
        unregister_availability_provider(token)
        assert await query_schedule_availability("bot-1") is None

    asyncio.run(run())
