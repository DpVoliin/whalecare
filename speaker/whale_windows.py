#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""活跃窗口学习器（说话时机 v4）：从真实使用数据里学出"他什么时候是活的"。

设计依据（都核过，见 docs 或主人那份《说话时机-可行性与文献》）：
  · arXiv:2608.04416（EOPA，2026-08）：**temporal preference anchors + evidence-bearing activity
    prototypes**，用"用户先验平滑 + 不确定性缩放"融合出「说/不说」；只用在线反馈更新，
    **不需要 LLM 推理、不需要重训**。本模块就是它的个人化精简版（纯标准库）。
  · arXiv:2602.01532（PRISM）：代价敏感的选择性干预 —— 我们保留"紧急通道不受窗口限制"。
  · Iqbal & Bailey CHI'08：断点处打扰代价更低 → 活跃槽是"断点"的廉价代理。
  · Pejovic UbiComp'14（InterruptMe）：手机数据能预测可打扰性，但**个体差异极大** → 必须个人化学习。
  · 实测（主人 12 天真实数据）：窗口内外的活跃区分度只有 **1.22×（分钟信号）/ 1.43×（密度信号）**
    ⇒ **活跃度只能当乘子（±30%），绝不能当开关** —— 当开关会误杀真实提醒（他在忙没碰手机，
      但"10 分钟后就上课"照样该说）。这条是硬设计约束，不是调参偏好。

两载体（EOPA 的思路落到我们的数据上）：
  ① **时段锚点**：96 个 15 分钟槽的活跃后验（两层证据融合，见下）
  ② **活跃原型**：可扩展的"他现在像在做什么"签名（刚解锁 / 连续在用 / 在听歌…）；
     本期先只落实时段锚点，原型接口留好（`prototypes()` 返回空权重=不参与）

两层证据（同一个"活跃"，两个独立信号）：
  · `minutes`：当天累计屏幕分钟数按相邻上报**求差** → 该槽真的用了多少分钟
  · `density`：上报条数 → 每次上报 = 手机醒过一次（实测这个信号更干净：1.43× vs 1.22×）
  两个信号各自算后验后取平均 —— 这比只用其中一个稳。

不确定性缩放（必须）：样本 < 14 天时，乘子向 1.0 收缩。前 2 天基本不动 —— 宁可晚一点学，
也不要在样本不足时把提醒节奏改歪。

用法：
    python3 whale_windows.py --learn            # 从真实数据学一次，写 .whale_windows.json
    python3 whale_windows.py --show             # 看当前窗口 + 各小时的乘子
    python3 whale_windows.py --holdout          # 留出验证（前 70% 天学 / 后 30% 天验）
    python3 whale_windows.py --at 21:30         # 某个时刻会拿到什么乘子
    python3 whale_windows.py --report           # 影子日志：新窗口"本来会"怎么改
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import pathlib
import ssl
import time
import urllib.request

SLOT_MIN = 15
SLOTS = 24 * 60 // SLOT_MIN          # 96
SHRINK_HOUR = 6.0                    # 槽 → 小时 的收缩强度（稳住稀疏槽）
SHRINK_GLOBAL = 2.0                  # 轻微回拉全局（**只做一次**，见 learning 里的坑）
TH_RATIO, TH_FLOOR = 1.3, 0.30       # 相对阈值：p ≥ max(0.30, 1.3×全局)
MAX_BOOST = 0.30                     # 窗口内最多把间隔压到 ×0.70
OUT_SLOW = 0.15                      # 窗口外轻微拉长（×1.15）
DEAD_SLOW = 0.40                     # "死区"（活跃度远低于他自己平均）再拉长（×1.40）
CONF_DAYS = 14.0                     # 置信度：14 天算完全置信
MIN_WINDOW_SLOTS = 2                 # 单窗口至少 30 分钟（15 分钟太碎，不算窗口）

BASE = pathlib.Path(os.getenv("WHALE_SCRIPTS", "/home/ubuntu/.hermes/scripts"))
CFG_PATH = pathlib.Path(os.getenv("WHALE_WINDOWS", str(BASE / ".whale_windows.json")))
SHADOW_PATH = pathlib.Path(os.getenv("WHALE_WINDOWS_SHADOW", str(BASE / ".whale_windows_shadow.jsonl")))


# ───────────────────────────── 取数
def fetch_metrics(metric="screen.active_minutes", limit=4000):
    hub = os.getenv("WHALE_HUB", "")
    tok = os.getenv("WHALE_TOKEN", "")
    ca = os.getenv("WHALE_CA", "/home/ubuntu/hub/tls/hub.crt")
    if not hub or not tok:
        raise SystemExit("需要 WHALE_HUB / WHALE_TOKEN（.whale_env 里有）")
    req = urllib.request.Request("%s/metrics?metric=%s&limit=%d" % (hub.rstrip("/"), metric, limit))
    req.add_header("X-Token", tok)
    with urllib.request.urlopen(req, timeout=40,
                                context=ssl.create_default_context(cafile=ca)) as r:
        return (json.loads(r.read().decode()) or {}).get("items") or []


EP_GAP_MIN = 10.0        # 相邻两次上报间隔 ≤10 分钟算"同一次连续使用"


def episodes(rows, gap_min=EP_GAP_MIN):
    """把一天的上报切成"连续使用 episode"，返回 [(开始 ts, 结束 ts)]。

    为什么是它（2026-09-29，依据 Fischer 2011 DOI 10.1145/2037373.2037402 /
    Attelia DOI 10.1109/PERCOM.2015.7146515）：**"这个 15 分钟槽有没有活动"是个坏信号** ——
    它把"没上报"和"没在用"混在一起（他手机约 18 分钟才上报一条），所以留出验证两次跑出
    1.36× 和 0.52×（不泛化）✗。正确做法是看**序列**：连续使用到某一条上报之后断了，
    那一条的时刻就是**断点**（他刚要放下手机）—— 序列信息天然免疫汇报缺口。
    """
    out = []
    if not rows:
        return out
    import datetime as _dt
    start = prev = None
    for ts, _v in rows:
        t = _dt.datetime.fromisoformat(ts)
        if prev is not None and (t - prev).total_seconds() > gap_min * 60:
            out.append((start, prev))
            start = t
        elif start is None:
            start = t
        prev = t
    if start is not None and prev is not None:
        out.append((start, prev))
    return out


def slots_by_day(items):
    """把原始上报压成 {day: {"minutes": [96], "density": [96]}}。

    ★ 累计值必须**按相邻上报求差**：中枢存的是"当天累计屏幕分钟"，直接求和会翻倍
      （项目里踩过：B站 96 分钟 → 154）。跨天/异常增量一律丢掉。
    """
    by_day = collections.defaultdict(list)
    for it in items:
        try:
            by_day[str(it["day"])].append((str(it["ts"]), float(it["value"])))
        except Exception:
            continue
    out = {}
    for day, rows in by_day.items():
        rows.sort()
        mins, dens, bnd = [0.0] * SLOTS, [0.0] * SLOTS, [0.0] * SLOTS
        for _s, e in episodes(rows):                  # 断点：episode 的**结束时刻**
            hh, mm = e.hour, e.minute
            bnd[(hh * 60 + mm) // SLOT_MIN] += 1
        for ts, _v in rows:                      # 密度：每次上报 = 手机醒过一次
            hh, mm = int(ts[11:13]), int(ts[14:16])
            dens[(hh * 60 + mm) // SLOT_MIN] += 1
        prev = None
        for ts, v in rows:
            hh, mm = int(ts[11:13]), int(ts[14:16])
            slot = (hh * 60 + mm) // SLOT_MIN
            if prev is not None:
                dv = v - prev
                if 0 < dv <= SLOT_MIN + 5:       # 合理增量才算
                    mins[slot] += dv
            elif 0 < v <= SLOT_MIN + 5:
                mins[slot] += v
            prev = v
        out[day] = {"minutes": mins, "density": dens, "boundary": bnd}
    return out


# ───────────────────────────── 学习
def _posterior(arr_hits, n_days):
    """槽级 Beta 后验 → 向小时级收缩一次 → 轻微回拉全局。返回 (p[96], 元信息)。

    ★ 踩过的坑（同龄 2026-09-29）：第一版把"槽→小时"和"小时→全局"**串起来各收一次**，
      全局先验被算了两遍，最强的 21:00 都被压到 0.42 → **一个窗口都学不出来** ✗
      收缩只该用来稳住稀疏估计，不该重复惩罚。
    """
    n_days = max(1, n_days)
    raw = [(1 + arr_hits[s]) / (2 + n_days) for s in range(SLOTS)]
    hour_p = {}
    for h in range(24):
        hh = sum(arr_hits[h * 4:h * 4 + 4])
        hour_p[h] = (1 + hh) / (2 + 4 * n_days)      # 小时级样本是槽级的 4 倍 → 更稳
    glob = sum(arr_hits) / max(1.0, SLOTS * n_days)
    p = []
    for s in range(SLOTS):
        a = (raw[s] * n_days + hour_p[s // 4] * SHRINK_HOUR) / (n_days + SHRINK_HOUR)
        b = (a * (n_days + SHRINK_HOUR) + glob * SHRINK_GLOBAL) / \
            (n_days + SHRINK_HOUR + SHRINK_GLOBAL)
        p.append(b)
    return p, {"days": n_days, "global": round(glob, 4),
               "hourly": [round(hour_p[h], 3) for h in range(24)]}


def _hits(day_slots, signal):
    n = len(day_slots)
    serial = [0] * SLOTS
    for _d, arrs in day_slots.items():
        a = arrs[signal]
        for s in range(SLOTS):
            if a[s] > 0:
                serial[s] += 1
    return serial, n


SIGNALS = [x for x in (os.getenv("WHALE_WIN_SIGNALS") or "minutes,density,boundary").split(",") if x]


def learn(day_slots, signals=None) -> dict:
    """学出窗口配置。返回可直接写进 .whale_windows.json 的 dict。"""
    sigs = signals or SIGNALS
    if not day_slots:
        return {"spec": "whale_windows/1", "windows": [], "days": 0,
                "note": "没有数据 → 不生成窗口（乘子恒为 1，等于不启用）"}
    ps, globs, days = [], 0.0, 0
    for sig in sigs:
        if all(sig not in v for v in day_slots.values()):
            continue
        pp, mm = _posterior(*_hits(day_slots, sig))
        ps.append(pp)
        globs += mm["global"]
        days = mm["days"]
    if not ps:
        return {"spec": "whale_windows/1", "windows": [], "days": 0, "note": "没有可用信号"}
    p = [sum(x[i] for x in ps) / len(ps) for i in range(SLOTS)]   # 多信号融合（取平均）
    meta_m = {"days": days}
    glob = globs / len(ps)
    thresh = max(TH_FLOOR, TH_RATIO * glob)
    peak = max(p)
    wins = []
    for w in _merge([s for s in range(SLOTS) if p[s] >= thresh]):
        if (w[1] - w[0] + 1) < MIN_WINDOW_SLOTS:
            continue
        seg = p[w[0]:w[1] + 1]
        mean_p = sum(seg) / len(seg)
        # 强度：相对"他自己最强的那一槽"归一 —— 最强窗口 ≈ 1.0，勉强过线的窗口 ≈ 0
        strength = 0.0 if peak <= glob else max(0.0, min(1.0, (mean_p - glob) / (peak - glob)))
        wins.append({"start": _hm(w[0]), "end": _hm(w[1] + 1),
                     "slots": w[1] - w[0] + 1, "mean_p": round(mean_p, 3),
                     "peak_p": round(max(seg), 3), "strength": round(strength, 3)})
    return {"spec": "whale_windows/1", "learned_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "days": meta_m["days"], "global_p": round(glob, 4), "thresh": round(thresh, 3),
            "peak_p": round(peak, 3), "signals": list(sigs),
            "hourly": [_hourly_of(p, h) for h in range(24)],   # 各整点活跃概率（判断"死区"用）
            "p_slots": [round(x, 4) for x in p],  # 96 个槽的后验（可溯源/可回放）
            "windows": wins, "note": "影子模式：只记录不改行为，直到 WHALE_WINDOW_MODE=on"}


def _hourly_of(p, h):
    seg = p[h * 4:h * 4 + 4]
    return round(sum(seg) / 4.0, 3)


def _merge(xs, max_gap=1):
    """阈值以上的槽合并成区间（允许中间空 max_gap 个槽）。"""
    if not xs:
        return []
    out, start, prev = [], xs[0], xs[0]
    for s in xs[1:]:
        if s - prev <= max_gap + 1:
            prev = s
            continue
        out.append((start, prev))
        start = prev = s
    out.append((start, prev))
    return out


def _hm(slot):
    m = min(24 * 60, slot * SLOT_MIN)
    return "%02d:%02d" % (m // 60, m % 60)


def _slot_of(hhmm):
    """"21:30" → slot。非法输入返回 -1（**配置坏了绝不能把说话层带崩**）。"""
    try:
        t = str(hhmm or "").strip()
        hh, mm = int(t[:2]), int(t[3:5])
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return -1
        return hh * 4 + mm // 15
    except Exception:
        return -1


# ───────────────────────────── 用：乘子
def confidence(days) -> float:
    """不确定性缩放：样本越少，越不敢改动（线性到 14 天满置信）。

    坏输入（"abc"/None/负数）一律当成 0 → 乘子恒等；**配置再烂也不许影响说话**。
    """
    try:
        d = float(days)
    except (TypeError, ValueError):
        return 0.0
    if d != d or d < 0:                      # NaN / 负数
        return 0.0
    return max(0.0, min(1.0, d / CONF_DAYS))


def multiplier_at(cfg: dict, hh, mm=0) -> dict:
    """某时刻的间隔乘子（<1 = 说勤点，>1 = 说少点）+ 理由。

    硬约束：① 只在"窗口强度"上做 ±30% 的**软调制**，不当开关；
            ② 免打扰时段由调用方在更外层拦掉（这里不认识 QUIET）；
            ③ 紧急通道永远不经过这里。
    """
    s = (int(hh) * 60 + int(mm)) // SLOT_MIN
    wins = (cfg or {}).get("windows") or []
    days = (cfg or {}).get("days") or 0
    conf = confidence(days)
    if not wins or s < 0 or s >= SLOTS:
        return {"mult": 1.0, "conf": round(conf, 2), "why": "没有可用窗口 → 不改节奏", "in": ""}
    for w in wins:
        a, b = _slot_of((w or {}).get("start")), _slot_of((w or {}).get("end"))
        if a < 0 or b < 0 or b <= a:
            continue                                   # 坏窗口跳过，不影响别的
        if a <= s < b:
            raw = 1.0 - MAX_BOOST * float(w.get("strength") or 0)
            return {"mult": round(1.0 + (raw - 1.0) * conf, 4), "conf": round(conf, 2),
                    "why": "在活跃窗口 %s–%s（强度 %.2f）→ 说勤点" % (w["start"], w["end"], w.get("strength") or 0),
                    "in": "%s-%s" % (w["start"], w["end"])}
    # 不在任何窗口里：默认轻微拉长；"死区"（明显低于他自己的平均）再拉长
    glob = float((cfg or {}).get("global_p") or 0)
    hh_p = ((cfg or {}).get("hourly") or [None] * 24)
    hp = hh_p[int(hh)] if 0 <= int(hh) < 24 and hh_p[int(hh)] is not None else None
    dead = hp is not None and glob > 0 and hp < 0.6 * glob
    raw = 1.0 + (DEAD_SLOW if dead else OUT_SLOW)
    return {"mult": round(1.0 + (raw - 1.0) * conf, 4), "conf": round(conf, 2),
            "why": ("他基本不活跃的时段（%02d 点，活跃概率 %.2f）→ 说少点" % (int(hh), hp or 0)) if dead
                   else "不在活跃窗口 → 略微说少点", "in": ""}


def prototypes(ctx: dict = None) -> list:
    """活跃原型（EOPA 的第二个载体）—— 本期只留接口，返回空表示不参与。

    下一步可以在这里加：刚解锁 / 连续在用 ≥10 分钟 / 在听歌 / 也有日程在附近。
    每个原型给一个权重，最后由融合式一起乘进去。之所以先留空：**没有数据支撑的因子不加**
    （项目里的老教训：加了永远算 0 分的字段，只会让"以为修好了"变成新坑）。
    """
    return []


# ───────────────────────────── 配置读写（原子）
def load_cfg(path=None) -> dict:
    p = pathlib.Path(path or CFG_PATH)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_cfg(cfg: dict, path=None) -> bool:
    p = pathlib.Path(path or CFG_PATH)
    try:
        tmp = str(p) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def shadow_log(rec: dict, path=None) -> None:
    """影子日志：只追加，永不抛（说话层不能因为观测而挂）。"""
    try:
        p = pathlib.Path(path or SHADOW_PATH)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        lines = p.read_text(encoding="utf-8").splitlines()
        if len(lines) > 3000:
            p.write_text("\n".join(lines[-2000:]) + "\n", encoding="utf-8")
    except Exception:
        pass


# ───────────────────────────── 留出验证
def _in_slots(cfg):
    out = set()
    for w in (cfg or {}).get("windows") or []:
        a, b = _slot_of(w.get("start")), _slot_of(w.get("end"))
        if a >= 0 and b > a:
            out |= set(range(a, min(SLOTS, b)))
    return out


def holdout(day_slots, ratio=0.7, signals=None) -> dict:
    """前 70% 天学、后 30% 天验，**逐信号**给"窗口内外倍数"。返回 {信号: 结果}。

    ★ 为什么必须逐信号：上一轮只用 minutes 时，同一份数据相隔 20 分钟跑出 1.36× 和 **0.52×**
      （窗口内比窗口外更不活跃）—— 那说明信号不泛化。所以这里对每个候选信号各学一次、各验一次，
      口径也同时给两个（分钟 / episode 断点），避免"用哪个口径就是哪个结论"。

    评估口径说明：**逐槽 F1 不用**（他上报有缺口，"这一槽有没有活动"混了一半是"没上报"）。
    用"窗口内 vs 窗口外的平均量"倍数 + 小时级精确/召回。
    """
    days = sorted(day_slots)
    cut = max(2, int(len(days) * ratio))
    train = {d: day_slots[d] for d in days[:cut]}
    test = {d: day_slots[d] for d in days[cut:]}
    if len(days) < 5 or not test:
        return {}
    cands = list(signals or SIGNALS) + ["fused"]
    res = {}
    for sig in cands:
        try:
            cfg = learn(train, None if sig == "fused" else [sig])
        except Exception as e:
            res[sig] = {"error": str(e)[:50]}
            continue
        inside = _in_slots(cfg)
        if not inside:
            res[sig] = {"windows": 0, "ratio_minutes": None, "ratio_boundary": None}
            continue
        acc = {k: [0.0, 0, 0.0, 0] for k in ("minutes", "boundary")}   # in_sum,in_n,out_sum,out_n
        htp = hfp = hfn = 0
        for _d, arrs in test.items():
            for k in acc:
                a = arrs.get(k) or [0.0] * SLOTS
                for sl in range(SLOTS):
                    if sl in inside:
                        acc[k][0] += a[sl]; acc[k][1] += 1
                    else:
                        acc[k][2] += a[sl]; acc[k][3] += 1
            a = arrs.get("minutes") or [0.0] * SLOTS
            for h in range(24):
                act = sum(a[h * 4:h * 4 + 4]) >= 2
                pred = any(x in inside for x in range(h * 4, h * 4 + 4))
                if act and pred:
                    htp += 1
                elif pred:
                    hfp += 1
                elif act:
                    hfn += 1
        prec = htp / max(1, htp + hfp)
        rec = htp / max(1, htp + hfn)
        def _r(k, _acc=acc):          # 绑定默认参数，避免闭包捕获循环变量（ruff B023）
            i_m, i_n, o_m, o_n = _acc[k]
            a = i_m / max(1, i_n)
            b = o_m / max(1, o_n)
            return round(a / b, 2) if b > 0 else None
        res[sig] = {"windows": len(cfg.get("windows") or []), "slots": len(inside),
                    "ratio_minutes": _r("minutes"), "ratio_boundary": _r("boundary"),
                    "hour_precision": round(prec, 3), "hour_recall": round(rec, 3),
                    "hour_f1": round(2 * prec * rec / max(1e-9, prec + rec), 3)}
    return res


def budget_progress(cfg, now=None, start_hour=7, end_hour=23) -> float:
    """到此刻为止，**加权过的**一天进度（0—1）—— 额度分配要用它。

    直觉：活跃窗口里的 1 分钟算更多的"预算进度"，窗口外算更少。
    这样每日额度不变，但**额度优先花在他活跃的时段**（HeartSteps 一天 5 次的同款思路，
    Liao 2020 DOI 10.1145/3381007）。
    """
    n = now or time.localtime()
    cur = n.tm_hour * 60 + n.tm_min
    a, b = start_hour * 60, end_hour * 60
    if cur <= a:
        return 0.0
    if cur >= b:
        return 1.0
    wins = (cfg or {}).get("windows") or []
    def w_of(slot):
        for w in wins:
            s0, s1 = _slot_of(w.get("start")), _slot_of(w.get("end"))
            if s0 >= 0 and s0 <= slot < s1:
                return 1.0 + MAX_BOOST * float(w.get("strength") or 0)
        return 1.0 - OUT_SLOW
    tot = sum(w_of(s) for s in range(a // SLOT_MIN, b // SLOT_MIN)) or 1.0
    got = sum(w_of(s) for s in range(a // SLOT_MIN, min(cur // SLOT_MIN + 1, b // SLOT_MIN)))
    return max(0.0, min(1.0, got / tot))


# ───────────────────────────── CLI
def _cmd_learn(a):
    items = fetch_metrics(limit=a.limit)
    sb = slots_by_day(items)
    cfg = learn(sb)
    days = cfg.get("days") or 0
    print("  信号融合：minutes + density ｜ 有效天 %d" % days)
    print("  全局活跃率 %.3f ｜ 阈值 %.3f ｜ 峰值 %.3f" % (cfg.get("global_p") or 0,
                                                     cfg.get("thresh") or 0, cfg.get("peak_p") or 0))
    print("  窗口 %d 个：" % len(cfg.get("windows") or []))
    for w in cfg.get("windows") or []:
        print("    %s–%s  强度 %.2f  mean_p %.3f  peak %.3f" % (
            w["start"], w["end"], w["strength"], w["mean_p"], w["peak_p"]))
    res = holdout(sb)
    if res:
        print("  逐信号留出验证（分钟倍数 / 断点倍数 / 小时F1）：")
        for sig, r in res.items():
            print("    %-10s %5s / %5s / %5s" % (sig, r.get("ratio_minutes"),
                                                 r.get("ratio_boundary"), r.get("hour_f1")))
    print("  置信度 %.2f（样本 %d 天 / 满置信 %d 天 → 乘子先按这个比例生效）"
          % (confidence(days), days, int(CONF_DAYS)))
    if not a.dry:
        ok = save_cfg(cfg)
        print("  %s %s" % ("✓ 已写" if ok else "✗ 写失败", CFG_PATH))
    return 0


def _cmd_show(a):
    cfg = load_cfg()
    print("  配置：%s" % CFG_PATH)
    print("  学习时间 %s ｜ 天 %s ｜ 全局 %.3f ｜ 阈值 %s" % (
        cfg.get("learned_at") or "-", cfg.get("days") or 0, cfg.get("global_p") or 0, cfg.get("thresh")))
    for w in cfg.get("windows") or []:
        print("    窗口 %s–%s  强度 %.2f" % (w["start"], w["end"], w["strength"]))
    print("\n  各整点的乘子（<1 说勤点，>1 说少点）：")
    for h in range(24):
        r = multiplier_at(cfg, h, 0)
        print("    %02d:00 ×%.2f  %s" % (h, r["mult"], r["why"]))
    return 0


def _cmd_at(a):
    hh, mm = (a.at.split(":") + ["0"])[:2]
    r = multiplier_at(load_cfg(), int(hh), int(mm))
    print("  %s → 间隔 ×%.2f（置信 %.2f）｜%s" % (a.at, r["mult"], r["conf"], r["why"]))
    return 0


def _cmd_holdout(a):
    sb = slots_by_day(fetch_metrics(limit=a.limit))
    res = holdout(sb)
    if not res:
        print("  天数不足（需要 ≥5 天）"); return 1
    print("  留出验证（前 70% 天学 / 后 30% 天验）—— **逐信号**看谁真的泛化：")
    print("  %-10s %6s %8s %10s %10s %8s" % ("信号", "窗口", "槽数", "分钟倍数", "断点倍数", "小时F1"))
    for sig, r in res.items():
        print("  %-10s %6s %8s %10s %10s %8s" % (
            sig, r.get("windows", "-"), r.get("slots", "-"),
            r.get("ratio_minutes", "-"), r.get("ratio_boundary", "-"), r.get("hour_f1", "-")))
    print("  读法：倍数 >1 = 窗口内确实更活跃（泛化）；<1 = 反了（信号不可用）")
    ok = [s for s, r in res.items() if (r.get("ratio_boundary") or 0) > 1.15]
    print("  断点口径下倍数 >1.15 的信号：%s" % ("、".join(ok) if ok else "（没有 → 先别启用）"))
    return 0


def _cmd_report(a):
    """影子报告：新窗口"本来会"把哪些决定改掉（不改行为，只告诉你差多少）。"""
    p = pathlib.Path(a.file or SHADOW_PATH)
    try:
        rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    except Exception:
        print("  还没有影子日志（%s）—— 影子模式只在她评估『说/不说』时记一行" % p); return 0
    print("  影子记录 %d 条" % len(rows))
    m = collections.Counter(r.get("in") or "窗口外" for r in rows)
    mults = [float(r.get("mult") or 1.0) for r in rows]
    for k, n in m.most_common(8):
        print("    %-12s %4d 条" % (k, n))
    if mults:
        print("  乘子平均 %.3f ｜ 小于 1（会更早开口）的占 %.0f%%" % (
            sum(mults) / len(mults), 100.0 * sum(1 for x in mults if x < 1) / len(mults)))
    return 0


def main():
    ap = argparse.ArgumentParser(description="活跃窗口学习器（影子模式默认）")
    ap.add_argument("--learn", action="store_true", help="学一次并写配置")
    ap.add_argument("--dry", action="store_true", help="学了但不写配置")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--at", default="", help="看某个时刻的乘子，如 21:30")
    ap.add_argument("--holdout", action="store_true")
    ap.add_argument("--report", action="store_true", help="影子日志汇总")
    ap.add_argument("--file", default="")
    ap.add_argument("--limit", type=int, default=4000)
    a = ap.parse_args()
    if a.learn:
        return _cmd_learn(a)
    if a.show:
        return _cmd_show(a)
    if a.at:
        return _cmd_at(a)
    if a.holdout:
        return _cmd_holdout(a)
    if a.report:
        return _cmd_report(a)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
