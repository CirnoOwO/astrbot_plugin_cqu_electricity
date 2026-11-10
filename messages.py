"""Shared deterministic copy for commands, tools and proactive notifications."""
from decimal import Decimal, ROUND_HALF_UP


NOT_BOUND = "你当前尚未绑定宿舍，先发送 /电费 绑定 A1101（替换成你的宿舍账号）即可。"


def short(value):
    text = format(Decimal(str(value)), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def condition(sub):
    if sub["reminder_type"] == "days":
        return f"预计不足 {short(sub['days_threshold'])} 天"
    return f"余额不足 {short(sub['low_balance_threshold'])} 元"


def destination(sub, origin):
    if sub["origin"] == origin:
        return "本会话"
    return "私聊" if not sub["group_id"] else "其他群聊"


def reminder_set(old, new):
    rule = condition(new)
    if not old or not old["enabled"]:
        phrase = (f"预计还能用不到 {short(new['days_threshold'])} 天时" if new["reminder_type"] == "days"
                  else f"电费余额不足 {short(new['low_balance_threshold'])} 元时")
        return phrase + "，我会在这里提醒你。持续不足时，每天最多提醒一次。\n关闭提醒：/电费 提醒 关闭"
    if condition(old) != rule:
        return f"以后按“{rule}”提醒，原来的“{condition(old)}”不再生效。提醒发在这里。"
    if old["origin"] != new["origin"]:
        return f"条件还是“{rule}”，之后改在这里提醒你。"
    return f"当前已设置“{rule}”提醒，提醒发在这里。"


def reminder_status(sub, origin):
    if not sub or not sub["enabled"]:
        return "你还没有开启电费提醒。\n按余额：/电费 提醒 10元\n按可用时间：/电费 提醒 2天"
    return (f"你设置的是：{condition(sub)}时提醒。\n发送位置：{destination(sub, origin)}。\n"
            "持续不足时，每天最多提醒一次。\n关闭提醒：/电费 提醒 关闭")


def days_phrase(value):
    days = Decimal(value)
    if days < 1:
        return "预计可用时间不到 1 天"
    rounded = days.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"预计还能用约 {short(rounded)} 天"


def alert(room, state, *, include_days):
    header = f"⚡ {room['building']} · {room['room_number']}"
    actual = Decimal(state["actual_balance"])
    if actual <= 0:
        label = "已低于 0 元" if actual < 0 else "已为 0 元"
        return f"{header}\n平台电费余额{label}，目前为 {actual:.2f} 元，请尽快充值。"
    projected = state.get("estimated_balance")
    if include_days and projected is not None:
        line = f"平台余额还是 {actual:.2f} 元；若扣除这次预估电费，约剩 {Decimal(projected):.2f} 元。"
        if Decimal(projected) <= 0:
            line += "\n预计余额已用尽，建议尽快充值。"
        elif state.get("days") is not None:
            line += f"\n{days_phrase(state['days'])}，建议尽快充值。"
        return header + "\n" + line
    if include_days and state.get("days") is not None:
        return f"{header}\n按近期用电估算，{days_phrase(state['days'])}，建议尽快充值。\n当前余额 {actual:.2f} 元。"
    return f"{header}\n电费还剩 {actual:.2f} 元，记得及时充一下。"


def binding_success(old, new, balance):
    label = f"{new['building']} · {new['room_number']}"
    if old and old["room_id"] == new["room_id"]:
        return f"你已经绑定了{label}，原有提醒保持不变。"
    if old:
        return f"已改绑到{label}。原宿舍的提醒已关闭，新宿舍需要重新设置。"
    return f"已绑定{label}，当前余额 {Decimal(balance):.2f} 元。发送 /电费 就能看面板。"
