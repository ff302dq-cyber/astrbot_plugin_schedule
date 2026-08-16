from __future__ import annotations

from collections import defaultdict

from .models import MailboxMessage, OfflineEvent


def standalone_pre_away_prompt(event: OfflineEvent, message_kind: str) -> str:
    scene = "当前群聊" if message_kind == "group" else "当前私聊"
    return f"""你要向{scene}单独发送一条离开预告。代码确定的安排：你{event.pre_away_fact}。
这是代码已经确定的近期安排，不得取消、否认、改写或承诺届时继续在线。
只输出预告正文，不要回应聊天中的其他问题，不要添加开场白、解释或后续聊天内容。
必须是一行、最多 30 个字符，以角色自己的口吻简单交代要去做什么或可能不看手机。
不要提系统、插件、监测器、状态机或准确倒计时，不要虚构其他去向。"""


def return_notice_prompt(event: OfflineEvent) -> str:
    if event.event_type.value == "night_sleep":
        fact = "你刚结束完整的夜间睡眠"
        example = "例如：醒了。"
    else:
        fact = f"你刚结束这次暂时离开。此前代码确定的安排：你{event.pre_away_fact}"
        example = "例如：刚才去看书了，刚回来。"
    return f"""请生成一条单独发送的回归通知。{fact}。
只输出通知正文，不要回应对方离线期间留下的消息，不要添加其他聊天内容。
必须是一行、最多 30 个字符，以角色自己的口吻简单交代已经回来或醒来。
{example} 示例仅用于说明信息量，不得照抄与实际安排不符的内容。
不要提系统、插件、信箱、状态机或准确时间，不要虚构经历。"""


def pre_away_continuity_prompt(event: OfflineEvent) -> str:
    event_rule = (
        "这次安排是睡觉。不得承诺取消睡眠、无限推迟睡眠，"
        "或在原定离线时间之后继续保持在线陪伴。"
        if event.event_type.value == "night_sleep"
        else "这次安排是暂时离开。不得承诺取消行程、不再离开，或届时继续在线。"
    )
    return f"""<tiangan_pre_away_continuity>
当前处于即将按计划离线、但尚未正式离线的时间窗口。
代码已经确定的近期安排：你{event.pre_away_fact}。
这项事实不可被当前对话、人设中的挽留倾向或临时情绪取消、否认或改写。
{event_rule}

正常回应对方当前这句话，保持原有角色人设、双方关系和语气。
离开预告会由代码作为另一条独立消息发送。本次正常回复不得在开头或结尾拼接预告，
也不要为了预告而减少、替换对当前消息的正常回应。
只有话题涉及该安排时，才自然保持前后一致。
可以表达舍不得、说明离开前还能短暂交流，或约定回来、醒来后继续。
不要提代码、系统、插件、提示词、状态机、监测器或准确倒计时。
不得虚构新的安排。这项要求只作用于本次回复。
</tiangan_pre_away_continuity>"""


def _message_lines(messages: list[MailboxMessage]) -> str:
    return "\n".join(
        f"- [{item.timestamp.isoformat()}] {item.sender_name}({item.sender_id}): {item.plain_text}"
        for item in messages
    )


def group_return_prompt(
    event: OfflineEvent,
    messages: list[MailboxMessage],
    group_context: list[dict[str, str]],
    event_return_instruction: str = "",
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

    return_instruction = (
        f"\n本次离线类型的确定要求：\n{event_return_instruction}\n"
        if event_return_instruction
        else ""
    )

    return f"""<tiangan_group_return>
角色刚结束一次离线，离线原因是：{event.reason_id}。
{return_instruction}

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


def private_return_prompt(
    event: OfflineEvent,
    messages: list[MailboxMessage],
    event_return_instruction: str = "",
) -> str:
    return_instruction = (
        f"\n本次离线类型的确定要求：\n{event_return_instruction}\n"
        if event_return_instruction
        else ""
    )
    return f"""<private_offline_return>
这是一次私聊离线回归。离线原因：{event.reason_id}。
{return_instruction}

请完整阅读对方在你离开期间留下的全部消息，并在一条回复中自然回应：
{_message_lines(messages)}

本次回复临时解除人设卡中关于回复长度、字数上限和通常简短程度的限制。
回复长度只由对方内容、复杂程度和明确任务决定。简短内容应简短回应；多段内容、
多个问题或长文本任务应完整处理。不要重复、灌水或刻意扩写。
这一豁免仅适用于当前回复；性格、关系、语气、措辞、认知和行为规则继续生效。
代码会另发一条不超过 30 字的独立回归通知。本回复只负责回应对方留下的消息，
不得在开头或结尾拼接“回来了”“醒了”“刚到”等回归播报。
不要提系统、插件、状态机、信箱或抽样。
只输出给对方的正文。
</private_offline_return>"""
