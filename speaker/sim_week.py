#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一周模拟：按"正常使用"的作息跑 N 天，看她的提醒到底正常不正常。

为什么要它：线上连着出过几次故障（思考 token 吃光预算 / 判重跨天 / 回执失败重发 /
补发过期提醒），每次都是**事后**从日志里翻出来的 ✗ —— 这个工具让同类问题**事前**暴露 ✓

它不重写一套逻辑 ✗，而是**直接调用说话层里那几个真函数**：
  · next_gap()                  → 这次该隔多久（含频率档/料分/在用/沉默的自适应）
  · material_score()            → 料分（★ 2026-09-29 起 = whale_salience 评分引擎）
  · salience_decide()           → 值不值得开口 / 走不走紧急通道（新引擎的唯一入口）
  · topic_of() / topic_already_said_today() / topic_mark_said()   → 话题台账（事前一票否决）
  · too_similar() / fingerprint()                                  → 事实层判重
  · _in_window()                → 时段窗口（睡前/早间）
  · QUIET                       → 免打扰时段
  · daily_cap()                 → 每日上限（证据 + 频率档）
（不生成文案：那要调模型，N 天几百次没必要 ✗）

★★ 2026-09-29 修掉的一个"模拟可信度"问题：
  旧版喂给 next_gap / 决策的 ctx 是**自己编的** `{"hour","screen","social","facts"}`，
  而料分读的是中枢真字段（weather_today / pc_health / classes …）→ 字段名全对不上，
  于是"真料分"在这套模拟里**恒等于 0**，模拟测的其实是节奏循环 + 一个手算的假料分 ✗
  ⇒ 现在 ctx 按**中枢 llm-preview 的真实形状**构造，并直接调真 material_score /
    salience_decide。顺带把 time.localtime() 也一起伪造 —— 否则"早上/睡前"时间带
    永远按**跑模拟的那一刻**算，7 天模拟的时间带全是同一个 ✗

用法：
    python3 sim_week.py                                   # 默认 7 天 · 标准档 · 新引擎
    python3 sim_week.py --days 14 --mode low              # 低频档
    python3 sim_week.py --engine legacy                   # 对照：旧料分 + 期望效用闸
    python3 sim_week.py --all                             # 一次跑：旧引擎 + 新引擎三档 + 命中率对比
"""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import random
import sys
import tempfile
import time as _time
from datetime import datetime, timedelta

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    import whale_windows as WW  # 活跃窗口学习器（同一目录）
except Exception:                        # 没有它也能跑（窗口不启用）
    WW = None


class FakeDatetime(datetime):
    """把说话层内部的"现在"换成模拟时间。

    为什么必须：说话层的台账/时段窗口/免打扰都用 datetime.now(TZ) ✓
    模拟跑的是 09-21~27，而真实今天是 09-24 ✗ → 台账永远不重置 → 后面几天全被当成
    "今天说过了" → 一周模拟只剩 3 条 ✗（第一版就栽在这 ✓）
    """
    _now = None

    @classmethod
    def now(cls, tz=None):
        n = cls._now or datetime.now()
        return n if tz is None else n.astimezone(tz)


class FakeTime:
    """连 time.localtime()/strftime() 一起伪造（时间带、日志时间都要跟着走）。"""
    _now = None

    def localtime(self, *a):
        n = self._now or datetime.now()
        return n.timetuple()

    def strftime(self, fmt, *a):
        n = self._now or datetime.now()
        return n.strftime(fmt) if not a else _time.strftime(fmt, *a)

    def time(self):
        return (self._now or datetime.now()).timestamp()

    def sleep(self, *a):
        return None


def set_clock(sp, when):
    FakeDatetime._now = when
    FakeTime._now = when
    sp.datetime = FakeDatetime
    sp.time = FakeTime()


def load_speaker(path: pathlib.Path):
    """把说话层当模块加载（只读它的函数，不启动它的主循环）。"""
    spec = importlib.util.spec_from_file_location("sp_sim", path)
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


# ─────────────────────────── 一天的数据（形状与中枢 llm-preview 一致）
COURSES = [("08时", "电气控制与PLC", "13-908"), ("09时", "电力系统分析", "7-305"),
           ("14时", "计算机控制技术", "7-515")]


def day_plan(d: int, weekday: bool):
    """每天有几件"该说的事"（真相清单，用来算命中率）。"""
    ev = []
    if weekday or d % 3 == 0:
        ev.append({"kind": "class_soon", "hour": 7, "why": "第一节 8:00"})
    if d == 2:
        ev.append({"kind": "disk_critical", "hour": 3, "why": "磁盘 1.2%"})
    if d == 3:
        ev.append({"kind": "battery_critical", "hour": 8, "why": "电量 6% 未充电"})
    if d == 4:
        ev.append({"kind": "weather", "hour": 7, "why": "出门前雷阵雨"})
    if d == 5:
        ev.append({"kind": "weather_alert", "hour": 15, "why": "暴雨预警"})
    if d % 2 == 0:
        ev.append({"kind": "anomaly_top", "hour": 20, "why": "短视频比平时多 120%"})
    if d >= 3:
        ev.append({"kind": "data_gap", "hour": 9, "why": "电脑没上报（第 N 天）"})
    return ev


def day_ctx(sp, day_index: int, weekday: bool, rng: random.Random, date):
    """生成这一天每个小时"她看到的数据"（真字段名，删掉谁她会真的看不到）。"""
    plan = day_plan(day_index, weekday)
    rows, screen, social = [], (60 if not weekday else 40), (30 if not weekday else 15)
    wake = 7 if weekday else 10
    classes = ([{"start_hour": h, "periods": "第1-2节", "kind": "上课"} for h, _n, _r in COURSES]
               if weekday else [])
    rainy = any(e["kind"].startswith("weather") for e in plan)
    alert = any(e["kind"] == "weather_alert" for e in plan)
    for h in range(24):
        if h < wake:
            rows.append((h, None))              # 睡着：不给 ctx
            continue
        screen += rng.randint(18, 52)
        if rng.random() < 0.35:
            social += rng.randint(10, 45)
        todays = [e for e in plan if e["hour"] == h]
        ctx = {
            "date": date.strftime("%Y-%m-%d"),
            "weekday": "周一" if date.weekday() == 0 else date.strftime("%a"),
            "classes": classes,
            "weather_today": {"city": "广州", "date": date.strftime("%m/%d"),
                              "desc": "雷阵雨" if rainy else "多云",
                              "tmax": 34.0 if rainy else 33.0, "tmin": 25.0},
            "weather_now": {"city": "广州", "desc": "小雨" if rainy else "多云",
                            "humidity": 90.0,
                            "rain_1h": 0.6 if (rainy and 6 <= h <= 10) else 0.0,
                            "rain_24h": 2.0 if rainy else 0.0},
            "weather_alert": "暴雨黄色预警" if (alert and h >= 15) else None,
            "screen_total_minutes_today": min(screen, 960),
            "screen_usage_minutes_by_category": ({"短视频/视频": min(social * 2, 300)}
                                                 if screen > 120 else {}),
            "battery_percent": 6 if any(e["kind"] == "battery_critical" for e in todays) else
                               (36 if rng.random() < 0.3 else 82),
            "battery_charging": False,
            "data_health": ({"电脑": {"verdict": "missing", "note": "今天还没有任何上报"}}
                            if any(e["kind"] == "data_gap" for e in todays) else {}),
            "deliveries_7d": 1 if day_index % 3 == 1 else 0,
            "orders_7d": 0,
        }
        if any(e["kind"] == "disk_critical" for e in todays):
            ctx["pc_health"] = {"disk_free_percent": 1.2, "mem_percent": 61}
        elif rng.random() < 0.2:
            ctx["pc_health"] = {"disk_free_percent": 12.5, "mem_percent": 58}
        if any(e["kind"] == "anomaly_top" for e in todays) and h >= 18:
            ctx["surprise"] = {"短视频/视频": "x"}
            ctx["most_notable"] = {"what": "短视频/视频", "text": "比平时多 120%"}
        rows.append((h, ctx))
    return rows, plan


def simulate(sp, days: int, seed: int, engine: str = "new", mode: str = "normal",
             quiet_ok: bool = True, learn_windows: bool = False, learn_after: int = 7):
    rng = random.Random(seed)
    start = datetime(2026, 9, 21, 0, 0)          # 周一
    log = []
    stats = {"msgs": 0, "dups": 0, "quiet": 0, "window_bad": 0, "per_day": {},
             "gate_blocked": 0, "cap_blocked": 0, "urgent": 0, "blocked": {},
             "speak_h": {}, "act_h": {}, "win": ""}
    hits, truths = {}, {}
    last_said = None
    day_slots = {}
    for d in range(days):
        date = start + timedelta(days=d)
        weekday = date.weekday() < 5
        # ★ 边学边用：第 learn_after 天起，用**前面几天**的活跃数据学窗口并启用
        if learn_windows and WW is not None and d >= learn_after and day_slots:
            cfg = WW.learn(day_slots)
            win_path = pathlib.Path(tempfile.mkdtemp(prefix="simwin-")) / "w.json"
            WW.save_cfg(cfg, str(win_path))
            sp.WIN_PATH = win_path
            sp.WINDOWS_MODE = "on"
            stats["win"] = "、".join("%s-%s" % (w["start"], w["end"]) for w in cfg["windows"])
            try:
                sp.windows_cfg()
            except Exception:
                pass
        sp.SAID_TODAY.clear()
        try:
            sp.sal_load()          # 提前建好状态（首次）
        except Exception:
            pass
        rows, plan = day_plan_all(sp, d, weekday, rng, date)
        said_count = 0
        spoken_kinds, spoken_texts = set(), []
        _prev_screen = 0.0
        _min_slots, _dens_slots = [0.0] * 96, [0.0] * 96
        for _h, _c in rows:
            if _c is None:
                continue
            _scr = float(_c.get("screen_total_minutes_today") or 0)
            _inc = max(0.0, _scr - _prev_screen)
            _prev_screen = _scr
            stats["act_h"][str(_h)] = stats["act_h"].get(str(_h), 0) + _inc
            for _k in range(4):
                _min_slots[_h * 4 + _k] = _inc / 4.0
                _dens_slots[_h * 4 + _k] = 3 if _inc > 20 else 0
        day_slots[date.strftime("%Y-%m-%d")] = {"minutes": _min_slots, "density": _dens_slots}
        for h, ctx in rows:
            for minute in range(0, 60, 5):
                now = date.replace(hour=h, minute=minute)
                set_clock(sp, now)
                if ctx is None:
                    continue
                if h >= sp.QUIET[0] or h < sp.QUIET[1]:
                    stats["quiet"] += 1
                    continue
                st = sp.pace()
                if engine == "legacy":
                    # 旧路径：手算料分 + 期望效用闸（同时把料分换成旧实现，对照"旧引擎"）
                    gap, _why = sp.next_gap(ctx)
                    if last_said is not None and (now - last_said).total_seconds() < gap:
                        continue
                    m = sp.material_score(ctx)[0]
                    ok_gate, _gate_why = sp.utility_gate(m)
                    if not ok_gate:
                        stats["gate_blocked"] += 1
                        stats["blocked"]["gate"] = stats["blocked"].get("gate", 0) + 1
                        continue
                    if said_count >= sp.daily_cap(st):
                        stats["cap_blocked"] += 1
                        continue
                    text, kind = _what_she_says(sp, ctx, "legacy")
                else:
                    # ★ 顺序与真实 maybe_speak 一致：先算值不值得 → 紧急绕开节奏闸 → 不紧急再看间隔
                    ev = sp.salience_decide(ctx, said_today=said_count, now=now.timetuple())
                    if ev is None:
                        continue
                    if not ev["say"]:
                        stats["gate_blocked"] += 1
                        key = "日限" if said_count >= ev["cap"] else "价值不够"
                        stats["blocked"][key] = stats["blocked"].get(key, 0) + 1
                        continue
                    if ev["urgent"]:
                        if last_said is not None and \
                                (now - last_said).total_seconds() < sp.URGENT_MIN_GAP:
                            continue
                    else:
                        gap, _why = sp.next_gap(ctx)
                        if last_said is not None and (now - last_said).total_seconds() < gap:
                            continue
                    top = ev.get("top") or {}
                    text, kind = top.get("why") or "", top.get("kind") or ""
                if not text:
                    continue
                if not sp._in_window("proactive"):
                    stats["window_bad"] += 1
                    continue
                if sp.topic_already_said_today(text):
                    stats["dups"] += 1
                    log.append((now, "跳过-台账", text))
                    continue
                if sp.too_similar(text):
                    stats["dups"] += 1
                    log.append((now, "跳过-相似", text))
                    continue
                said_count += 1
                stats["msgs"] += 1
                stats["speak_h"][str(h)] = stats["speak_h"].get(str(h), 0) + 1
                if engine != "legacy" and ev.get("urgent"):
                    stats["urgent"] += 1
                spoken_kinds.add(kind)
                spoken_texts.append(text)
                sp.topic_mark_said(text)
                sp.remember(text)
                if engine != "legacy":
                    try:
                        sp.sal_note(ev)
                    except Exception:
                        pass
                last_said = now
                log.append((now, "开口", text))
        # ── 当天命中率：这件事**当天**有没有被说过（不限定同一小时 —— 那是"及时性"，
        #    这里量的是"该说的说了没有" ✓）
        _blob = " ".join(spoken_texts)
        for e in plan:
            truths[e["kind"]] = truths.get(e["kind"], 0) + 1
            if e["kind"] in spoken_kinds or any(w in _blob for w in KIND_KW.get(e["kind"], ())):
                hits[e["kind"]] = hits.get(e["kind"], 0) + 1
        stats["per_day"][date.strftime("%m-%d %a")] = said_count
        stats.setdefault("topics", []).append(len(set(sp.topic_of(t)[0] for t in spoken_texts) - {""}))
    if stats.get("topics"):
        stats["topics_avg"] = sum(stats["topics"]) / len(stats["topics"])
    stats["align"] = _alignment(stats["speak_h"], stats["act_h"])
    return log, stats, hits, truths


def _alignment(speak_h, act_h):
    """她开口的小时分布 vs 他活跃的小时分布：余弦相似度 + 落在 top6 活跃小时的比例。

    ★ 这是"时机对不对"唯一可量化的验收指标（比"条数"有意义得多）。
    """
    import math as _m
    hh = [str(h) for h in range(24)]
    a = [max(0.0, float(act_h.get(h, 0))) for h in hh]
    s = [float(speak_h.get(h, 0)) for h in hh]
    sa, ss = sum(a), sum(s)
    if sa <= 0 or ss <= 0:
        return {"cos": None, "top6_pct": None}
    cos = sum(x * y for x, y in zip(a, s, strict=False)) / (_m.sqrt(sum(x * x for x in a)) * _m.sqrt(sum(x * x for x in s)))
    top6 = sorted(range(24), key=lambda i: -a[i])[:6]
    return {"cos": round(cos, 3), "top6_pct": round(100.0 * sum(s[i] for i in top6) / ss)}


def day_plan_all(sp, d, weekday, rng, date):
    return day_ctx(sp, d, weekday, rng, date)


KIND_KW = {
    "class_soon": ("课", "节"),
    "disk_critical": ("磁盘", "盘"),
    "battery_critical": ("电量", "充电"),
    "weather": ("雨", "伞", "天气"),
    "weather_alert": ("预警",),
    "anomaly_top": ("反常", "比平时"),
    "data_gap": ("没上报", "没同步", "失联"),
}


def _what_she_says(sp, ctx, engine="new"):
    """她会说什么（取引擎选出的那一件事实；不生成文案 ✓）。"""
    try:
        if engine == "legacy":
            # 旧引擎没有"候选排序"，只能拿最关键的一条当代表（按旧料分的顺序）
            m, why = sp.material_score(ctx)
            return (why[0] if why else ""), ""
        ev = sp._sal_mod().evaluate(ctx, mode="normal")
        top = ev.get("top") or {}
        return top.get("why") or "", top.get("kind") or ""
    except Exception:
        return "", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--speaker", default=str(HERE / "whale_speaker.py"))
    ap.add_argument("--engine", default="new", choices=["new", "legacy"])
    ap.add_argument("--mode", default="normal", choices=["low", "normal", "high"])
    ap.add_argument("--all", action="store_true", help="跑对照实验：旧引擎 + 新引擎三档")
    ap.add_argument("--check", action="store_true",
                    help="回归模式：不变量被破坏就退出码非 0（CI 用）")
    a = ap.parse_args()

    if a.all:
        return run_all(a)
    sp = _fresh(a)
    return one(sp, a.days, a.seed, a.engine, a.mode, a.check)


def _fresh(a):
    """每次跑都重建 speaker 模块 + 隔离状态目录（绝不碰真实台账）。"""
    sp = load_speaker(pathlib.Path(a.speaker))
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="simweek-"))
    sp.SENT_PATH = tmp / "sent.jsonl"
    sp.RECENT_PATH = tmp / "said.jsonl"
    sp.SAL_PATH = tmp / "salience.json"
    sp.WIN_SHADOW = tmp / "win_shadow.jsonl"      # 绝不写真实影子日志
    sp.WIN_PATH = tmp / "windows.json"
    sp.WINDOWS_MODE = "off"
    sp.FREQ_PATH = tmp / "freq.json"
    sp.PACE_PATH = tmp / "pace.json"
    if a.engine == "legacy":
        # 对照：把料分换回旧实现（这才是"旧引擎"的真实含义）
        sp.material_score = sp._material_score_legacy
        try:
            sp._sal_mod = lambda: None
        except Exception:
            pass
    sal = sp._sal_mod()
    if sal is not None:
        sal.save_mode(a.mode, str(sp.FREQ_PATH))
    return sp


def one(sp, days, seed, engine, mode, check=False, quiet=True):
    print(f"  引擎 {engine} ｜ 频率档 {mode} ｜ {days} 天 ｜ 种子 {seed}")
    log, stats, hits, truths = simulate(sp, days, seed, engine, mode)
    _report(days, seed, stats, hits, truths, log)
    problems = _invariants(days, stats)
    for pr in problems:
        print("  ✗", pr)
    print("  判定:", "✗ 有问题（见上）" if problems else "✓ 时段不错位、不重复打扰、条数在区间内")
    return 1 if (check and problems) else 0


def _invariants(days, stats):
    per_day = stats["msgs"] / max(1, days)
    problems = []
    if stats["window_bad"] > 0:
        problems.append(f"时段窗口违规 {stats['window_bad']} 次（会'早上说晚上的事' ✗）")
    if per_day > 20.0:
        problems.append(f"平均每天 {per_day:.1f} 条（太密 ✗ 会烦人）")
    if stats["quiet"] == 0:
        problems.append("免打扰时段一次都没跳过（时间判断可能坏了 ✗）")
    return problems


def _report(days, seed, stats, hits, truths, log, brief=False):
    if not brief:
        for day, n in stats["per_day"].items():
            print(f"   {day}  {n:>2} 条  {'█' * n}")
    total = stats["msgs"]
    print(f"   合计开口 {total} 条（{days} 天，平均 {total / days:.1f} 条/天）"
          f" ｜ 紧急通道 {stats['urgent']} 条")
    print(f"   被拦：{('、'.join('%s %s 次' % (k, v) for k, v in stats['blocked'].items())) or '无'}"
          f" ｜ 台账/相似度拦下 {stats['dups']} 次")
    ks = [k for k in truths]
    if ks:
        line = "  ".join("%s %s/%s" % (k, hits.get(k, 0), truths[k]) for k in sorted(ks))
        print(f"   重要事件命中：{line}")
        print(f"   命中率 {sum(hits.values())}/{sum(truths.values())}"
              f" = {100.0 * sum(hits.values()) / max(1, sum(truths.values())):.0f}%")
    if stats.get("topics_avg") is not None:
        print(f"   每天谈到的话题数 {stats['topics_avg']:.1f}（越多说明越不只是念同一个数）")
    al = stats.get("align") or {}
    if al.get("cos") is not None:
        print(f"   时机对齐：余弦 {al['cos']} ｜ 开口落在活跃 top6 小时 {al['top6_pct']}%")
    if stats.get("win"):
        print(f"   学到的活跃窗口：{stats['win']}")
    if not brief:
        print("  ── 抽样 ──")
        for ts, kind, txt in log[:12]:
            print(f"   {ts.strftime('%m-%d %H:%M')}  [{kind}] {txt[:52]}")
    return {"msgs": total, "per_day": total / days, "urgent": stats["urgent"],
            "topics": stats.get("topics_avg", 0.0),
            "hits": sum(hits.values()), "truths": sum(truths.values())}


def run_all(a):
    """对照实验：旧引擎 vs 新引擎三档。一条命令给出"改之前/改之后"的分档曲线。"""
    print("  " + "=" * 74)
    print(f"  对照实验：{a.days} 天 · 种子 {a.seed} ｜ 同一套作息数据")
    print("  " + "=" * 74)
    rows = []
    for label, eng, mode, lw in (("旧引擎（等权料分+期望效用闸）", "legacy", "normal", False),
                                 ("新引擎·低频", "new", "low", False),
                                 ("新引擎·标准", "new", "normal", False),
                                 ("新引擎·高频", "new", "high", False),
                                 ("标准档+活跃窗口(边学边用)", "new", "normal", True)):
        sp = _fresh(argparse.Namespace(speaker=a.speaker, engine=eng, mode=mode))
        log, stats, hits, truths = simulate(sp, a.days, a.seed, eng, mode,
                                            learn_windows=lw, learn_after=7)
        r = _report(a.days, a.seed, stats, hits, truths, log, brief=True)
        r["label"] = label
        r["dups"] = stats["dups"]
        r["align"] = (stats.get("align") or {})
        rows.append(r)
        print()
    print("  " + "=" * 74)
    print("  %-28s %7s %9s %8s %8s %8s" % ("引擎/档位", "条/天", "重要事件", "话题/天", "时机对齐", "top6%"))
    for r in rows:
        al = r.get("align") or {}
        print("  %-28s %7.1f %9s %8.1f %8s %8s" % (
            r["label"], r["per_day"], "%d/%d" % (r["hits"], r["truths"]),
            r["topics"], al.get("cos", "-"), al.get("top6_pct", "-")))
    print("  " + "=" * 74)
    print("  读法：『条/天』是吵不吵；『命中率』是该说的有没有说；『判重拦截』越低越好")
    return 0


if __name__ == "__main__":
    sys.exit(main())
