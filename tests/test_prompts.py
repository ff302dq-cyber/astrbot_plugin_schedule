from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from astrbot_plugin_tiangan_schedule.models import (
    MailboxMessage,
    OfflineEvent,
    OfflineEventType,
    QueueState,
)
from astrbot_plugin_tiangan_schedule.prompts import (
    group_return_prompt,
    pre_away_continuity_prompt,
    pre_away_prompt,
    private_return_prompt,
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


def test_preaway_is_request_local_instruction():
    event, _ = fixtures()
    prompt = pre_away_prompt(event)
    assert "只作用于本次回复" in prompt
    assert "甜品店" in prompt
    assert "不得取消" in prompt


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
    assert "已经预告、但尚未正式离线" in prompt
    assert "不得承诺取消睡眠" in prompt
    assert "不必主动重复完整预告" in prompt
    assert "只作用于本次回复" in prompt


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
