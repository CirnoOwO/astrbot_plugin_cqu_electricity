"""Caller-scoped operations shared by QQ commands and LLM tools.

The plugin holds the sender's identity lock around every operation.
"""
import re
import shlex
from decimal import Decimal

from . import messages
from .models import ERRORS, QueryError, Room, shapingba_building


def parse_room(value):
    args = shlex.split(value) if isinstance(value, str) else value
    if len(args) == 1:
        return Room.from_account(args[0])
    if len(args) == 2:
        campus = {"虎溪": "huxi", "沙坪坝": "shapingba", "huxi": "huxi", "shapingba": "shapingba"}.get(args[0])
        if campus:
            room = Room.from_account(args[1])
            if room.campus != campus:
                raise ValueError("账号格式与所填校区不一致。")
            return room
        return Room.validated("shapingba" if shapingba_building(args[0]) else "huxi", *args)
    if len(args) == 3:
        return Room.validated(*args)
    if len(args) == 4 and args[0] in {"沙坪坝", "shapingba"}:
        return Room.validated(args[0], args[1], args[2] + args[3])
    raise ValueError("用法：/电费 绑定 A1101 或 B10S306；也可填写校区、楼栋、房号。")


def reminder_value(value, unit):
    if unit not in {"元", "天"} or isinstance(value, bool):
        raise ValueError("提醒需要指定金额或天数，例如 /电费 提醒 10元 或 /电费 提醒 2天。")
    if isinstance(value, (int, float, Decimal)):
        value = format(Decimal(str(value)), "f")
    if not re.fullmatch(r"-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", str(value).strip()):
        raise ValueError("提醒数值应为有效数字，例如 10元 或 2.5天。")
    result = Decimal(str(value).strip())
    if unit == "元" and not 0 <= result <= 10000:
        raise ValueError("金额应为 0～10000 元。")
    if unit == "天" and not result > 0:
        raise ValueError("提醒天数应大于 0。")
    return result, "balance" if unit == "元" else "days"


def parse_reminder(args, default="10"):
    if not args:
        return "get", None, None
    if args == ["关闭"]:
        return "off", None, None
    if args[0] == "开启" and len(args) in {1, 2}:
        value = args[1] if len(args) == 2 else default
        # Compatibility with the old amount-only command, including 元 suffix.
        return "set", str(value).removesuffix("元"), "元"
    match = re.fullmatch(r"(-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))\s*(元|天)", " ".join(args))
    if match:
        return "set", match[1], match[2]
    raise ValueError("用法：/电费 提醒 10元、/电费 提醒 2天，或 /电费 提醒 关闭。")


class ElectricityActions:
    def __init__(self, service, config):
        self.service, self.repo, self.config = service, service.repo, config

    @staticmethod
    def result(message, **state):
        return {"ok": True, "message": message, **state}

    def get_reminder(self, identity, origin):
        binding = self.repo.binding(identity)
        if not binding:
            return {"ok": False, "error_code": "NOT_BOUND", "message": messages.NOT_BOUND}
        sub = self.repo.subscription(binding["id"])
        state = {"enabled": bool(sub and sub["enabled"])}
        if state["enabled"]:
            state.update(unit="天" if sub["reminder_type"] == "days" else "元",
                         value=sub["days_threshold"] if sub["reminder_type"] == "days" else sub["low_balance_threshold"],
                         destination=messages.destination(sub, origin))
        return self.result(messages.reminder_status(sub, origin), reminder=state)

    def set_reminder(self, identity, origin, value, unit):
        threshold, kind = reminder_value(value, unit)
        binding = self.repo.binding(identity)
        if not binding:
            return {"ok": False, "error_code": "NOT_BOUND", "message": messages.NOT_BOUND}
        if not self.config.get("reminder_enabled", True):
            return {"ok": False, "error_code": "REMINDERS_DISABLED", "message": "管理员暂时关闭了电费提醒功能。"}
        old = self.repo.subscription(binding["id"])
        self.repo.subscribe(binding["id"], True, threshold, reminder_type=kind,
                            platform_id=identity[0], group_id=identity[1], origin=origin)
        new = self.repo.subscription(binding["id"])
        result = self.get_reminder(identity, origin)
        result["message"] = messages.reminder_set(old, new)
        return result

    def disable_reminder(self, identity, origin):
        binding = self.repo.binding(identity)
        old = self.repo.subscription(binding["id"]) if binding else None
        if not old or not old["enabled"]:
            return self.result("你当前没有开启电费提醒。", reminder={"enabled": False})
        self.repo.subscribe(binding["id"], False, old["low_balance_threshold"])
        return self.result("已关闭你的电费提醒，需要时可以再开启。", reminder={"enabled": False})

    async def bind(self, identity, origin, value):
        room = parse_room(value)
        old = self.repo.binding(identity)
        try:
            new = await self.service.bind(identity, room, origin)
        except QueryError as exc:
            message = "这次没能验证宿舍：" + ERRORS.get(exc.code, "暂时无法查询。")
            if old:
                message += "\n原来的绑定和提醒都保留着。"
            return {"ok": False, "error_code": exc.code, "message": message}
        latest = self.repo.latest(new["room_id"])
        return self.result(messages.binding_success(old, new, latest["total_balance_yuan"]),
                           room={k: new[k] for k in ("campus", "building", "room_number")})

    def unbind(self, identity, origin):
        if not self.repo.binding(identity):
            return self.result("你当前没有绑定宿舍。", bound=False)
        self.repo.unbind(identity)
        return self.result("已解绑，关联提醒也已关闭。历史记录会保留。", bound=False)
