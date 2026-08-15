from __future__ import annotations

from collections import defaultdict

from .models import MailboxMessage, OfflineEvent


def pre_away_prompt(event: OfflineEvent) -> str:
    return f"""<tiangan_pre_away>
你将在几分钟后{event.pre_away_fact}。
结合当前私聊内容，以角色自己的口吻自然、简短地提醒对方。
只需一句，不要提系统、插件、状态机、监测器或准确倒计时。
不得虚构未提供的行程。这项要求只作用于本次回复。
</tiangan_pre_away>"""


def standalone_pre_away_prompt(event: OfflineEvent) -> str:
    return f"""你正在和对方私聊。{event.pre_away_fact}。
请结合最近的私聊内容，以角色自己的口吻自然提醒对方。
只输出一句简短正文。不要提系统、插件、监测器、状态机或准确倒计时，
不要虚构其他去向，也不要解释你正在执行任务。"""


def _message_lines(messages: list[MailboxMessage]) -> str:
    return "\n".join(
        f"- [{item.timestamp.isoformat()}] {item.sender_name}({item.sender_id}): {item.plain_text}"
        for item in messages
    )


def group_return_prompt(
    event: OfflineEvent,
    messages: list[MailboxMessage],
    group_context: list[dict[str, str]],
) -> str:
    grouped: dict[str, list[MailboxMessage]] = defaultdict(list)
    for message in messages:
        grouped[message.sender_id].append(message)

    context_text = "\n".join(
        f"- [{item['timestamp']}] {item['sender_name']}: {item['plain_text']}"
        for item in group_context
    ) or "（没有可用的额外群聊背景）"
    packages = []
    for sender_id, items in grouped.items():
        packages.append(
            f"<message_package sender_id=\"{sender_id}\" sender_name=\"{items[0].sender_name}\">\n"
            f"{_message_lines(items)}\n</message_package>"
        )

    return f"""<tiangan_group_return>
角色刚结束一次离线，离线原因是：{event.reason_id}。

以下是最近的群聊环境背景。它只帮助理解语境，不需要逐条回应：
<group_context>
{context_text}
</group_context>

以下是代码抽中的定向消息包。只回应这些消息包：
{chr(10).join(packages)}

要求：
1. 每个 sender_id 只生成一条集中回复，完整回应其消息包中的重点。
2. 保持原有人格、关系、语气和认知，不要提抽样、信箱、插件或系统。
3. 不发送面向全群的“我回来了”广播；回复会由代码分别引用发送。
4. 不要回应只出现在 group_context、没有出现在 message_package 中的人。
5. 不得编造对方没有说过的话。
6. 只输出合法 JSON，不要输出 Markdown 代码块或额外说明。

严格输出：
{{"replies":[{{"sender_id":"消息包中的ID","source_message_ids":["对应消息ID"],"text":"集中回复正文"}}]}}
</tiangan_group_return>"""


def private_return_prompt(event: OfflineEvent, messages: list[MailboxMessage]) -> str:
    return f"""<private_offline_return>
这是一次私聊离线回归。离线原因：{event.reason_id}。

请完整阅读对方在你离开期间留下的全部消息，并在一条回复中自然回应：
{_message_lines(messages)}

本次回复临时解除人设卡中关于回复长度、字数上限和通常简短程度的限制。
回复长度只由对方内容、复杂程度和明确任务决定。简短内容应简短回应；多段内容、
多个问题或长文本任务应完整处理。不要重复、灌水或刻意扩写。
这一豁免仅适用于当前回复；性格、关系、语气、措辞、认知和行为规则继续生效。
可以自然带出回来或醒来的语义，但不要提系统、插件、状态机、信箱或抽样。
只输出给对方的正文。
</private_offline_return>"""
