#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开口价值评估 v2（salience）：要不要开口、开口说哪一条、说多勤。

为什么重做（2026-09-29，依据是 .whale_said.jsonl 里 68 条真实语料）：
  旧版 material_score 是**等权布尔相加** —— 十几个信号各 +1（磁盘≤5% 才 +2），然后只用
  3 个档位（≥3 / ≥1 / 0）去调间隔，另外把同一个分丢给期望效用闸当"够不够格开口"。
  它不成熟在四处，都能从真实语料里指出来：
    ① **只数件数、不看轻重**：电量 19% 与 3% 同分；"今天有课"与"下节课 20 分钟后开始"
       同分 —— **时间敏感度根本没有进模型**，而"错过就没用了"恰恰是提醒最该抓的东西。
    ② **不看时效窗**：带伞这件事 08:40 之后基本没价值，旧版整天算分；
       反过来"人正要出门"这种窗口一开，雨的价值应该瞬间拉满。
    ③ **不看新鲜度**：同一件"电脑没上报"连着 8 天被报（真实语料：53→62→86→110→134→158
       小时），数字一直在变、**事情没变**，旧版照样天天计满分。
    ④ **一个分干两件事**：既决定"说勤点"，又决定"够不够格开口"，阈值只能折中 ——
       实测结论：UTIL_THRESHOLD 是粗开关、不是频率旋钮。

v2 的模型：**每条候选事实五个属性（各 0—1）**，按类型权重合成"开口价值"，再折算 0—100：

    urgency       多急（错过就没用 / 越拖越糟）
    surprise      相对主人自己历史有多反常（比硬阈值贴人）
    actionability 现在能不能做点什么（说了也做不了的事，价值本来就该低）
    relevance     此刻相不相关（时段窗 / 出门窗 / 睡点窗）
    freshness     比上次播报有没有实质变化（没变化 → 衰减到 0）

    value = weight × (0.35·urgency + 0.25·surprise + 0.20·actionability + 0.20·relevance)
            × freshness

多条候选用**概率或式**合并（1 − ∏(1 − v)），不是相加：相加会饱和（"三件小事"顶到和
"磁盘只剩 1%" 一样高），或式的语义正好是"至少有一件值得说"。

分级与频率解耦：
    urgent（最高一件 ≥ urgent 阈且 urgency 高）→ 紧急通道，绕开节奏闸
    否则 salience ≥ speak 阈 才开口 —— 频率档（低/标准/高）**只动阈值与额度**，
    不动属性算法。"什么值得说"与"说多说少"必须分开调，否则永远调不动。

零依赖、纯函数（除了 time.localtime 的时段判断），可直接被 sim_week 与单测调用。
"""
from __future__ import annotations

import time

# ── 频率档：只动"阈值 / 额度 / 间隔倍率"，不动属性算法 ────────────────────────
MODES = {
    "quiet":  {"label": "静默", "speak": 65, "urgent": 85, "cap": 4,  "gap_mult": 2.50, "chat": 0},
    "low":    {"label": "低频", "speak": 55, "urgent": 72, "cap": 9,  "gap_mult": 1.70, "chat": 0},
    "normal": {"label": "标准", "speak": 38, "urgent": 62, "cap": 12, "gap_mult": 1.00, "chat": 1},
    "high":   {"label": "高频", "speak": 22, "urgent": 50, "cap": 24, "gap_mult": 0.60, "chat": 2},
}
DEFAULT_MODE = "normal"
MODE_FILE = "/home/ubuntu/.hermes/scripts/.whale_freq.json"

# 各属性的权重（合计 1.0）。改这里等于改"她怎么想"，改 MODES 等于改"她多啰嗦"。
W_URGENCY, W_SURPRISE, W_ACTION, W_RELEVANCE = 0.35, 0.25, 0.20, 0.20

# 新鲜度衰减：同类同值连续第 N 次提 → 乘这个系数（第 3 次起彻底不值得说）
FRESH_BY_REPEAT = [1.0, 0.55, 0.25, 0.0]

# 深夜（免打扰时段）也允许走紧急通道的类型：真正的"错过就出事"
NIGHT_URGENT = {"alert", "battery_critical", "disk_critical"}

# 出门窗：这两个时段谈"带伞/穿衣/路上"才有用（早出门 / 下午出门）
LEAVE_WINDOWS = ((6, 30, 9, 30), (13, 0, 15, 0))


def _f(x, lo=0.0, hi=1.0, d=0.0) -> float:
    """安全取值并夹到 [lo, hi]（中枢给的字段可能是 None / 字符串）。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        v = d
    return max(lo, min(hi, v))


def _num(x, d=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _hits_any(text, keys) -> bool:
    t = str(text or "")
    return any(k in t for k in keys)


def in_leave_window(now=None) -> bool:
    """现在是不是"要出门"的窗口（早出门 / 午后出门）。"""
    n = now or time.localtime()
    t = n.tm_hour * 60 + n.tm_min
    return any(a * 60 + am <= t < b * 60 + bm for a, am, b, bm in LEAVE_WINDOWS)


def _minutes_until_class(cls, now=None):
    """下节课还有多少分钟（拿不到就 None）。中枢给的是 start_hour="08时" 这种。"""
    n = now or time.localtime()
    import re
    if not isinstance(cls, (list, tuple)):        # 中枢/离线数据可能给字符串 → 别崩
        return None
    best = None
    for c in cls:
        if not isinstance(c, dict):
            continue
        m = re.search(r"(\d{1,2})", str(c.get("start_hour") or ""))
        if not m:
            continue
        h = int(m.group(1))
        if h < 0 or h > 23:
            continue
        left = (h - n.tm_hour) * 60 - n.tm_min
        if 0 <= left <= 300:
            best = left if best is None else min(best, left)
    return best


def candidates(ctx: dict, now=None) -> list:
    """把"她现在看到的数据"拆成一条条候选事实，各自带五个属性 + 类型权重。

    ★ 这里读的每个键都必须是**中枢 llm_context 真会给的键**（tests/test_material_signals.py
      会静态比对）。字段名对不上时**不报错、只是永远算 0 分** —— 这就出现过一次，
      所以"读的键必须存在"是硬约束，加字段时要同步测试。
    """
    out = []
    wt = ctx.get("weather_today") or {}
    wn = ctx.get("weather_now") or {}
    wd = ctx.get("weather_tomorrow") or {}
    wx_desc = "%s %s %s" % (wt.get("desc") or "", wn.get("desc") or "", wd.get("desc") or "")
    leave = in_leave_window(now)

    def add(kind, why, weight, urg, sur, act, rel):
        out.append({"kind": kind, "why": why, "weight": _f(weight),
                    "urgency": _f(urg), "surprise": _f(sur),
                    "actionability": _f(act), "relevance": _f(rel)})

    # ── 1. 气象预警：最硬的一条，任何时段都值得说 ─────────────────────────────
    if ctx.get("weather_alert"):
        add("alert", "天气预警：%s" % str(ctx.get("weather_alert"))[:20], 1.0, .95, .75, .90, .95)

    # ── 2. 出门相关的天气：只在"要出门"的窗口里拉满 ───────────────────────────
    rain = 0.0
    for k, mult in (("rain_24h", 1.0), ("rain_1h", 3.0)):
        v = _num(wn.get(k))
        if v is not None:
            rain = max(rain, v * mult)
    rainy = rain >= 0.5 or _hits_any(wx_desc, ("雨", "雪", "雷", "冰雹", "雾"))
    if rainy:
        tmax, tmin = _num(wt.get("tmax")), _num(wt.get("tmin"))
        extreme = (tmax is not None and tmax >= 35) or (tmin is not None and tmin <= 2)
        add("weather", "今天%s%s" % (wt.get("desc") or wn.get("desc") or "有降水",
                                     "（要出门）" if leave else ""),
            0.85 if leave else 0.55,
            .95 if leave else .35, .45, .90 if leave else .55, .95 if leave else .40)
        if extreme:
            add("weather_extreme", "极端温度（%s～%s）" % (tmin, tmax), 0.85,
                .80 if leave else .45, .70, .80, .80 if leave else .45)
    elif _num(wt.get("tmax")) is not None and _num(wt.get("tmin")) is not None \
            and (_num(wt["tmax"]) - _num(wt["tmin"])) >= 10:
        add("weather_swing", "温差 %d 度" % int(_num(wt["tmax"]) - _num(wt["tmin"])),
            0.5, .35, .55, .55, .60 if leave else .30)

    # ── 3. 下节课（时间敏感：越近越值得说；已过的时段自然消失）─────────────────
    # ★ 踩过的坑（2026-09-29 一周模拟抓到的）：原来写成 if/elif/elif，
    #   "离下节课 3 小时"这种既不属于 ≤30 也不属于 ≤90 → **一个候选都不产生**，
    #   于是"今天有课"这条彻底消失（旧版至少有 +1 分）→ 早上的课提醒再也没响过。
    #   现在第三档兜住"今天有课但不紧急"。
    cls = ctx.get("classes")
    left = _minutes_until_class(cls, now)
    if left is not None and left <= 30:
        add("class_soon", "下节课 %d 分钟后" % left, 0.95, 1.0, .45, .90, .90)
    elif left is not None and left <= 90:
        add("class_today", "下节课还有 %d 分钟" % left, 0.6, .55, .30, .70, .75)
    elif isinstance(cls, (list, tuple)) and cls:
        add("class_today", "今天 %d 节课" % len(cls), 0.45, .25, .30, .45, .60)

    # ── 4. 电量：按档位细分成三档，而不是"≤20% 就 +1" ─────────────────────────
    b = _num(ctx.get("battery_percent"))
    if b is not None and not ctx.get("battery_charging"):
        if b <= 8:
            add("battery_critical", "手机只剩 %d%% 且没充电" % int(b), 1.0, .95, .55, .95, .90)
        elif b <= 20:
            add("battery_low", "手机 %d%% 没充电" % int(b), 0.75, .60, .40, .85, .70)
        elif b <= 35 and leave:
            add("battery_low", "出门前手机只有 %d%%" % int(b), 0.6, .70, .30, .80, .85)
    for nm, v in (ctx.get("bluetooth_batteries") or {}).items():
        p = _num((v or {}).get("percent")) if isinstance(v, dict) else _num(v)
        if p is not None and p <= 20:
            add("bt_low", "%s 只剩 %d%%" % (nm, int(p)),
                0.7 if p <= 10 else 0.55, .55 if p <= 10 else .40, .40, .85, .65)

    # ── 5. 电脑健康：磁盘是"越拖越糟"的典型（可行动性高），内存是"当下卡不卡"─────
    pc = ctx.get("pc_health") or {}
    df = _num(pc.get("disk_free_percent"))
    if df is not None:
        if df <= 2:
            add("disk_critical", "电脑磁盘只剩 %.1f%%" % df, 1.0, .90, .80, .95, .70)
        elif df <= 5:
            add("disk_low", "电脑磁盘只剩 %.1f%%" % df, 0.85, .70, .70, .90, .60)
        elif df <= 15:
            add("disk_low", "电脑磁盘 %.1f%%" % df, 0.5, .35, .50, .70, .45)
    mp = _num(pc.get("mem_percent"))
    if mp is not None and mp >= 92:
        add("mem_high", "电脑内存 %d%%" % int(mp), 0.6, .45, .60, .55, .55)
    upt = _num(pc.get("uptime_hours"))
    if upt is not None and upt >= 24 * 7:
        add("pc_uptime", "电脑连续开了 %.0f 天" % (upt / 24), 0.35, .25, .40, .45, .35)

    # ── 6. 屏幕 / 分类用量：分档看饱和度，不再"≥300 分钟就算一件事"─────────────
    scr = _num(ctx.get("screen_total_minutes_today"))
    if scr is not None:
        if scr >= 720:
            add("screen_very_long", "屏幕今天 %.1f 小时" % (scr / 60), 0.8, .55, .65, .40, .70)
        elif scr >= 480:
            add("screen_long", "屏幕今天 %.1f 小时" % (scr / 60), 0.6, .35, .45, .45, .60)
        elif scr >= 300:
            add("screen_long", "屏幕今天 %d 分钟" % int(scr), 0.4, .20, .30, .40, .55)
    for cat, m in (ctx.get("screen_usage_minutes_by_category") or {}).items():
        mm = _num(m)
        if mm is not None and mm >= 90:
            # 单个类别冲到 90+ 分钟：视为"可能没刹住"，可行动性中等（提醒歇眼睛）
            add("category_long", "%s %d 分钟" % (cat, int(mm)),
                0.6 if mm >= 180 else 0.5, .30, .55 if mm >= 180 else .40, .45, .60)

    # ── 7. 个人基线反常（中枢算好的 surprise）——比硬阈值贴人 ────────────────────
    sur = ctx.get("surprise") or {}
    if isinstance(sur, dict) and sur:
        n = min(3, len(sur))
        add("anomaly", "相对自己反常 %d 项" % n, 0.75, .35, min(1.0, .55 + .15 * n), .40, .65)
        mn = ctx.get("most_notable") or {}
        if mn.get("what"):
            add("anomaly_top", "最反常：%s %s" % (mn.get("what"), mn.get("text") or ""),
                0.8, .45, .85, .45, .70)

    # ── 8. 睡眠：只在"刚醒"或"该睡"的窗口里说（数据平时没有，允许缺席）──────────
    sm = _num(ctx.get("sleep_minutes"))
    if sm is None:
        _s = ctx.get("sleep")
        sm = _num(_s.get("minutes_rounded")) if isinstance(_s, dict) else _num(_s)
    if sm is not None and sm and sm < 390:
        n = now or time.localtime()
        window = n.tm_hour < 10 or n.tm_hour >= 21
        add("sleep_short", "睡眠只 %d 分钟" % int(sm), 0.8,
            .70 if window else .25, .60, .55, .90 if window else .25)

    # ── 9. 快递 / 订单 / 待办 / 闹钟 ─────────────────────────────────────────
    dv = _num(ctx.get("deliveries_7d")) or 0
    if dv and (ctx.get("orders_7d") or 0):
        add("order", "近 7 天 %d 件到、%d 笔下单" % (dv, int(ctx.get("orders_7d"))), 0.5, .25, .35, .40, .45)
    elif dv:
        add("order", "近 7 天 %d 件快递到" % int(dv), 0.5, .30, .35, .55, .50)
    todo = _num(ctx.get("todo_count")) or 0
    if todo:
        add("todo", "有 %d 条待办" % int(todo), 0.5, .40, .35, .60, .55)
    al = ctx.get("next_alarm_at")
    if al:
        try:
            import datetime as _dt
            cur = _dt.datetime.now().astimezone() if now is None else _dt.datetime(
                now.tm_year, now.tm_mon, now.tm_mday, now.tm_hour, now.tm_min).astimezone()
            t = _dt.datetime.fromisoformat(str(al))
            mins = (t - cur).total_seconds() / 60
            if 0 <= mins <= 90:
                add("alarm_soon", "%d 分钟后的闹钟" % int(mins), 0.7, .85, .30, .50, .85)
        except Exception:
            pass

    # ── 10. 听歌 / 游戏：弱信号，"连续循环"才算有话说 ─────────────────────────
    li = ctx.get("listening_now") or {}
    if isinstance(li, dict) and li.get("title"):
        add("listening", "在听 %s" % str(li.get("title"))[:18], 0.35, .20, .35, .15, .45)
    tr = _num(ctx.get("tracks_today_count")) or 0
    if tr >= 60:
        add("listening_long", "今天听了 %d 首" % int(tr), 0.4, .20, .45, .25, .40)

    # ── 11. 数据缺口：★ 降噪的重点 ───────────────────────────────────────────
    #     "电脑今天还没上报"第一天是信息，第八天是噪音（真实语料：连报 8 天、数字从 53 涨到 158）。
    #     所以给低可行动性 + 低紧迫，靠 freshness 衰减把它压下去；真正的"刚断"才值得说。
    dh = ctx.get("data_health") or {}
    if isinstance(dh, dict) and dh:
        missing = [k for k, v in dh.items() if str((v or {}).get("verdict") or "") == "missing"]
        if missing:
            add("data_gap", "%s 今天没上报" % "/".join(list(missing)[:2]), 0.45,
                .25, .40, .25, .45)

    # 兜底：什么都没命中 → 只有"闲聊额度"（不产生候选，由调用方按额度处理）
    return out


def value_of(c: dict, repeat: int = 0) -> float:
    """单条候选的开口价值（0—1）。repeat = 同类同值已经连着说过几次。"""
    base = (W_URGENCY * c["urgency"] + W_SURPRISE * c["surprise"]
            + W_ACTION * c["actionability"] + W_RELEVANCE * c["relevance"])
    fresh = FRESH_BY_REPEAT[min(max(0, int(repeat)), len(FRESH_BY_REPEAT) - 1)]
    return _f(c["weight"] * base * fresh)


def bucket_of(c: dict) -> str:
    """把候选折算成"同一个值桶"，用来数"同一件事说过几次"。

    桶必须粗一点：电量 19%→10%、磁盘 3.2%→3%、屏幕 512 分钟→8 小时 ——
    数字抖一下就当成新事，就会变成"同一件事换着说法说 23 遍"（真实踩过）。
    """
    import re
    why = str(c.get("why") or "")
    nums = [int(x) for x in re.findall(r"\d+", why)]
    if c["kind"].startswith("battery"):
        return "battery:%d0%%" % (nums[0] // 10 if nums else 0)
    if c["kind"].startswith("disk"):
        return "disk:%d%%" % (nums[0] // 5 * 5 if nums else 0)
    if c["kind"].startswith("screen"):
        mins = nums[0]
        if "小时" in why and nums:
            mins = nums[0] * 60
        return "screen:%dh" % (mins // 120 * 2)
    if c["kind"].startswith("bt_"):
        return "bt:%d0%%" % (nums[0] // 10 if nums else 0)
    if c["kind"] in ("class_soon", "class_today"):
        return "class:%s" % (nums[0] // 30 * 30 if nums else 0)
    return "%s:%s" % (c["kind"], nums[0] if nums else "")


def evaluate(ctx: dict, mode: str = DEFAULT_MODE, repeats: dict = None,
             said_today: int = 0, now=None) -> dict:
    """算出"现在值不值得开口、说哪一条"。返回可打印、可落库的结构化结果。

    `now` 可注入（默认取系统时间）—— 单测/模拟要确定性，不能靠"跑的时候正好几点"。
    """
    m = MODES.get(mode) or MODES[DEFAULT_MODE]
    reps = repeats or {}
    items = []
    for c in candidates(ctx, now):
        key = bucket_of(c)
        v = value_of(c, int(reps.get(key, 0)))
        d = dict(c)
        d["bucket"] = key
        d["value"] = round(v, 3)
        d["repeat"] = int(reps.get(key, 0))
        items.append(d)
    items.sort(key=lambda x: -x["value"])
    # 概率或式合并：1 − ∏(1 − v)。语义是"至少有一件值得说"，天然不会饱和。
    prod = 1.0
    for it in items:
        prod *= (1.0 - it["value"])
    score = int(round((1.0 - prod) * 100))
    top = items[0] if items else None
    urgent = bool(top and top["value"] * 100 >= m["urgent"]
                  and top["urgency"] >= 0.80 and (top["kind"] != "data_gap"))
    if urgent and not in_leave_window(now) and top["kind"] in ("weather", "weather_extreme", "weather_swing"):
        urgent = False                      # 非出门窗的天气，再准也不该"紧急"
    say = bool(urgent) or (score >= m["speak"] and said_today < m["cap"])
    why = [it["why"] for it in items[:4]]
    if top:
        why.append("最高价值：%s（%.2f%s）" % (top["kind"], top["value"],
                                            "，已说过 %d 次" % top["repeat"] if top["repeat"] else ""))
    return {"score": score, "mode": mode, "mode_label": m["label"], "items": items,
            "top": top, "urgent": urgent, "say": say,
            "threshold": m["urgent"] if urgent else m["speak"],
            "cap": m["cap"], "gap_mult": m["gap_mult"], "chat": m["chat"],
            "said_today": int(said_today), "why": why}


def note_said(state: dict, ev: dict) -> dict:
    """开口之后记一笔：同类同值的重复计数 +1（`state` 由调用方持久化）。

    ★ 计数写在 `state["repeats"]` **里面**（调用方按 `{"repeats": {...}}` 存取）。
      第一版写在顶层，导致"重复计数永远读不到" → 新鲜度衰减形同虚设。
    """
    top = ev.get("top") or {}
    st = dict(state or {})
    reps = dict(st.get("repeats") or {})
    if top.get("bucket"):
        reps[top["bucket"]] = int(reps.get(top["bucket"], 0)) + 1
        st["repeats"] = reps
        return st
    return state or {}


def decide(ctx: dict, state: dict = None, mode: str = DEFAULT_MODE,
           said_today: int = 0, p_accept: float = None, util_threshold: float = 0.67,
           now=None, boost: float = 0.0, cap_eff: float = None) -> dict:
    """★ 唯一入口：salience（值不值得）× 节奏（今天说够没有）× 后验（说了有没有用）。

    与旧实现的区别：三件事**分开判、理由分开写**，不再让一个整数同时干两件事。
    后验只用来"收紧/放宽非紧急的那一档"，**永远不能压掉紧急通道** ——
    真实语料里"电脑磁盘只剩 1.2%"这种话是不该被一个 0.5 的先验挡掉的。
    """
    ev = evaluate(ctx, mode=mode, repeats=(state or {}).get("repeats") or {},
                  said_today=said_today, now=now)
    m = dict(MODES.get(mode) or MODES[DEFAULT_MODE])
    # ★ 活跃窗口的**正确杠杆**：调**开口阈值**，不是调间隔。
    #   实测（2026-09-29，14 天模拟）：把窗口乘到 next_gap 上完全无效（对齐 0.599→0.599），
    #   因为"说不说"真正由阈值/每日上限/话题台账决定，间隔被它们吸收了 ✗
    #   boost>0（活跃窗口）→ 阈值下调，更愿意开口；boost<0（死区）→ 上调，更闭嘴。
    #   只作用于**非紧急**那一档：紧急通道永远不受窗口影响（PRISM：代价敏感，不能为了"时机"
    #   把该说的紧急事压掉）。
    if boost:
        m["speak"] = max(5, min(95, int(round(m["speak"] * (1.0 - float(boost))))))
        ev["speak_adj"] = m["speak"]
    # ★ 额度分配（token bucket）：**此刻允许说几条**可以随时间窗变化，而不是整天一个常数。
    #   依据 Liao 2020 Personalized HeartSteps（DOI 10.1145/3381007）：一天只做 N 次决策、
    #   按时刻在线学习。这是**直接打在 binding constraint 上**的杠杆（阈值/间隔都被它吸收）。
    if cap_eff is not None:
        m["cap"] = max(1.0, float(cap_eff))
        ev["cap_adj"] = round(float(cap_eff), 2)
    if ev["urgent"]:
        ev["gate"] = "紧急通道（%s）：绕开节奏闸与后验" % (ev["top"] or {}).get("kind", "")
        return ev
    if said_today >= m["cap"]:
        ev["say"] = False
        ev["gate"] = "今天已说 %d 条（%s档上限 %d）→ 只留紧急通道" % (said_today, m["label"], m["cap"])
        return ev
    if ev["score"] < m["speak"]:
        ev["say"] = False
        ev["gate"] = "开口价值 %d < %s档阈值 %d（最值得说的只有 %s）" % (
            ev["score"], m["label"], m["speak"], (ev["top"] or {}).get("why", "无"))
        return ev
    if p_accept is not None and p_accept < util_threshold:
        # 后验偏低 → 只在"料很足"时才说（阈值上浮 40%），但不再是一刀切闭嘴
        need = min(95, int(round(m["speak"] * 1.4)))
        if ev["score"] < need:
            ev["say"] = False
            ev["gate"] = "p(接受)=%.2f 偏低 → 要求价值 ≥%d，现在 %d" % (p_accept, need, ev["score"])
            return ev
        ev["gate"] = "p(接受)=%.2f 偏低，但价值 %d ≥%d" % (p_accept, ev["score"], need)
        return ev
    ev["gate"] = "开口价值 %d ≥ %s档阈值 %d" % (ev["score"], m["label"], m["speak"])
    return ev


def load_mode(path: str = MODE_FILE) -> str:
    """读频率档（坏文件/非法值一律回落 standard，绝不因此不说话）。"""
    import json
    try:
        with open(path, encoding="utf-8") as f:
            v = (json.load(f) or {}).get("mode")
        return v if v in MODES else DEFAULT_MODE
    except Exception:
        return DEFAULT_MODE


def save_mode(mode: str, path: str = MODE_FILE) -> bool:
    """写频率档（原子写：临时文件 + replace，避免读到半截）。"""
    import json
    import os
    if mode not in MODES:
        return False
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"mode": mode, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, f,
                      ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def describe(mode: str = None) -> str:
    m = MODES.get(mode or DEFAULT_MODE) or MODES[DEFAULT_MODE]
    return ("%s档：开口阈值 %d / 紧急 %d / 每天上限 %d 条 / 间隔 ×%.2f%s"
            % (m["label"], m["speak"], m["urgent"], m["cap"], m["gap_mult"],
               " / 闲聊额度 %d 条" % m["chat"] if m["chat"] else " / 不闲聊"))


if __name__ == "__main__":
    import argparse
    import json as _json
    ap = argparse.ArgumentParser(description="开口价值评估（调试/调参用）")
    ap.add_argument("--mode", default=None, help="low / normal / high")
    ap.add_argument("--ctx", default="", help="ctx 的 JSON 文件路径（默认去中枢拉 llm-preview）")
    ap.add_argument("--repeats", default="{}", help="重复计数 JSON")
    ap.add_argument("--show-modes", action="store_true")
    a = ap.parse_args()
    if a.show_modes:
        for k in MODES:
            print("  %-7s %s" % (k, describe(k)))
        raise SystemExit(0)
    if a.ctx:
        ctx = _json.loads(open(a.ctx, encoding="utf-8").read())
    else:
        import os
        import ssl
        import urllib.request
        hub = os.getenv("WHALE_HUB", "")
        tok = os.getenv("WHALE_TOKEN", "")
        ca = os.getenv("WHALE_CA", "/home/ubuntu/hub/tls/hub.crt")
        r = urllib.request.Request(hub + "/llm-preview")
        r.add_header("X-Token", tok)
        ctx = _json.loads(urllib.request.urlopen(
            r, timeout=20, context=ssl.create_default_context(cafile=ca)).read().decode()
        ).get("would_send_to_model") or {}
    rep = _json.loads(a.repeats)
    mode = a.mode or load_mode()
    ev = decide(ctx, {"repeats": rep}, mode=mode, said_today=0)
    print("  模式：%s（%s）" % (mode, describe(mode)))
    print("  开口价值 %d ｜ 紧急=%s ｜ 说=%s" % (ev["score"], ev["urgent"], ev["say"]))
    print("  闸门：%s" % ev.get("gate"))
    for it in ev["items"][:8]:
        print("   %-16s v=%.3f  urg=%.2f sur=%.2f act=%.2f rel=%.2f  rep=%d  %s"
              % (it["kind"], it["value"], it["urgency"], it["surprise"],
                 it["actionability"], it["relevance"], it["repeat"], it["why"][:30]))
