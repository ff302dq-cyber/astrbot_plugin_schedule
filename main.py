from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api import message_components as Comp
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.agent.message import TextPart

from .config import PluginSettings, load_settings, load_settings_for_bot
from .llm_service import LLMService
from .models import PresenceState
from .prompts import pre_away_prompt
from .repository import Repository
from .runtime import RuntimeService

PLUGIN_NAME = "astrbot_plugin_tiangan_schedule"


@register(
    PLUGIN_NAME,
    "菌菌",
    "随机作息、离线监测器、离线信箱与回归回复",
    "1.0.0",
    "",
)
class TianganSchedulePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.settings: PluginSettings = load_settings(config)
        self._settings_by_bot: dict[str, PluginSettings] = {}
        self.repository: Repository | None = None
        self.llm: LLMService | None = None
        self.runtime: RuntimeService | None = None
        self._runtimes: dict[str, RuntimeService] = {}
        self._ticker: asyncio.Task | None = None
        self._closing = False

    async def initialize(self) -> None:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        except Exception:  # noqa: BLE001 - 兼容 4.24 不同补丁版本的路径 API
            data_dir = Path(StarTools.get_data_dir()) / PLUGIN_NAME
        data_dir.mkdir(parents=True, exist_ok=True)
        self.repository = Repository(data_dir / "tiangan_schedule.sqlite3")
        self._ticker = asyncio.create_task(self._ticker_loop())
        logger.info(
            f"[小天干作息] 插件已加载，AstrBot>=4.24.0，数据库={data_dir / 'tiangan_schedule.sqlite3'}"
        )

    def _repo(self) -> Repository:
        if self.repository is None:
            raise RuntimeError("小天干作息数据库尚未初始化")
        return self.repository

    def _settings(self, bot_id: str) -> PluginSettings:
        if bot_id not in self._settings_by_bot:
            self._settings_by_bot[bot_id] = load_settings_for_bot(self.config, bot_id)
        return self._settings_by_bot[bot_id]

    def _runtime(self, bot_id: str) -> RuntimeService:
        # self.runtime 仅保留为测试和旧式单实例注入兼容口。
        if self.runtime is not None:
            return self.runtime
        if bot_id not in self._runtimes:
            if self.repository is None:
                raise RuntimeError("小天干作息运行服务尚未初始化")
            settings = self._settings(bot_id)
            llm = LLMService(self.context, settings, self.repository)
            self._runtimes[bot_id] = RuntimeService(
                bot_id,
                settings,
                self.repository,
                llm,
                self._send_text,
                self._send_quoted_reply,
            )
        return self._runtimes[bot_id]

    def _now(self, bot_id: str) -> datetime:
        return datetime.now(self._settings(bot_id).tz)

    def _message_time(self, event: AstrMessageEvent, bot_id: str) -> datetime:
        timestamp = int(getattr(event.message_obj, "timestamp", 0) or 0)
        return (
            datetime.fromtimestamp(timestamp, self._settings(bot_id).tz)
            if timestamp > 0
            else self._now(bot_id)
        )

    @staticmethod
    def _message_kind(event: AstrMessageEvent) -> str:
        return "group" if str(event.get_group_id() or "") else "private"

    @staticmethod
    def _message_id(event: AstrMessageEvent) -> str:
        value = str(getattr(event.message_obj, "message_id", "") or "").strip()
        if value:
            return value
        timestamp = str(getattr(event.message_obj, "timestamp", "0") or "0")
        return f"fallback-{event.get_sender_id()}-{timestamp}"

    @staticmethod
    def _component_summary(event: AstrMessageEvent) -> str:
        result = []
        for component in getattr(event.message_obj, "message", []) or []:
            item: dict[str, Any] = {"type": type(component).__name__}
            for key in ("id", "qq", "name", "file", "url"):
                value = getattr(component, key, None)
                if value not in (None, ""):
                    item[key] = str(value)[:500]
            result.append(item)
        return json.dumps(result, ensure_ascii=False)

    @staticmethod
    def _plain_text(event: AstrMessageEvent) -> str:
        text = str(getattr(event, "message_str", "") or "").strip()
        if text:
            return text
        names = [
            f"[{type(component).__name__}]"
            for component in (getattr(event.message_obj, "message", []) or [])
        ]
        return " ".join(names) or "[空消息]"

    async def _ticker_loop(self) -> None:
        while not self._closing:
            try:
                for bot_id in await self._repo().bot_ids():
                    now = self._now(bot_id)
                    runtime = self._runtime(bot_id)
                    await runtime.reconcile(bot_id, now)
                    await runtime.process_due_notices(now)
                    await runtime.process_due_replies(now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 后台循环隔离单次故障
                logger.error(f"[小天干作息] 后台轮询异常：{exc}", exc_info=True)
            await asyncio.sleep(self.settings.scheduler_interval_seconds)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10000)
    async def on_message(self, event: AstrMessageEvent):
        sender_id = str(event.get_sender_id() or "")
        bot_id = str(event.get_self_id() or "")
        if not bot_id or sender_id == bot_id:
            return
        settings = self._settings(bot_id)
        if not settings.enabled or self.repository is None:
            return

        now = self._now(bot_id)
        message_time = self._message_time(event, bot_id)
        platform = str(event.get_platform_name() or "")
        umo = str(event.unified_msg_origin)
        group_id = str(event.get_group_id() or "")
        kind = self._message_kind(event)
        message_id = self._message_id(event)
        text = self._plain_text(event)
        repo = self._repo()

        await repo.register_bot(bot_id, platform, now)
        await repo.touch_session(umo, bot_id, kind, group_id, now)
        state = await self._runtime(bot_id).reconcile(bot_id, now)
        if kind == "private" and state == PresenceState.PRE_AWAY:
            await self._runtime(bot_id).refresh_pre_away_session(bot_id, umo, now)

        is_offline_wake = state in {
            PresenceState.AWAY,
            PresenceState.SLEEPING,
            PresenceState.RETURNING,
        } and bool(getattr(event, "is_at_or_wake_command", False))
        if kind == "group" and not is_offline_wake:
            await repo.save_group_context(
                umo,
                sender_id,
                str(event.get_sender_name() or sender_id),
                message_id,
                message_time,
                text,
            )

        if not is_offline_wake:
            return

        runtime = await repo.get_runtime(bot_id)
        if not runtime or not runtime.current_event_id:
            return
        offline_event = await repo.get_event(runtime.current_event_id)
        if not offline_event:
            return

        await repo.save_mailbox(
            offline_event.id,
            umo,
            kind,
            group_id,
            sender_id,
            str(event.get_sender_name() or sender_id),
            message_id,
            message_time,
            text,
            self._component_summary(event),
        )
        try:
            await event.send(event.plain_result(offline_event.fixed_monitor_text))
        except Exception as exc:  # noqa: BLE001 - 发送失败仍必须拦截本次 LLM
            logger.error(f"[小天干作息] 监测器提示发送失败：{exc}", exc_info=True)
        event.should_call_llm(False)
        event.stop_event()

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        if self._message_kind(event) != "private":
            return
        bot_id = str(event.get_self_id() or "")
        if not bot_id:
            return
        if not self._settings(bot_id).enabled or self.repository is None:
            return
        now = self._now(bot_id)
        pre_away = await self._runtime(bot_id).refresh_pre_away_session(
            bot_id, str(event.unified_msg_origin), now
        )
        if not pre_away:
            return
        umo = str(event.unified_msg_origin)
        if not await self._repo().claim_notice(pre_away.id, umo):
            return
        req.extra_user_content_parts.append(
            TextPart(text=pre_away_prompt(pre_away)).mark_as_temp()
        )
        event.set_extra(
            "tiangan_pre_away_notice",
            {"event_id": pre_away.id, "umo": umo},
        )

    @filter.on_llm_response()
    async def on_llm_response(
        self, event: AstrMessageEvent, resp: LLMResponse
    ) -> None:
        marker = event.get_extra("tiangan_pre_away_notice")
        if not marker or str(getattr(resp, "completion_text", "") or "").strip():
            return
        await self._repo().release_notice(marker["event_id"], marker["umo"])

    @filter.after_message_sent(priority=1)
    async def after_message_sent(self, event: AstrMessageEvent, *_args, **_kwargs) -> None:
        if self.repository is None:
            return
        marker = event.get_extra("tiangan_pre_away_notice")
        if not marker:
            return
        text = ""
        result = event.get_result()
        if result:
            try:
                text = str(result.get_plain_text() or "").strip()
            except Exception:  # noqa: BLE001 - 兼容不同结果组件实现
                text = ""
        await self._repo().mark_notice_sent(
            marker["event_id"], marker["umo"], text, self._now(str(event.get_self_id()))
        )

    async def _send_text(self, umo: str, text: str) -> None:
        await self.context.send_message(umo, MessageChain().message(text))

    async def _send_quoted_reply(
        self, umo: str, quote_message_id: str, sender_id: str, text: str
    ) -> None:
        chain = MessageChain(
            [
                Comp.Reply(id=quote_message_id),
                Comp.At(qq=sender_id),
                Comp.Plain(text),
            ]
        )
        await self.context.send_message(umo, chain)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("作息状态")
    async def schedule_status(self, event: AstrMessageEvent):
        bot_id = str(event.get_self_id() or "")
        now = self._now(bot_id)
        state = await self._runtime(bot_id).reconcile(bot_id, now)
        runtime = await self._repo().get_runtime(bot_id)
        detail = ""
        if runtime and runtime.current_event_id:
            current = await self._repo().get_event(runtime.current_event_id)
            if current:
                detail = (
                    f"\n当前事件：{current.reason_id}"
                    f"\n开始：{current.start_at:%Y-%m-%d %H:%M:%S}"
                    f"\n结束：{current.end_at:%Y-%m-%d %H:%M:%S}"
                )
        yield event.plain_result(f"当前状态：{state.value}{detail}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("今日作息")
    async def today_schedule(self, event: AstrMessageEvent):
        bot_id = str(event.get_self_id() or "")
        now = self._now(bot_id)
        await self._runtime(bot_id).ensure_calendar(bot_id, now)
        schedule = await self._repo().get_schedule(bot_id, now.date().isoformat())
        if not schedule:
            yield event.plain_result("今日作息尚未生成。")
            return
        lines = [
            f"日期：{schedule.schedule_date}",
            f"起床：{schedule.wake_at:%H:%M:%S}",
            f"睡觉：{schedule.sleep_at:%Y-%m-%d %H:%M:%S}",
        ]
        for item in schedule.events:
            if item.event_type.value == "daytime_away":
                lines.append(
                    f"离开：{item.start_at:%H:%M:%S}～{item.end_at:%H:%M:%S}（{item.reason_id}）"
                )
        yield event.plain_result("\n".join(lines))

    async def terminate(self) -> None:
        self._closing = True
        if self._ticker:
            self._ticker.cancel()
            await asyncio.gather(self._ticker, return_exceptions=True)
        if self.runtime:
            await self.runtime.shutdown()
        for runtime in self._runtimes.values():
            await runtime.shutdown()
        if self.repository:
            await self.repository.close()
        logger.info("[小天干作息] 插件已卸载")
