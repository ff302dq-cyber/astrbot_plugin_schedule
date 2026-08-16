from astrbot_plugin_tiangan_schedule.config import load_settings


def test_defaults_are_valid():
    settings = load_settings({})
    assert settings.timezone == "Asia/Shanghai"
    assert settings.bot_name == ""
    assert settings.sample_rate == 0.30
    assert settings.total_minutes_min == 60
    assert settings.total_minutes_max == 120
    assert settings.segments_min == 1
    assert settings.segments_max == 2
    assert settings.segment_minutes_min == 30
    assert settings.segment_minutes_max == 120
    assert settings.daytime_placement_mode == "balanced"
    assert settings.show_precise_schedule is False
    assert settings.daytime_reasons
    assert settings.daytime_reasons_error == ""
    assert "{bot_name}" in settings.daytime_reasons[0].monitor_messages[0]
    assert "打个盹" in settings.daytime_reasons[1].pre_away_fact
    assert "睡午觉" not in settings.daytime_reasons[1].monitor_messages[0]
    assert "符合角色人设和双方关系" in settings.night_reason.pre_away_fact
    assert "完整夜间睡眠" in settings.night_return_instruction
    assert "只是打了个盹" in settings.night_return_instruction
    assert settings.offline_allowed_commands == (
        "查看核心记忆",
        "记忆总结",
        "查看总结进度",
    )


def test_invalid_ranges_are_normalized():
    settings = load_settings(
        {
            "daytime_away": {
                "total_minutes_min": 100,
                "total_minutes_max": 20,
                "segments_min": 4,
                "segments_max": 1,
                "segment_minutes_min": 20,
                "segment_minutes_max": 5,
            },
            "group_return": {"sample_rate": 3, "sample_fluctuation": -1},
        }
    )
    assert settings.total_minutes_max >= settings.total_minutes_min
    assert settings.segments_max >= settings.segments_min
    assert settings.segment_minutes_max >= settings.segment_minutes_min
    assert settings.sample_rate == 1.0
    assert settings.sample_fluctuation == 0.0


def test_unlimited_json_text_reasons_and_monitor_message_arrays():
    settings = load_settings(
        {
            "reasons": {
                "daytime_json": """
                [
                  {
                    "id": "reading",
                    "pre_away_fact": "几分钟后去看书",
                    "monitor_messages": ["第一条", "第二条"]
                  },
                  {
                    "id": "",
                    "pre_away_fact": "几分钟后去散步",
                    "monitor_messages": ["第三条"]
                  }
                ]
                """
            }
        }
    )
    assert len(settings.daytime_reasons) == 2
    assert settings.daytime_reasons[0].id == "reading"
    assert settings.daytime_reasons[0].monitor_messages == ("第一条", "第二条")
    assert settings.daytime_reasons[1].id == "reason_2"
    assert settings.daytime_reasons_error == ""


def test_invalid_reason_json_falls_back_to_defaults():
    settings = load_settings({"reasons": {"daytime_json": "[invalid"}})
    assert settings.daytime_reasons
    assert settings.daytime_reasons[0].id == "dessert_shop"
    assert "第 1 行" in settings.daytime_reasons_error


def test_structurally_invalid_reason_json_reports_item_number():
    settings = load_settings(
        {
            "reasons": {
                "daytime_json": '[{"id":"missing_fields"}]'
            }
        }
    )
    assert "第 1 条原因" in settings.daytime_reasons_error


def test_daytime_placement_mode_can_switch():
    free = load_settings({"daytime_away": {"placement_mode": "自由随机"}})
    assert free.daytime_placement_mode == "free_random"

    balanced = load_settings({"daytime_away": {"placement_mode": "均匀散布"}})
    assert balanced.daytime_placement_mode == "balanced"


def test_precise_schedule_switch_can_be_enabled():
    settings = load_settings(
        {"daytime_away": {"show_precise_schedule": True}}
    )
    assert settings.show_precise_schedule is True


def test_new_night_instruction_field_overrides_legacy_fact():
    settings = load_settings(
        {
            "reasons": {
                "night_sleep": {
                    "pre_away_instruction": "新的睡前要求",
                    "pre_away_fact": "旧字段",
                    "monitor_messages": "睡着了\n已经休息",
                    "return_instruction": "这是自定义睡醒回归要求",
                }
            }
        }
    )
    assert settings.night_reason.pre_away_fact == "新的睡前要求"
    assert settings.night_reason.monitor_messages == ("睡着了", "已经休息")
    assert settings.night_return_instruction == "这是自定义睡醒回归要求"


def test_offline_allowed_commands_are_normalized_and_can_be_empty():
    settings = load_settings(
        {
            "offline_allowed_commands": [
                " 查看核心记忆 ",
                "修改核心记忆   增加内容",
                "查看核心记忆",
                "",
            ]
        }
    )
    assert settings.offline_allowed_commands == (
        "查看核心记忆",
        "修改核心记忆 增加内容",
    )
    assert load_settings({"offline_allowed_commands": []}).offline_allowed_commands == ()
