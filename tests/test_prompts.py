from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from astrbot_plugin_tiangan_schedule.llm_service import normalize_notice
from astrbot_plugin_tiangan_schedule.models import (
    MailboxMessage,
    OfflineEvent,
    OfflineEventType,
    QueueState,
)
from astrbot_plugin_tiangan_schedule.prompts import (
    group_return_prompt,
    pre_away_continuity_prompt,
    private_return_prompt,
    return_notice_prompt,
    standalone_pre_away_prompt,
)

TZ = ZoneInfo("Asia/Shanghai")


def fixtures():
    now = datetime(2026, 8, 15, 12, tzinfo=TZ)
    event = OfflineEvent(
        "e1",
        "bot",
        "2026-08-15",
        OfflineEventType.DAYTIME_AWAY,
        "dessert_shop",
        "几分钟后去甜品店，可能无法及时回复",
        now,
        now + timedelta(hours=1),
        "不在",
    )
    messages = [
        MailboxMessage(
            1,
            "e1",
            "aiocqhttp:GroupMessage:1",
            "group",
            "1",
            "123",
            "A",
            "m1",
            now,
            "回来看看这个",
            "[]",
            QueueState.SELECTED,
        )
    ]
    return event, messages


def test_preaway_is_a_separate_short_message_for_group_or_private():
    event, _ = fixtures()
    private_prompt = standalone_pre_away_prompt(event, "private")
    group_prompt = standalone_pre_away_prompt(event, "group")
    assert "当前私聊" in private_prompt
    assert "当前群聊" in group_prompt
    assert "单独发送" in private_prompt
    assert "不要回应聊天中的其他问题" in private_prompt
    assert "最多 30 个字符" in group_prompt
    assert "甜品店" in group_prompt


def test_short_notice_is_one_line_and_hard_limited_to_30_chars():
    raw = "第一行\n" + "很长" * 30
    notice = normalize_notice(raw, "刚回来")
    assert "\n" not in notice
    assert len(notice) == 30
    assert normalize_notice("", "醒了") == "醒了"


def test_sleep_continuity_prompt_prevents_contradicting_the_schedule():
    now = datetime(2026, 8, 15, 23, tzinfo=TZ)
    event = OfflineEvent(
        "sleep-1",
        "bot",
        "2026-08-15",
        OfflineEventType.NIGHT_SLEEP,
        "night_sleep",
        "很快要准备睡觉，之后可能无法及时回复",
        now,
        now + timedelta(hours=8),
        "睡着了",
    )
    prompt = pre_away_continuity_prompt(event)
    assert "即将按计划离线" in prompt
    assert "不得承诺取消睡眠" in prompt
    assert "另一条独立消息发送" in prompt
    assert "不得在开头或结尾拼接预告" in prompt
    assert "只作用于本次回复" in prompt


def test_return_notice_is_separate_and_short_by_instruction():
    event, _ = fixtures()
    prompt = return_notice_prompt(event)
    assert "单独发送" in prompt
    assert "最多 30 个字符" in prompt
    assert "不要回应对方离线期间留下的消息" in prompt
    assert "刚回来" in prompt


def test_sleep_return_prompt_uses_configured_night_instruction():
    event, messages = fixtures()
    sleep_event = OfflineEvent(
        event.id,
        event.bot_id,
        event.schedule_date,
        OfflineEventType.NIGHT_SLEEP,
        "night_sleep",
        "很快要准备睡觉",
        event.start_at,
        event.end_at,
        "睡着了",
    )
    instruction = "这是完整夜间睡眠，禁止说只是打了个盹。"

    private_prompt = private_return_prompt(sleep_event, messages, instruction)
    group_prompt = group_return_prompt(sleep_event, messages, [], instruction)

    assert instruction in private_prompt
    assert instruction in group_prompt
    assert "本次离线类型的确定要求" in private_prompt
    assert "本次离线类型的确定要求" in group_prompt


def test_group_prompt_only_directs_selected_packages():
    event, messages = fixtures()
    prompt = group_return_prompt(
        event,
        messages,
        [
            {
                "timestamp": "2026-08-15T11:59:00+08:00",
                "sender_name": "路人",
                "plain_text": "普通水聊",
            }
        ],
    )
    assert 'sender_id="123"' in prompt
    assert "只回应这些消息包" in prompt
    assert "不要回应只出现在 group_context" in prompt
    assert "只输出合法 JSON" in prompt


def test_private_prompt_has_one_request_length_exemption():
    event, messages = fixtures()
    private_messages = [
        MailboxMessage(
            item.id,
            item.offline_event_id,
            item.umo.replace("GroupMessage", "FriendMessage"),
            "private",
            "",
            item.sender_id,
            item.sender_name,
            item.message_id,
            item.timestamp,
            item.plain_text,
            item.components_json,
            item.selection_state,
        )
        for item in messages
    ]
    prompt = private_return_prompt(event, private_messages)
    assert "临时解除" in prompt
    assert "仅适用于当前回复" in prompt
    assert "简短内容应简短回应" in prompt
    assert "代码会另发" in prompt
    assert "不得在开头或结尾拼接" in prompt
