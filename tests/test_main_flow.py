import asyncio
import types
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from astrbot.api import message_components as Comp

from astrbot_plugin_tiangan_schedule.config import load_settings
from astrbot_plugin_tiangan_schedule.main import TianganSchedulePlugin
from astrbot_plugin_tiangan_schedule.models import (
    DailySchedule,
    OfflineEvent,
    OfflineEventType,
    PresenceState,
)

TZ = ZoneInfo("Asia/Shanghai")


class FakeContext:
    pass


class FakeResult:
    def __init__(self, text):
        self.text = text

    def get_plain_text(self):
        return self.text


class FakeEvent:
    def __init__(self, wake=True):
        self.unified_msg_origin = "aiocqhttp:GroupMessage:100"
        self.message_str = "角色Bot在吗"
        self.is_at_or_wake_command = wake
        self.message_obj = types.SimpleNamespace(
            timestamp=1786723200,
            message_id="m1",
            message=[],
        )
        self.sent = []
        self.stopped = False
        self.call_llm = True
        self.extras = {}
        self.admin = False

    def get_sender_id(self):
        return "user-1"

    def get_self_id(self):
        return "bot-1"

    def get_group_id(self):
        return "100"

    def get_platform_name(self):
        return "aiocqhttp"

    def get_sender_name(self):
        return "用户"

    def is_admin(self):
        return self.admin

    def plain_result(self, text):
        return FakeResult(text)

    async def send(self, result):
        self.sent.append(result.text)

    def should_call_llm(self, value):
        self.call_llm = value

    def stop_event(self):
        self.stopped = True

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key):
        return self.extras.get(key)


class FakeRepository:
    def __init__(self, event):
        self.event = event
        self.context_saved = []
        self.mailbox_saved = []
        self.schedule = None
        self.monitor_claims = set()
        self.meta = {}

    async def register_bot(self, *_args):
        pass

    async def touch_session(self, *_args):
        pass

    async def bot_ids(self):
        return [self.event.bot_id]

    async def get_meta(self, key):
        return self.meta.get(key)

    async def set_meta(self, key, value):
        self.meta[key] = value

    async def save_group_context(self, *args):
        self.context_saved.append(args)

    async def get_runtime(self, _bot_id):
        return types.SimpleNamespace(current_event_id=self.event.id)

    async def get_event(self, _event_id):
        return self.event

    async def get_schedule(self, _bot_id, _schedule_date):
        return self.schedule

    async def save_mailbox(self, *args):
        self.mailbox_saved.append(args)
        return True

    async def claim_offline_monitor(self, event_id, umo):
        key = (event_id, umo)
        if key in self.monitor_claims:
            return False
        self.monitor_claims.add(key)
        return True

    async def mark_offline_monitor_sent(self, *_args):
        pass

    async def release_offline_monitor(self, event_id, umo):
        self.monitor_claims.discard((event_id, umo))


class FakeRuntime:
    def __init__(self, state, pre_away_event=None):
        self.state = state
        self.pre_away_event = pre_away_event
        self.adjusted = []

    async def reconcile(self, *_args):
        return self.state

    async def ensure_calendar(self, *_args):
        pass

    async def refresh_pre_away_session(self, *_args):
        return self.pre_away_event

    async def adjust_future_schedule(self, bot_id, now):
        self.adjusted.append((bot_id, now))


def make_plugin(state):
    now = datetime.now(TZ)
    offline_event = OfflineEvent(
        "e1",
        "bot-1",
        now.date().isoformat(),
        OfflineEventType.DAYTIME_AWAY,
        "dessert_shop",
        "几分钟后去甜品店",
        now - timedelta(minutes=1),
        now + timedelta(minutes=20),
        "【监测器】暂时不在",
    )
    plugin = TianganSchedulePlugin(FakeContext(), {})
    plugin.repository = FakeRepository(offline_event)
    plugin.runtime = FakeRuntime(state)
    return plugin


def test_offline_wake_is_saved_monitored_and_stopped():
    plugin = make_plugin(PresenceState.AWAY)
    event = FakeEvent(wake=True)
    event.message_obj.message = [Comp.At(qq="bot-1")]
    asyncio.run(plugin.on_message(event))
    assert len(plugin.repository.mailbox_saved) == 1
    assert plugin.repository.context_saved == []
    assert event.sent == ["【监测器】暂时不在"]
    assert event.stopped is True
    assert event.call_llm is False


def test_private_offline_monitor_is_sent_once_but_all_messages_are_saved():
    plugin = make_plugin(PresenceState.AWAY)
    first = FakeEvent(wake=True)
    first.unified_msg_origin = "aiocqhttp:FriendMessage:user-1"
    first.get_group_id = lambda: ""
    second = FakeEvent(wake=True)
    second.unified_msg_origin = first.unified_msg_origin
    second.get_group_id = lambda: ""
    second.message_obj.message_id = "m2"

    asyncio.run(plugin.on_message(first))
    asyncio.run(plugin.on_message(second))

    assert first.sent == ["【监测器】暂时不在"]
    assert second.sent == []
    assert len(plugin.repository.mailbox_saved) == 2
    assert first.stopped is True and second.stopped is True
    assert first.call_llm is False and second.call_llm is False


def test_group_reply_only_is_not_treated_as_an_explicit_wake():
    plugin = make_plugin(PresenceState.AWAY)
    event = FakeEvent(wake=True)
    event.message_obj.message = [Comp.Reply(id="quoted-message")]

    asyncio.run(plugin.on_message(event))

    assert len(plugin.repository.context_saved) == 1
    assert plugin.repository.mailbox_saved == []
    assert event.sent == []
    assert event.stopped is True
    assert event.call_llm is False


def test_group_astrbot_wake_prefix_is_detected_from_preprocessing():
    plugin = make_plugin(PresenceState.AWAY)
    event = FakeEvent(wake=True)
    event.message_str = "角色Bot在吗"
    event.set_extra("astrbot_original_message_str", "+角色Bot在吗")

    asyncio.run(plugin.on_message(event))

    assert len(plugin.repository.mailbox_saved) == 1
    assert event.sent == ["【监测器】暂时不在"]
    assert event.stopped is True


def test_quoted_group_message_with_real_wake_prefix_still_wakes():
    plugin = make_plugin(PresenceState.AWAY)
    event = FakeEvent(wake=True)
    event.message_str = "角色Bot在吗"
    event.message_obj.message = [
        Comp.Reply(id="quoted-message"),
        Comp.Plain("+角色Bot在吗"),
    ]

    asyncio.run(plugin.on_message(event))

    assert len(plugin.repository.mailbox_saved) == 1
    assert event.sent == ["【监测器】暂时不在"]
    assert event.stopped is True


def test_offline_ordinary_group_message_is_cached_then_stopped():
    plugin = make_plugin(PresenceState.AWAY)
    event = FakeEvent(wake=False)
    asyncio.run(plugin.on_message(event))
    assert len(plugin.repository.context_saved) == 1
    assert plugin.repository.mailbox_saved == []
    assert event.sent == []
    assert event.stopped is True
    assert event.call_llm is False


def test_offline_poke_notice_is_silently_stopped_before_other_plugins():
    plugin = make_plugin(PresenceState.SLEEPING)
    event = FakeEvent(wake=False)
    event.message_str = ""
    event.message_obj.message = []
    event.message_obj.raw_event = {
        "post_type": "notice",
        "notice_type": "notify",
        "sub_type": "poke",
        "target_id": "bot-1",
        "user_id": "user-1",
    }

    asyncio.run(plugin.on_message(event))

    assert plugin.repository.context_saved == []
    assert plugin.repository.mailbox_saved == []
    assert event.sent == []
    assert event.stopped is True
    assert event.call_llm is False


def test_configured_command_is_allowed_offline_with_current_wake_prefix():
    plugin = make_plugin(PresenceState.SLEEPING)
    event = FakeEvent(wake=True)
    event.message_str = "查看核心记忆"
    event.set_extra("astrbot_original_message_str", "/查看核心记忆")

    asyncio.run(plugin.on_message(event))

    assert event.get_extra("tiangan_offline_command_allowed") is True
    assert plugin.repository.context_saved == []
    assert plugin.repository.mailbox_saved == []
    assert event.sent == []
    assert event.stopped is False
    assert event.call_llm is True

    request = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.on_llm_request(event, request))
    assert event.stopped is False
    assert event.call_llm is True


def test_offline_command_allowlist_follows_custom_astrbot_prefix():
    plugin = make_plugin(PresenceState.AWAY)
    plugin.settings = load_settings(
        {"offline_allowed_commands": ["查看记忆日志"]}
    )
    event = FakeEvent(wake=True)
    event.message_str = "查看记忆日志"
    event.set_extra("astrbot_original_message_str", "+查看记忆日志")

    asyncio.run(plugin.on_message(event))

    assert event.get_extra("tiangan_offline_command_allowed") is True
    assert event.sent == []
    assert event.stopped is False


def test_private_allowlisted_command_needs_no_prefix_when_astrbot_marks_it_awake():
    plugin = make_plugin(PresenceState.SLEEPING)
    event = FakeEvent(wake=True)
    event.unified_msg_origin = "aiocqhttp:FriendMessage:user-1"
    event.get_group_id = lambda: ""
    event.message_str = "查看核心记忆"

    asyncio.run(plugin.on_message(event))

    assert event.get_extra("tiangan_offline_command_allowed") is True
    assert event.sent == []
    assert event.stopped is False


def test_private_allowlisted_command_is_not_opened_if_astrbot_requires_wake():
    plugin = make_plugin(PresenceState.SLEEPING)
    event = FakeEvent(wake=False)
    event.unified_msg_origin = "aiocqhttp:FriendMessage:user-1"
    event.get_group_id = lambda: ""
    event.message_str = "查看核心记忆"

    asyncio.run(plugin.on_message(event))

    assert event.get_extra("tiangan_offline_command_allowed") is None
    assert event.sent == []
    assert event.stopped is True
    assert event.call_llm is False


def test_unlisted_wake_command_is_still_blocked_offline():
    plugin = make_plugin(PresenceState.SLEEPING)
    event = FakeEvent(wake=True)
    event.message_str = "查看记忆日志"
    event.set_extra("astrbot_original_message_str", "/查看记忆日志")

    asyncio.run(plugin.on_message(event))

    assert event.get_extra("tiangan_offline_command_allowed") is None
    assert event.sent == ["【监测器】暂时不在"]
    assert event.stopped is True


def test_adjust_schedule_command_is_always_allowed_to_reach_permission_check():
    plugin = make_plugin(PresenceState.SLEEPING)
    plugin.settings = load_settings({"offline_allowed_commands": []})
    event = FakeEvent(wake=True)
    event.unified_msg_origin = "aiocqhttp:FriendMessage:user-1"
    event.get_group_id = lambda: ""
    event.message_str = "调整作息"

    asyncio.run(plugin.on_message(event))

    assert event.get_extra("tiangan_offline_command_allowed") is True
    assert event.sent == []
    assert event.stopped is False


def test_adjust_schedule_rejects_non_admin_with_fixed_text():
    plugin = make_plugin(PresenceState.ONLINE)
    event = FakeEvent()

    async def collect():
        return [item.get_plain_text() async for item in plugin.adjust_schedule(event)]

    assert asyncio.run(collect()) == ["只有亲妈才能修改我的作息表……"]
    assert plugin.runtime.adjusted == []


def test_admin_adjust_schedule_changes_global_bot_schedule_from_private_chat():
    plugin = make_plugin(PresenceState.ONLINE)
    event = FakeEvent()
    event.admin = True
    event.unified_msg_origin = "aiocqhttp:FriendMessage:admin"
    event.get_group_id = lambda: ""

    async def collect():
        return [item.get_plain_text() async for item in plugin.adjust_schedule(event)]

    assert asyncio.run(collect()) == ["作息表已经重新调整好了。"]
    assert len(plugin.runtime.adjusted) == 1
    assert plugin.runtime.adjusted[0][0] == "bot-1"


def test_changed_schedule_configuration_rebuilds_once_and_saves_fingerprint():
    plugin = make_plugin(PresenceState.ONLINE)

    asyncio.run(plugin._sync_schedule_configuration())
    first_fingerprint = plugin.repository.meta["schedule_config_fingerprint"]
    assert len(plugin.runtime.adjusted) == 1

    asyncio.run(plugin._sync_schedule_configuration())
    assert len(plugin.runtime.adjusted) == 1

    plugin.settings = load_settings(
        {
            "reasons": {
                "daytime_json": """
                [{
                  "id": "walk",
                  "pre_away_fact": "几分钟后出去走走",
                  "monitor_messages": ["出去走走了"]
                }]
                """
            }
        }
    )
    asyncio.run(plugin._sync_schedule_configuration())
    assert len(plugin.runtime.adjusted) == 2
    assert plugin.repository.meta["schedule_config_fingerprint"] != first_fingerprint


def test_existing_blank_sleep_monitor_recovers_current_bot_name():
    plugin = make_plugin(PresenceState.SLEEPING)
    old = plugin.repository.event
    plugin.repository.event = OfflineEvent(
        id=old.id,
        bot_id=old.bot_id,
        schedule_date=old.schedule_date,
        event_type=OfflineEventType.NIGHT_SLEEP,
        reason_id="night_sleep",
        pre_away_fact=old.pre_away_fact,
        start_at=old.start_at,
        end_at=old.end_at,
        fixed_monitor_text="【作息监测器提示】已经睡着了，暂时看不到消息……",
    )
    event = FakeEvent(wake=True)
    event.unified_msg_origin = "aiocqhttp:FriendMessage:user-1"
    event.get_group_id = lambda: ""
    event.message_obj.self_name = "王bot"

    asyncio.run(plugin.on_message(event))

    assert event.sent == ["【作息监测器提示】王bot已经睡着了，暂时看不到消息……"]


def test_online_wake_is_not_intercepted():
    plugin = make_plugin(PresenceState.ONLINE)
    event = FakeEvent(wake=True)
    asyncio.run(plugin.on_message(event))
    assert len(plugin.repository.context_saved) == 1
    assert plugin.repository.mailbox_saved == []
    assert event.stopped is False


def test_preaway_normal_private_request_gets_temporary_instruction():
    plugin = make_plugin(PresenceState.PRE_AWAY)
    pre_away = plugin.repository.event
    plugin.runtime = FakeRuntime(PresenceState.PRE_AWAY, pre_away)
    claimed = []

    async def claim(event_id, umo):
        claimed.append((event_id, umo))
        return True

    plugin.repository.claim_notice = claim
    event = FakeEvent(wake=True)
    event.unified_msg_origin = "aiocqhttp:FriendMessage:user-1"
    event.get_group_id = lambda: ""
    request = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.on_llm_request(event, request))
    assert claimed == [(pre_away.id, event.unified_msg_origin)]
    assert len(request.extra_user_content_parts) == 1
    assert request.extra_user_content_parts[0].temporary is True
    assert "只作用于本次回复" in request.extra_user_content_parts[0].text


def test_offline_llm_request_has_a_final_global_blocker():
    plugin = make_plugin(PresenceState.SLEEPING)
    event = FakeEvent(wake=True)
    request = types.SimpleNamespace(extra_user_content_parts=[])

    asyncio.run(plugin.on_llm_request(event, request))

    assert event.stopped is True
    assert event.call_llm is False
    assert request.extra_user_content_parts == []


def test_preaway_followup_gets_continuity_constraint_after_notice_was_sent():
    plugin = make_plugin(PresenceState.PRE_AWAY)
    pre_away = plugin.repository.event
    plugin.runtime = FakeRuntime(PresenceState.PRE_AWAY, pre_away)

    async def already_sent(_event_id, _umo):
        return False

    plugin.repository.claim_notice = already_sent
    event = FakeEvent(wake=True)
    event.unified_msg_origin = "aiocqhttp:FriendMessage:user-1"
    event.get_group_id = lambda: ""
    request = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.on_llm_request(event, request))

    assert len(request.extra_user_content_parts) == 1
    assert request.extra_user_content_parts[0].temporary is True
    assert "已经预告、但尚未正式离线" in request.extra_user_content_parts[0].text
    assert event.get_extra("tiangan_pre_away_notice") is None


def test_today_schedule_only_shows_wake_and_sleep_times():
    plugin = make_plugin(PresenceState.ONLINE)
    offline_event = plugin.repository.event
    plugin.repository.schedule = DailySchedule(
        bot_id="bot-1",
        schedule_date=offline_event.schedule_date,
        timezone="Asia/Shanghai",
        wake_at=offline_event.start_at - timedelta(hours=4),
        sleep_at=offline_event.end_at + timedelta(hours=8),
        events=(offline_event,),
    )
    event = FakeEvent()

    async def collect():
        return [
            item.get_plain_text()
            async for item in plugin.today_schedule(event)
        ]

    output = asyncio.run(collect())
    assert len(output) == 1
    assert "日期：" in output[0]
    assert "起床：" in output[0]
    assert "睡觉：" in output[0]
    assert plugin.repository.schedule.wake_at.strftime("%H:%M") in output[0]
    assert plugin.repository.schedule.sleep_at.strftime("%Y-%m-%d %H:%M") in output[0]
    assert plugin.repository.schedule.wake_at.strftime("%H:%M:%S") not in output[0]
    assert plugin.repository.schedule.sleep_at.strftime("%Y-%m-%d %H:%M:%S") not in output[0]
    assert "离开：" not in output[0]
    assert "dessert_shop" not in output[0]
    assert "JSON 配置错误" not in output[0]


def test_today_schedule_precise_mode_only_lists_away_start_minutes():
    plugin = make_plugin(PresenceState.ONLINE)
    plugin.settings = load_settings(
        {"daytime_away": {"show_precise_schedule": True}}
    )
    offline_event = plugin.repository.event
    later_event = OfflineEvent(
        id="e2",
        bot_id=offline_event.bot_id,
        schedule_date=offline_event.schedule_date,
        event_type=OfflineEventType.DAYTIME_AWAY,
        reason_id="reading",
        pre_away_fact="几分钟后去看书",
        start_at=offline_event.start_at + timedelta(hours=3, seconds=17),
        end_at=offline_event.end_at + timedelta(hours=4, seconds=29),
        fixed_monitor_text="看书去了",
    )
    plugin.repository.schedule = DailySchedule(
        bot_id="bot-1",
        schedule_date=offline_event.schedule_date,
        timezone="Asia/Shanghai",
        wake_at=offline_event.start_at - timedelta(hours=4),
        sleep_at=later_event.end_at + timedelta(hours=5),
        events=(later_event, offline_event),
    )
    event = FakeEvent()

    async def collect():
        return [
            item.get_plain_text()
            async for item in plugin.today_schedule(event)
        ]

    output = asyncio.run(collect())[0]
    first_start = offline_event.start_at.strftime("%H:%M")
    second_start = later_event.start_at.strftime("%H:%M")
    assert f"可能暂时离开：{first_start}、{second_start}" in output
    assert offline_event.end_at.strftime("%H:%M:%S") not in output
    assert later_event.end_at.strftime("%H:%M:%S") not in output
    assert "dessert_shop" not in output
    assert "reading" not in output


def test_today_schedule_appends_reason_json_error_only_when_invalid():
    plugin = make_plugin(PresenceState.ONLINE)
    plugin.settings = load_settings({"reasons": {"daytime_json": "[invalid"}})
    offline_event = plugin.repository.event
    plugin.repository.schedule = DailySchedule(
        bot_id="bot-1",
        schedule_date=offline_event.schedule_date,
        timezone="Asia/Shanghai",
        wake_at=offline_event.start_at - timedelta(hours=4),
        sleep_at=offline_event.end_at + timedelta(hours=8),
        events=(offline_event,),
    )
    event = FakeEvent()

    async def collect():
        return [
            item.get_plain_text()
            async for item in plugin.today_schedule(event)
        ]

    output = asyncio.run(collect())
    assert len(output) == 1
    assert "⚠ 白天离开原因 JSON 配置错误" in output[0]
    assert "第 1 行" in output[0]
    assert "离开：" not in output[0]


def test_schedule_status_uses_three_public_chinese_labels():
    cases = {
        PresenceState.ONLINE: "在线",
        PresenceState.PRE_AWAY: "在线",
        PresenceState.AWAY: "暂时离开",
        PresenceState.SLEEPING: "睡觉",
        PresenceState.RETURNING: "在线",
    }

    async def collect(plugin, event):
        return [
            item.get_plain_text()
            async for item in plugin.schedule_status(event)
        ]

    for state, label in cases.items():
        plugin = make_plugin(state)
        output = asyncio.run(collect(plugin, FakeEvent()))
        assert output == [f"当前状态：{label}"]


def test_public_availability_only_allows_online_state():
    sleeping = make_plugin(PresenceState.SLEEPING)
    sleeping_snapshot = asyncio.run(
        sleeping._provide_schedule_availability("bot-1")
    )
    assert sleeping_snapshot is not None
    assert sleeping_snapshot.state == "SLEEPING"
    assert sleeping_snapshot.can_send_proactive is False
    assert sleeping_snapshot.next_online_at == sleeping.repository.event.end_at

    online = make_plugin(PresenceState.ONLINE)
    online_snapshot = asyncio.run(
        online._provide_schedule_availability("bot-1")
    )
    assert online_snapshot is not None
    assert online_snapshot.state == "ONLINE"
    assert online_snapshot.can_send_proactive is True
    assert online_snapshot.next_online_at is None
