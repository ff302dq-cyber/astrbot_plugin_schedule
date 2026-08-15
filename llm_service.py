from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from astrbot.api import logger

from .config import PluginSettings
from .models import MailboxMessage, OfflineEvent
from .prompts import (
    group_return_prompt,
    private_return_prompt,
    standalone_pre_away_prompt,
)
from .repository import Repository


class LLMService:
    def __init__(self, context: Any, settings: PluginSettings, repository: Repository):
        self.context = context
        self.settings = settings
        self.repository = repository

    async def _provider_id(self, umo: str) -> str:
        if self.settings.provider_id:
            try:
                if self.context.get_provider_by_id(self.settings.provider_id):
                    return self.settings.provider_id
            except Exception:  # noqa: BLE001 - 小版本 Provider 注册表异常类型不固定
                logger.warning("[角色作息] 配置的 Provider 不可用，回退当前会话模型")
        provider_id = await self.context.get_current_chat_provider_id(umo=umo)
        if not provider_id:
            raise RuntimeError(f"会话 {umo} 没有可用的聊天模型")
        return str(provider_id)

    async def _persona_prompt(self, umo: str) -> str:
        try:
            persona = await self.context.persona_manager.get_default_persona_v3(umo=umo)
            if isinstance(persona, dict):
                return str(persona.get("prompt", "") or "")
        except Exception as exc:  # noqa: BLE001 - 人格缺失时允许回退
            logger.warning(f"[角色作息] 获取会话人格失败：{exc}")
        return ""

    async def _contexts(self, umo: str) -> list[dict]:
        try:
            manager = self.context.conversation_manager
            cid = await manager.get_curr_conversation_id(umo)
            if not cid:
                return []
            conversation = await manager.get_conversation(umo, cid)
            history = json.loads(conversation.history or "[]") if conversation else []
            return history if isinstance(history, list) else []
        except Exception as exc:  # noqa: BLE001 - 历史损坏时仍应能够回归
            logger.warning(f"[角色作息] 读取会话历史失败：{exc}")
            return []

    async def _generate(self, umo: str, prompt: str, include_history: bool = True) -> str:
        kwargs: dict[str, Any] = {
            "chat_provider_id": await self._provider_id(umo),
            "prompt": prompt,
        }
        persona = await self._persona_prompt(umo)
        if persona:
            kwargs["system_prompt"] = persona
        if include_history:
            contexts = await self._contexts(umo)
            if contexts:
                kwargs["contexts"] = contexts
        response = await self.context.llm_generate(**kwargs)
        return str(getattr(response, "completion_text", "") or "").strip()

    async def pre_away(self, event: OfflineEvent, umo: str) -> str:
        return await self._generate(umo, standalone_pre_away_prompt(event))

    async def private_return(
        self, event: OfflineEvent, umo: str, messages: list[MailboxMessage]
    ) -> str:
        return await self._generate(umo, private_return_prompt(event, messages))

    async def group_return(
        self, event: OfflineEvent, umo: str, messages: list[MailboxMessage]
    ) -> dict[str, str]:
        context = await self.repository.recent_group_context(umo)
        raw = await self._generate(
            umo,
            group_return_prompt(event, messages, context),
            include_history=True,
        )
        parsed = self._parse_group_json(raw)
        expected = {item.sender_id for item in messages}
        return {sender: text for sender, text in parsed.items() if sender in expected and text}

    async def record_private_return(
        self, umo: str, messages: list[MailboxMessage], reply_text: str
    ) -> None:
        """把实际发出的私聊回归写回原会话，保持下一轮上下文连续。"""
        try:
            from astrbot.core.agent.message import (
                AssistantMessageSegment,
                TextPart,
                UserMessageSegment,
            )

            manager = self.context.conversation_manager
            cid = await manager.get_curr_conversation_id(umo)
            if not cid:
                return
            user_text = "\n".join(
                f"[{item.timestamp.isoformat()}] {item.plain_text}" for item in messages
            )
            await manager.add_message_pair(
                cid=cid,
                user_message=UserMessageSegment(content=[TextPart(text=user_text)]),
                assistant_message=AssistantMessageSegment(
                    content=[TextPart(text=reply_text)]
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 历史接口失败不能阻断已发送回复
            logger.warning(f"[角色作息] 私聊回归写入会话历史失败：{exc}")

    @staticmethod
    def _parse_group_json(raw: str) -> dict[str, str]:
        cleaned = raw.strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match:
            cleaned = match.group(0)
        try:
            data = json.loads(cleaned)
        except (TypeError, json.JSONDecodeError):
            logger.error("[角色作息] 群回归结构化输出解析失败，已使用安全回退")
            return {}
        replies = data.get("replies", []) if isinstance(data, dict) else []
        if not isinstance(replies, list):
            return {}
        result: dict[str, str] = {}
        for item in replies:
            if not isinstance(item, dict):
                continue
            sender = str(item.get("sender_id", "") or "").strip()
            text = str(item.get("text", "") or "").strip()
            if sender and text and sender not in result:
                result[sender] = text
        return result


def group_by_sender(messages: list[MailboxMessage]) -> dict[str, list[MailboxMessage]]:
    grouped: dict[str, list[MailboxMessage]] = defaultdict(list)
    for message in messages:
        grouped[message.sender_id].append(message)
    return grouped
