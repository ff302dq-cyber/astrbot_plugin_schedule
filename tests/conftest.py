import sys
import types


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _Filter:
    EventMessageType = types.SimpleNamespace(ALL="all")
    PermissionType = types.SimpleNamespace(ADMIN="admin")

    @staticmethod
    def _decorator(*_args, **_kwargs):
        return lambda function: function

    event_message_type = _decorator
    on_llm_request = _decorator
    on_llm_response = _decorator
    after_message_sent = _decorator
    permission_type = _decorator
    command = _decorator


class _Star:
    def __init__(self, context):
        self.context = context


class _StarTools:
    @staticmethod
    def get_data_dir():
        return "."


class _TextPart:
    def __init__(self, text):
        self.text = text
        self.temporary = False

    def mark_as_temp(self):
        self.temporary = True
        return self


class _Plain:
    def __init__(self, text):
        self.text = text


class _At:
    def __init__(self, qq):
        self.qq = qq


class _Reply:
    def __init__(self, id):
        self.id = id


class _MessageChain:
    def __init__(self, chain=None):
        self.chain = list(chain or [])

    def message(self, text):
        self.chain.append(_Plain(text))
        return self


def _register(*_args, **_kwargs):
    return lambda cls: cls


astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
event = types.ModuleType("astrbot.api.event")
provider = types.ModuleType("astrbot.api.provider")
star = types.ModuleType("astrbot.api.star")
components = types.ModuleType("astrbot.api.message_components")
core = types.ModuleType("astrbot.core")
agent = types.ModuleType("astrbot.core.agent")
agent_message = types.ModuleType("astrbot.core.agent.message")

api.AstrBotConfig = dict
api.logger = _Logger()
api.message_components = components
event.filter = _Filter()
event.AstrMessageEvent = object
event.MessageChain = _MessageChain
provider.ProviderRequest = object
provider.LLMResponse = object
star.Context = object
star.Star = _Star
star.StarTools = _StarTools
star.register = _register
components.Plain = _Plain
components.At = _At
components.Reply = _Reply
agent_message.TextPart = _TextPart

astrbot.api = api
astrbot.core = core
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api)
sys.modules.setdefault("astrbot.api.event", event)
sys.modules.setdefault("astrbot.api.provider", provider)
sys.modules.setdefault("astrbot.api.star", star)
sys.modules.setdefault("astrbot.api.message_components", components)
sys.modules.setdefault("astrbot.core", core)
sys.modules.setdefault("astrbot.core.agent", agent)
sys.modules.setdefault("astrbot.core.agent.message", agent_message)
