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

from .config import PluginSettings, load_settings
from .llm_service import LLMService
from .models import OfflineEvent, OfflineEventType, PresenceState
from .prompts import pre_away_continuity_prompt, pre_away_prompt
from .repository import Repository
from .runtime import RuntimeService

PLUGIN_NAME = "astrbot_plugin_tiangan_schedule"


@register(
    PLUGIN_NAME,
    "菌菌",
    "随机作息、离线监测器、离线信箱与回归回复",
    "1.0.15",
    "https://github.com/ff302dq-cyber/astrbot_plugin_schedule",
)
class TianganSchedulePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.settings: PluginSettings = load_settings(config)
        self.repository: Repository | None = None
        self.llm: LLMService | None = None
        self.runtime: RuntimeService | None = None
        self._runtimes: dict[str, RuntimeService] = {}
        self._ticker: asyncio.Task | None = None
        self._closing = False
        self._bot_display_names: dict[str, str] = {}

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
            f"[角色作息] 插件已加载，AstrBot>=4.24.0，数据库={data_dir / 'tiangan_schedule.sqlite3'}"
        )

    def _repo(self) -> Repository:
        if self.repository is None:
            raise RuntimeError("角色作息数据库尚未初始化")
        return self.repository

    def _settings(self, bot_id: str) -> PluginSettings:
        return self.settings

    def _runtime(self, bot_id: str) -> RuntimeService:
        # self.runtime 仅保留为测试和旧式单实例注入兼容口。
        if self.runtime is not None:
            return self.runtime
        if bot_id not in self._runtimes:
            if self.repository is None:
                raise RuntimeError("角色作息运行服务尚未初始化")
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

    @staticmethod
    def _has_message_content(event: AstrMessageEvent) -> bool:
        return bool(
            str(getattr(event, "message_str", "") or "").strip()
            or (getattr(event.message_obj, "message", []) or [])
        )

    async def _bot_display_name(
        self, event: AstrMessageEvent, bot_id: str
    ) -> str:
        configured = self._settings(bot_id).bot_name
        if configured:
            return configured
        if cached := self._bot_display_names.get(bot_id):
            return cached

        for attr in ("self_name", "bot_name"):
            value = str(getattr(event.message_obj, attr, "") or "").strip()
            if value:
                self._bot_display_names[bot_id] = value
                return value

        getter = getattr(event, "get_self_name", None)
        if callable(getter):
            try:
                value = str(getter() or "").strip()
                if value:
                    self._bot_display_names[bot_id] = value
                    return value
            except Exception as exc:  # noqa: BLE001 - 兼容不同平台事件实现
                logger.debug(f"[角色作息] 从事件读取 Bot 昵称失败：{exc}")

        bot = getattr(event, "bot", None)
        api = getattr(bot, "api", None)
        call_action = getattr(api, "call_action", None)
        if callable(call_action):
            try:
                result = await call_action("get_login_info")
                data: Any = result
                if isinstance(result, dict):
                    data = result.get("data", result)
                elif getattr(result, "data", None) is not None:
                    data = result.data
                if isinstance(data, dict):
                    value = str(data.get("nickname", "") or "").strip()
                    if value:
                        self._bot_display_names[bot_id] = value
                        return value
            except Exception as exc:  # noqa: BLE001 - 昵称读取失败不影响离线拦截
                logger.warning(f"[角色作息] 读取 Bot 昵称失败：{exc}")

        fallback = "Bot"
        self._bot_display_names[bot_id] = fallback
        return fallback

    def _monitor_templates(self, event: OfflineEvent, bot_id: str) -> tuple[str, ...]:
        settings = self._settings(bot_id)
        if event.event_type == OfflineEventType.NIGHT_SLEEP:
            return settings.night_reason.monitor_messages
        reason = next(
            (item for item in settings.daytime_reasons if item.id == event.reason_id),
            None,
        )
        return reason.monitor_messages if reason else ()

    async def _render_monitor_text(
        self, event: AstrMessageEvent, offline_event: OfflineEvent, bot_id: str
    ) -> str:
        text = offline_event.fixed_monitor_text
        name = await self._bot_display_name(event, bot_id)
        if "{bot_name}" in text:
            return text.replace("{bot_name}", name)

        # 兼容旧版本已经生成进 SQLite 的日程：当时角色名称留空会提前把
        # 占位符替换为空。只在能与当前配置模板精确反向匹配时恢复名称。
        if not self._settings(bot_id).bot_name:
            for template in self._monitor_templates(offline_event, bot_id):
                if "{bot_name}" in template and template.replace("{bot_name}", "") == text:
                    return template.replace("{bot_name}", name)
        return text

    @staticmethod
    def _directly_mentions_bot(event: AstrMessageEvent, bot_id: str) -> bool:
        """Only a real At component aimed at this bot counts; Reply never does."""
        for component in getattr(event.message_obj, "message", []) or []:
            if isinstance(component, Comp.At) and str(
                getattr(component, "qq", "") or ""
            ) == str(bot_id):
                return True
        return False

    @staticmethod
    def _astrbot_wake_prefix_was_used(event: AstrMessageEvent) -> bool:
        """Use AstrBot's preprocessing result instead of duplicating its prefix config."""
        if not bool(getattr(event, "is_at_or_wake_command", False)):
            return False
        try:
            original = event.get_extra("astrbot_original_message_str")
        except Exception:  # noqa: BLE001 - 兼容缺少 extras 的适配器
            original = None
        current = str(getattr(event, "message_str", "") or "").strip()
        if original is not None:
            return str(original).strip() != current

        # 兼容尚未写入 original extra 的 4.24 补丁版本：AstrBot 会从
        # message_str 剥掉实际配置的唤醒前缀，但不会改原始 Plain 消息段。
        original_plain = "".join(
            str(getattr(component, "text", "") or "")
            for component in (getattr(event.message_obj, "message", []) or [])
            if isinstance(component, Comp.Plain)
        ).strip()
        return bool(current and original_plain != current and original_plain.endswith(current))

    @classmethod
    def _is_explicit_group_wake(
        cls, event: AstrMessageEvent, bot_id: str
    ) -> bool:
        # AstrBot 的广义 wake 标志可能包含“仅引用 Bot 消息”；这里必须再收窄。
        if cls._directly_mentions_bot(event, bot_id):
            return True
        if cls._astrbot_wake_prefix_was_used(event):
            return True
        components = getattr(event.message_obj, "message", []) or []
        if any(isinstance(item, (Comp.Reply, Comp.At)) for item in components):
            return False
        # 老版本没有 original extra 时，纯文本消息的 broad wake 只能来自
        # AstrBot 自己已经识别的唤醒词；插件不猜测也不写死具体前缀。
        return bool(getattr(event, "is_at_or_wake_command", False))

    def _is_allowed_offline_command(
        self, event: AstrMessageEvent, bot_id: str
    ) -> bool:
        # 私聊遵循 AstrBot 的 friend_message_needs_wake_prefix 判定：关闭时，
        # 直接发送指令名称就是有效指令；开启时仍需 AstrBot 先认定为唤醒。
        # 群聊则必须使用当前实例的实际唤醒词，插件不写死 /、+ 等前缀。
        kind = self._message_kind(event)
        astrbot_wake = bool(getattr(event, "is_at_or_wake_command", False))
        if kind == "private":
            if not astrbot_wake:
                return False
        elif not self._astrbot_wake_prefix_was_used(event):
            return False
        text = " ".join(str(getattr(event, "message_str", "") or "").strip().split())
        for command in self._settings(bot_id).offline_allowed_commands:
            if text == command or text.startswith(f"{command} "):
                return True
        return False

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
                logger.error(f"[角色作息] 后台轮询异常：{exc}", exc_info=True)
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

        is_offline = state in {
            PresenceState.AWAY,
            PresenceState.SLEEPING,
            PresenceState.RETURNING,
        }
        if is_offline and self._is_allowed_offline_command(event, bot_id):
            event.set_extra("tiangan_offline_command_allowed", True)
            return
        if kind == "group":
            is_offline_wake = is_offline and self._is_explicit_group_wake(
                event, bot_id
            )
        else:
            is_offline_wake = is_offline and bool(
                getattr(event, "is_at_or_wake_command", False)
            )
        if (
            kind == "group"
            and not is_offline_wake
            and self._has_message_content(event)
        ):
            await repo.save_group_context(
                umo,
                sender_id,
                str(event.get_sender_name() or sender_id),
                message_id,
                message_time,
                text,
            )

        if is_offline and not is_offline_wake:
            # 真正离线时，所有未明确唤醒的事件都在最高优先级静默终止。
            # 这会覆盖戳一戳等通知事件，阻止低优先级插件直接调用 LLM。
            event.should_call_llm(False)
            event.stop_event()
            return

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
        # 私聊同一离线事件只提示一次，后续消息仍进入信箱并继续阻止 LLM。
        # 群聊只有明确 @ 当前 Bot 或使用 AstrBot 唤醒词时才到这里。
        should_send_monitor = kind == "group" or await repo.claim_offline_monitor(
            offline_event.id, umo
        )
        if should_send_monitor:
            try:
                monitor_text = await self._render_monitor_text(
                    event, offline_event, bot_id
                )
                await event.send(event.plain_result(monitor_text))
                if kind == "private":
                    await repo.mark_offline_monitor_sent(
                        offline_event.id, umo, now
                    )
            except Exception as exc:  # noqa: BLE001 - 发送失败仍必须拦截本次 LLM
                if kind == "private":
                    await repo.release_offline_monitor(offline_event.id, umo)
                logger.error(f"[角色作息] 监测器提示发送失败：{exc}", exc_info=True)
        event.should_call_llm(False)
        event.stop_event()

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        bot_id = str(event.get_self_id() or "")
        if not bot_id:
            return
        if not self._settings(bot_id).enabled or self.repository is None:
            return
        now = self._now(bot_id)
        state = await self._runtime(bot_id).reconcile(bot_id, now)
        if event.get_extra("tiangan_offline_command_allowed"):
            return
        if state in {
            PresenceState.AWAY,
            PresenceState.SLEEPING,
            PresenceState.RETURNING,
        }:
            event.should_call_llm(False)
            event.stop_event()
            return
        if self._message_kind(event) != "private":
            return
        pre_away = await self._runtime(bot_id).refresh_pre_away_session(
            bot_id, str(event.unified_msg_origin), now
        )
        if not pre_away:
            return
        umo = str(event.unified_msg_origin)
        if not await self._repo().claim_notice(pre_away.id, umo):
            req.extra_user_content_parts.append(
                TextPart(text=pre_away_continuity_prompt(pre_away)).mark_as_temp()
            )
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

    @filter.command("作息状态")
    async def schedule_status(self, event: AstrMessageEvent):
        bot_id = str(event.get_self_id() or "")
        now = self._now(bot_id)
        state = await self._runtime(bot_id).reconcile(bot_id, now)
        label = {
            PresenceState.ONLINE: "在线",
            PresenceState.PRE_AWAY: "在线",
            PresenceState.AWAY: "暂时离开",
            PresenceState.SLEEPING: "睡觉",
            PresenceState.RETURNING: "在线",
        }.get(state, "在线")
        yield event.plain_result(f"当前状态：{label}")

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
        if self.settings.daytime_reasons_error:
            lines.extend(
                [
                    "",
                    "⚠ 白天离开原因 JSON 配置错误",
                    self.settings.daytime_reasons_error,
                    "请在插件配置中修正“白天离开原因 JSON”，保存并重载插件。",
                ]
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
        logger.info("[角色作息] 插件已卸载")
