# --------------------------------------------------------------- 个人基线 / 惊讶度
# 为什么要它：原来的"有料程度"是拍脑袋阈值（屏幕≥300 分钟就算有料）——
# 300 分钟对一个人是"今天有点多"、对另一个人是日常。值不值得开口应该**相对他自己的历史**。
# 全是成熟且标准库能实现的稳健统计：median + MAD（抗离群）→ 小样本收缩 → 单向判定。
BASELINE_MIN_DAYS = 3        # 少于此天数不做基线判断（回落阈值规则）
BASELINE_SHRINK_K = 4.0      # 收缩强度：z' = z * n/(n+k)
BASELINE_FLAG_Z = 1.5        # 收缩后 |z| 达到多少才算"反常"（保守，宁少勿滥）
BASELINE_MIN_SAMPLES = 3     # 一天至少要有这么多条上报才算"这天数据是完整的"


def _robust(vals):
    """中位数 + 稳健σ（1.4826×MAD），带尺度下限，避免 z 爆炸。"""
    vals = [float(v) for v in vals if v is not None]
    if not vals:
        return 0.0, 1.0
    med = statistics.median(vals)
    mad = statistics.median([abs(v - med) for v in vals]) if len(vals) >= 2 else 0.0
    sigma = max(1.4826 * mad, abs(med) * 0.05, 1.0)
    return med, sigma


def _day_series(c, metric, days=21, kind="peak", floor=0.0):
    """{日期: 当天代表值}。

    kind="peak"：当天最大值（累计型指标：设备每轮报"今天到目前的累计"，峰值才是"这一天用了多少"）
    kind="last"：当天最后一条（瞬时型：电量、温度）
    ★ 完整性门槛：一天的上报条数少于 BASELINE_MIN_SAMPLES、或值低于 floor，
      视为**残缺日**直接丢弃 —— 否则"采集器那天没跑"会被当成"他那天几乎没用手机"，
      进而把 σ 撑大、让真正反常的日子看起来正常（实测抓到过：某天只有 5 分钟）。
    """
    d0 = (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = c.execute("SELECT day, ts, value FROM metrics WHERE metric=? AND day>=? "
                     "ORDER BY ts ASC", (metric, d0)).fetchall()
    per, cnt = {}, {}
    for r in rows:
        try:
            v = float(r["value"])
        except (TypeError, ValueError):
            continue
        day = r["day"]
        cnt[day] = cnt.get(day, 0) + 1
        if kind == "last":
            per[day] = v
        else:
            per[day] = max(v, per.get(day, v))
    out = {}
    for day, v in per.items():
        if cnt.get(day, 0) < BASELINE_MIN_SAMPLES:
            continue
        if floor and v < floor:
            continue
        out[day] = v
    return out


def _cat_day_series(c, days=21):
    """{类别: {日期: 分钟}} —— 与 llm_context 同口径：每个 App 取当天最新，再按类别相加。"""
    d0 = (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    # ★ 先在库里把「每个 App 当天最新」压出来，别再拉几万行回 Python 逐行 json.loads。
    #   压测实测（40 万行）：原写法 21 天要拉 14 万行 + 14 万次 json.loads →
    #   单次 llm_context 2 秒（而她每次开口前都要算一次）。
    #   窗口函数需要 SQLite ≥3.25；没有就退回旧写法（功能不变，只是慢）。
    try:
        rows = c.execute(
            "SELECT day, ts, value, meta FROM ("
            "  SELECT day, ts, value, meta,"
            # ★ 分组键必须与下面循环用的**同一个键**（pkg/app）。
            #   原来按 meta 整串 JSON 分组：meta 里只要混进任何"每次都变"的字段
            #   （时间戳/标题/序号…），分组就散 → 同一 App 返回多行 →
            #   聚合静默退化成"取 SQL 返回顺序里最后那条"（不保证是最新的）。
            "         ROW_NUMBER() OVER (PARTITION BY day,"
            "             COALESCE(json_extract(meta,'$.pkg'), json_extract(meta,'$.app'), '?')"
            "           ORDER BY ts DESC) rn"
            "  FROM metrics WHERE day>=? AND metric='app.usage_minutes'"
            ") WHERE rn=1", (d0,)).fetchall()
    except Exception:
        rows = c.execute("SELECT day, ts, value, meta FROM metrics "
                         "WHERE day>=? AND metric='app.usage_minutes' ORDER BY ts ASC", (d0,)).fetchall()
    latest = {}                                   # (day, pkg) -> (类别, 分钟)
    for r in rows:
        try:
            meta = json.loads(r["meta"] or "{}")
        except Exception:
            meta = {}
        pkg = meta.get("pkg") or meta.get("app") or "?"
        lab = cat_app(meta.get("app", ""), pkg)
        try:
            v = int(float(r["value"] or 0))
        except (TypeError, ValueError):
            continue
        latest[(r["day"], pkg)] = (lab, v)
    out = {}
    for (day, _pkg), (lab, v) in latest.items():
        out.setdefault(lab, {})[day] = out.get(lab, {}).get(day, 0) + v
    return out


# ---- P0 数据健康度：让"这条数据可不可信"变成一等公民 ----
DATA_CONF = {"ok": 1.0, "thin": 0.6, "stale": 0.5, "missing": 0.35}
DATA_HEALTH_DAYS = 14


def source_health(days=DATA_HEALTH_DAYS, day=None):
    """每个数据源的「新鲜度 / 覆盖 / 可信度」。

    判据（保守优先，宁少勿滥）：
      - `missing`：今天一条都没有
      - `stale`  ：最近一条上报超过 36 小时
      - `thin`   ：今天条数 < 平时条数中位数的 1/4（且平时 ≥5 条）
      - `ok`     ：其余

    返回 {源名: {verdict, confidence, today_n, typical_n, fresh_hours, note}}
    """
    today = day or today_str()
    start = (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    agg = {}
    with db() as c:
        rows = c.execute("SELECT device, day, COUNT(*) n, MAX(ts) last FROM metrics "
                         "WHERE day >= ? GROUP BY device, day", (start,)).fetchall()
    for r in rows:
        s = blur_device(r["device"])
        a = agg.setdefault(s, {"days": {}, "last": ""})
        a["days"][r["day"]] = a["days"].get(r["day"], 0) + r["n"]
        if (r["last"] or "") > a["last"]:
            a["last"] = r["last"]
    out = {}
    now = datetime.now(TZ)
    for s, a in agg.items():
        hist = [v for d, v in a["days"].items() if d != today]
        typical = int(_robust(hist)[0]) if hist else 0
        tn = a["days"].get(today, 0)
        fresh = None
        try:
            fresh = round((now - datetime.fromisoformat(a["last"]).astimezone(TZ)).total_seconds() / 3600.0, 1)
        except Exception:
            pass
        if tn == 0:
            v, note = "missing", "今天还没有任何上报"
        elif fresh is not None and fresh > 36:
            v, note = "stale", "最近一条已经 %.0f 小时前了" % fresh
        elif typical >= 5 and tn < max(2, typical / 4.0):
            v, note = "thin", "今天只有 %d 条上报（平时约 %d 条）" % (tn, typical)
        else:
            v, note = "ok", ""
        out[s] = {"verdict": v, "confidence": DATA_CONF[v], "today_n": tn,
                  "typical_n": typical, "fresh_hours": fresh, "note": note}
    return out


def data_confidence(source="手机"):
    """取某个源当前的可信度（0.35–1.0）。取不到就当 1.0（不因为拿不到健康度就变保守）。"""
    try:
        return float((source_health().get(source) or {}).get("confidence", 1.0))
    except Exception:
        return 1.0


def _surprise_of(today_value, series, direction="high", today=None, conf=1.0):
    """今天相对自己历史有多反常。返回 dict（flag/z/median/ratio/text）。"""
    today = today or today_str()
    hist = [v for d, v in (series or {}).items() if d != today]
    o = {"flag": False, "z": 0.0, "median": None, "ratio": None, "n": len(hist), "text": ""}
    if today_value is None or len(hist) < BASELINE_MIN_DAYS:
        return o
    med, sigma = _robust(hist)
    try:
        z = (float(today_value) - med) / sigma
    except Exception:
        return o
    n = len(hist)
    # ★ 小样本收缩：天数少时不敢下结论。
    # ★ P0：再按**数据可信度**放大 —— 这个源今天上报残缺时，k 变大 → 收缩更狠 → 不轻易说"反常"。
    conf = min(1.0, max(0.1, float(conf or 1.0)))
    _k = BASELINE_SHRINK_K / conf
    zs = z * (n / (n + _k))
    ratio = (float(today_value) / med) if med else None
    o.update(z=round(zs, 2), median=round(med, 1),
             ratio=(round(ratio, 2) if ratio else None))
    if (zs >= BASELINE_FLAG_Z) if direction == "high" else (zs <= -BASELINE_FLAG_Z):
        o["flag"] = True
        pct = int(round((ratio - 1) * 100)) if ratio else 0
        o["text"] = ("比平时多 %d%%（平时约 %.0f）" % (pct, med)) if direction == "high" \
            else ("比平时少 %d%%（平时约 %.0f）" % (abs(pct), med))
    return o


def surprise_report(days=21):
    """算一遍所有"值得相对自己历史看"的指标，返回 {标签: {...}}。

    ★ 只输出**类别/指标名 + 相对变化**，绝不带 App 名 —— 给模型的那份因此不需要放宽隐私。
    ★ 只做"偏多"才值得提的量；像"连续活跃"这种**重置型**指标不走比例基线（它用阈值规则）。
    """
    out = {}
    today = today_str()
    conf_phone = data_confidence("手机")          # ★ P0：手机侧数据今天可不可信
    with db() as c:
        # 屏幕总量（floor=30：低于半小时视为残缺日）
        s = _day_series(c, "screen.active_minutes", days, "peak", floor=30)
        v = s.get(today)
        r = _surprise_of(v, s, "high", conf=conf_phone)
        if r["median"] is not None:
            out["屏幕使用"] = dict(r, today=v)
        # 按类别（游戏/视频/社交…）：只报"偏多"的
        for lab, series in (_cat_day_series(c, days) or {}).items():
            v = series.get(today)
            if v is None or v < 10:
                continue
            r = _surprise_of(v, series, "high", conf=conf_phone)
            if r["median"] is not None:
                out[lab] = dict(r, today=v)
    return out


ASK_LIMIT_PER_DAY = 1          # 一天最多问一个（问多了就成了审问）


def question_now(day=None):
    """挑**一个**有数据支撑的问题。返回 {slot, hint, facts}；没有可问的就返回 None。

    三条原则（用户明确要求）：
      1) **必须基于已有数据** —— 语气里要带着真实数字，不能凭空问"你今天心情如何"
      2) **一天最多一个**（ASK_LIMIT_PER_DAY），且要过免打扰/频率闸
      3) **问过就不再问**：问过的记成情节（kind=ask:<slot>），三天内不重复
    问题比播报更值钱：她的稿子里就缺"主人自己的说法"（为什么刷这么久？打算几点睡？）
    """
    day = day or today_str()
    try:
        asked = {str(e.get("kind") or "") for e in episodes_recent(days=3, limit=30)}
    except Exception:
        asked = set()
    if len([k for k in asked if k.startswith("ask:said")]) >= ASK_LIMIT_PER_DAY:
        return None
    cands = []

    # ① 某类今天明显偏多 → 问原因（画像增量最大）
    try:
        for lab, v in (surprise_report() or {}).items():
            if v.get("flag") and lab != "屏幕使用" and v.get("text"):
                cands.append({"slot": "ask:why_" + str(lab), "facts": {"类别": lab, "变化": v["text"]},
                              "hint": "今天%s%s。问她这是特意放松还是没刹住 —— 带上这个数字，语气是关心不是质问。" % (lab, v["text"])})
    except Exception as e:
        print(f"[ask] surprise 不可用：{str(e)[:60]}", flush=True)

    # ② 深夜还活跃 → 问作息计划
    try:
        with db() as c:
            if _deep_night_active(c, day):
                cands.append({"slot": "ask:sleep_plan", "facts": {"时间": now_iso()[11:16]},
                              "hint": "这么晚还亮着屏。问她今晚打算几点睡 —— 别催，就是问一句。"})
    except Exception:
        pass

    # ③ 连续静坐很久 → 问她动没动
    try:
        with db() as c:
            sit = _latest_metric(c, "pc.continuous_active_minutes", days=1)
        if sit and sit.get("value") and sit["value"] >= (CFG.get("rules") or {}).get("sit_continuous_minutes", 50) * 2:
            cands.append({"slot": "ask:sit_check", "facts": {"连续活跃": int(sit["value"])},
                          "hint": "连着坐了 %d 分钟。问她刚刚有没有起来走动 —— 一句话就够。" % int(sit["value"])})
    except Exception:
        pass

    for c_ in cands:
        if c_["slot"] not in asked:
            return c_
    return None


def _series_avg(series, d1, d2):
    vals = [v for d, v in (series or {}).items() if d1 <= d <= d2]
    return (sum(vals) / len(vals)) if vals else None, len(vals)


def review(kind="week", speak=False):
    """周/月复盘：**本期 vs 上期**，看趋势、异常，以及"上次提的那件事有没有变好"。

    "上期建议兑现"不靠猜：每次复盘的结论会以情节（kind=review）存下来，
    下一次复盘读出上期结论、按同一指标再看一遍方向 —— 这样"她记得住自己说过什么"。
    """
    now = datetime.now(TZ)
    if kind == "month":
        cur0 = now.replace(day=1)
        prev_end = cur0 - timedelta(days=1)
        prev0 = prev_end.replace(day=1)
        d1, p1, p2 = cur0.strftime("%Y-%m-%d"), prev0.strftime("%Y-%m-%d"), prev_end.strftime("%Y-%m-%d")
        span_days, label = 31, "这个月"
    else:
        d1 = (now - timedelta(days=6)).strftime("%Y-%m-%d")
        p2 = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        p1 = (now - timedelta(days=13)).strftime("%Y-%m-%d")
        span_days, label = 7, "这周"
    today = now.strftime("%Y-%m-%d")
    out = {"kind": kind, "label": label, "period": [d1, today], "prev": [p1, p2], "items": [], "followup": []}

    with db() as c:
        scr = _day_series(c, "screen.active_minutes", 60, "peak", floor=30)
        cats = _cat_day_series(c, 60) or {}

    def add(what, series):
        cur, n1 = _series_avg(series, d1, today)
        pre, n2 = _series_avg(series, p1, p2)
        if not cur or not pre:
            return
        pct = int(round((cur / pre - 1) * 100))
        out["items"].append({"what": what, "this": int(round(cur)), "prev": int(round(pre)), "pct": pct})

    add("屏幕", scr)
    for lab, s in cats.items():
        add(lab, s)
    out["items"] = [i for i in out["items"] if abs(i["pct"]) >= 15 or i["what"] == "屏幕"]
    out["items"].sort(key=lambda i: -abs(i["pct"]))

    # 上期复盘说了什么 → 这次核对方向（改善 / 变差 / 没动）
    last_rev = None
    try:
        for e in episodes_recent(days=span_days * 2 + 3, limit=30, kind="review"):
            last_rev = e
            break
    except Exception:
        last_rev = None
    if last_rev:
        try:
            prev_items = json.loads(last_rev.get("sig") or "{}") or last_rev.get("sig")
            if isinstance(prev_items, str):
                prev_items = json.loads(prev_items)
            prev_items = (prev_items or {}).get("items") or []
        except Exception:
            prev_items = []
        cur_map = {i["what"]: i["pct"] for i in out["items"]}
        for pi in prev_items:
            w, was = pi.get("what"), pi.get("pct")
            if w in cur_map and was is not None:
                nowp = cur_map[w]
                if (was > 0 and nowp < was - 5) or (was < 0 and nowp > was + 5):
                    verdict = "好转"
                elif abs(nowp - was) <= 5:
                    verdict = "没动"
                else:
                    verdict = "更明显了"
                out["followup"].append({"what": w, "was": was, "now": nowp, "verdict": verdict})

    # 异常次数（这期）
    try:
        out["said_count"] = len([e for e in episodes_recent(days=span_days, limit=60)
                                 if str(e.get("kind") or "").startswith("said:")])
    except Exception:
        out["said_count"] = 0

    # 话语（短、只说变化最大的两条 + 一句跟进）
    parts = []
    for i in out["items"][:2]:
        parts.append("%s日均 %d 分钟，比上期%s %d%%" % (i["what"], i["this"],
                                                    "多" if i["pct"] >= 0 else "少", abs(i["pct"])))
    if out["followup"]:
        f0 = out["followup"][0]
        parts.append("上次提的%s：%s" % (f0["what"], f0["verdict"]))
    core = "；".join(parts) if parts else "这期没什么明显变化"
    out["text"] = "（把%s的记录翻了一遍）%s。今天已经说了 %d 条。" % (
        "这一周" if kind == "week" else "这个月", core, out.get("said_count") or 0)
    if speak:
        say(out["text"], "info", kind="review", key="review:%s:%s" % (kind, d1))
    episode_add("review", "复盘（%s）：%s" % (label, core), sig={"items": out["items"]})
    return out


def top_surprise(rep=None):
    """挑"最值得说的那一条"：按收缩后 z 排序取第一（对上角色卡的"只挑一条"）。"""
    rep = rep if rep is not None else surprise_report()
    items = [(abs(v["z"]), k, v) for k, v in (rep or {}).items() if v.get("flag")]
    if not items:
        return None
    items.sort(reverse=True, key=lambda x: x[0])
    _, lab, v = items[0]
    return {"what": lab, "text": v["text"], "z": v["z"], "today": v.get("today"),
            "median": v.get("median")}


def care_now():
    """给**终端**（挂件/网页）用的"现在都关心什么"一块数据。

    与 llm_context 的区别：这里是给你自己的设备看的（同一 token 保护），
    所以**不做模糊化**（电量就是 34%，歌名就是歌名）—— 但同样只给聚合，
    不给通知原文、不给进程路径、不给精确金额。
    """
    out = {}
    day = today_str()
    # 天气（城市级坐标，不含定位）
    try:
        for key, w in (("today", weather_of(0)), ("tomorrow", weather_of(1))):
            if w:
                out.setdefault("weather", {})[key] = {
                    "desc": w.get("desc"), "tmin": w.get("tmin"), "tmax": w.get("tmax"),
                    "rain_prob": w.get("rain_prob"),
                }
    except Exception:
        pass
    with db() as c:
        # 电量 / 充电
        b = _latest_metric(c, "device.battery_percent", days=1)
        if b and b.get("value") is not None:
            ch = _latest_metric(c, "device.charging", days=1)
            out["battery"] = {"percent": int(float(b["value"])),
                              "charging": bool(ch and float(ch.get("value") or 0) > 0),
                              "at": hour_only(b["ts"])}
        # 下一个闹钟
        al = _latest_metric(c, "device.next_alarm", days=2)
        if al and al.get("value") is not None:
            out["next_alarm"] = {"in_minutes": int(float(al["value"])), "at": hour_only(al["ts"])}
        # 连续活跃（坐太久）
        sit = _latest_metric(c, "pc.continuous_active_minutes", days=1)
        if sit and sit.get("value") is not None:
            out["continuous_active_minutes"] = int(float(sit["value"]))
        # 单片机温湿度
        for m, k in (("temp", "temp"), ("hum", "hum")):
            v = _latest_metric(c, m, days=1)
            if v and v.get("value") is not None:
                out.setdefault("env", {})[k] = round(float(v["value"]), 1)
        # 在听什么（15 分钟内、且在播才算"现在"）
        try:
            r = c.execute("SELECT ts, meta FROM metrics WHERE metric='music.track' "
                          "ORDER BY ts DESC LIMIT 1").fetchone()
            if r:
                meta = json.loads(r["meta"] or "{}")
                try:
                    fresh = (datetime.now(TZ) - datetime.fromisoformat(r["ts"])).total_seconds() < 15 * 60
                except Exception:
                    fresh = False
                if fresh and meta.get("playing"):
                    t = (meta.get("title") or "").strip()
                    a = (meta.get("artist") or "").strip()
                    if " - " in t:
                        t, a = [x.strip() for x in t.split(" - ", 1)]
                    out["listening"] = {"title": t, "artist": a}
        except Exception:
            pass
        # 快递/订单：只给聚合次数（7 天）
        try:
            d0 = (datetime.now(TZ) - timedelta(days=7)).strftime("%Y-%m-%d")
            rows = c.execute("SELECT meta FROM metrics WHERE metric='order.event' AND day >= ?",
                             (d0,)).fetchall()
            ship = pay = 0
            for r in rows:
                try:
                    meta = json.loads(r["meta"] or "{}")
                except Exception:
                    meta = {}
                if meta.get("type") in ("发货", "派送", "签收"):
                    ship += 1
                elif meta.get("type") == "支付":
                    pay += 1
            if ship or pay:
                out["orders_7d"] = {"shipping": ship, "paid": pay}
        except Exception:
            pass
        # ★ 个人基线（相对自己历史的偏离）
        try:
            _sur = surprise_report()
            if _sur:
                out["surprise"] = {k: {"text": v["text"], "flag": v["flag"], "z": v["z"]}
                                   for k, v in _sur.items() if v.get("median") is not None}
        except Exception:
            pass
        # ★ 电脑体检（新数据源）：系统盘剩余 / 内存占用 / 开机时长 / 今日窗口切换次数
        for m, k in (("pc.disk_free_percent", "disk_free_percent"),
                     ("pc.mem_percent", "mem_percent"),
                     ("pc.uptime_hours", "uptime_hours"),
                     ("pc.window_switches_today", "window_switches_today")):
            v = _latest_metric(c, m, days=1)
            if v and v.get("value") is not None:
                out.setdefault("pc", {})[k] = round(float(v["value"]), 1)
        # 今天的游戏时长（>=10 分钟才给）
        try:
            rows = c.execute("SELECT ts, value, meta FROM metrics WHERE day=? AND metric='app.usage_minutes'",
                             (day,)).fetchall()
            per = {}
            for r in rows:
                try:
                    meta = json.loads(r["meta"] or "{}")
                except Exception:
                    meta = {}
                per[meta.get("pkg") or meta.get("app") or "?"] = (meta.get("app", ""), int(float(r["value"] or 0)))
            games = {n: m for pkg, (n, m) in per.items()
                     if is_game(n, pkg) and m >= int(CFG.get("privacy", {}).get("game_min_minutes", 10))}
            if games:
                out["games_minutes_today"] = dict(sorted(games.items(), key=lambda kv: -kv[1]))
        except Exception:
            pass
    # ★ P0 数据健康度：只列"有问题的源"，终端/挂件上一眼看到
    try:
        out["data_health"] = {s: {"verdict": v["verdict"], "note": v["note"],
                                  "fresh_hours": v["fresh_hours"]}
                              for s, v in source_health().items() if v["verdict"] != "ok"}
    except Exception as e:
        print(f"[care:health] {str(e)[:60]}", flush=True)
    return out


def llm_context(day=None):
    """**唯一**允许进入模型的上下文：分类 + 粗粒度 + 小时级。返回 (payload, dropped 清单)。"""
    day = day or today_str()
    dropped = []
    ctx = {"date": day, "weekday": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][
        datetime.now(TZ).weekday()], "notes": []}

    with db() as c:
        # 睡眠（30 分钟粒度 + 小时级时间）
        for metric, _label in (("sleep.total_minutes", "睡眠"),):
            m = _latest_metric(c, metric)
            if m and m.get("value") is not None:
                ctx["sleep"] = {"minutes_rounded": blur_minutes(m["value"]), "at": hour_only(m["ts"])}
                dropped.append("睡眠原始值 + 精确时间")

        # 健康数值（保留数值：判断必需；去掉来源/原文）
        for metric, key in (("health.heart_rate", "heart_rate"), ("health.spo2", "spo2"),
                            ("health.stress", "stress")):
            m = _latest_metric(c, metric, days=1)
            if m and m.get("value") is not None:
                v = float(m["value"])
                if key == "stress":
                    v = blur_minutes(v, int(CFG["privacy"].get("blur_stress_to", 10)))
                ctx.setdefault("health", {})[key] = {"value": v, "at": hour_only(m["ts"])}
                dropped.append(f"{key} 的数据来源与通知原文")

        # 屏幕使用：**按分类聚合分钟数**（不是 App 个数！）
        # 踩过的坑：这里原来给的是"分类下有几个 App"（如 {"短视频":1,"社交":2}），
        #           模型读成"主人只刷了 1 条短视频、2 条社交"，于是说了胡话。
        #           现在把"单位=分钟"写进字段名，且只给分钟数。
        # ★ 口径：每个 App 只取当天**最新**一次的累计值，再按分类相加。
        #   踩过的坑：App 每轮上报的是"当天累计分钟"，一天会重复上报很多次；
        #            直接 SUM 会把同一段时间重复加（实测 B站 96 分钟被算成 154）。
        rows = c.execute("SELECT ts, value, meta FROM metrics WHERE day=? "
                         "AND metric='app.usage_minutes' ORDER BY ts ASC", (day,)).fetchall()
        per_app = {}
        for t in rows:
            try:
                meta = json.loads(t["meta"] or "{}")
            except Exception:
                meta = {}
            key = meta.get("pkg") or meta.get("app") or "?"
            per_app[key] = (meta.get("app", ""), int(float(t["value"] or 0)))   # 后写覆盖先写 = 取最新
        cats = {}
        for pkg, (name, mins) in per_app.items():
            lab = cat_app(name, pkg)
            cats[lab] = cats.get(lab, 0) + mins
        screen_peak, _n = _peak_metric(c, day, "screen.active_minutes")
        # 音乐：正在听的那首 + 今天听过几首（酷狗的 artist 字段会带歌名，顺手清一下）
        try:
            rows = c.execute("SELECT ts, meta FROM metrics WHERE metric='music.track' "
                             "ORDER BY ts DESC LIMIT 30").fetchall()
            if rows:
                import re as _re
                seen = []
                now_ts = datetime.now(TZ)
                for r in rows:
                    try:
                        m = json.loads(r["meta"] or "{}")
                    except Exception:
                        m = {}
                    t = (m.get("title") or "").strip()
                    if not t or t in seen:
                        continue
                    seen.append(t)
                latest = rows[0]
                try:
                    lm = json.loads(latest["meta"] or "{}")
                except Exception:
                    lm = {}
                try:
                    fresh = (now_ts - datetime.fromisoformat(latest["ts"])).total_seconds() < 15 * 60
                except Exception:
                    fresh = False
                if fresh and lm.get("playing"):
                    a = (lm.get("artist") or "").strip()
                    t = (lm.get("title") or "").strip()
                    # 实测（酷狗）：artist 里塞的是"歌手-上一首/下一首"，没法用；
                    # 而 title 常常是"歌名 - 歌手" → **优先从 title 里拆**，拆不出来才信 artist。
                    if " - " in t:
                        t, a2 = t.split(" - ", 1)
                        t, a = t.strip(), a2.strip()
                    elif a and (t in a or a.endswith(t)):
                        a = _re.sub(r"[-—]?\s*" + _re.escape(t) + r"\s*$", "", a).strip(" -—")
                    elif " - " in a or a.count("-") >= 2:
                        a = ""          # 明显是拼起来的脏字段，宁可不给歌手
                    ctx["listening_now"] = {"title": t, "artist": a}
                    dropped.append("音乐：只给曲名/歌手（不含播放进度、播放列表）")
                if len(seen) >= 2:
                    ctx["tracks_today_count"] = len(seen)
        except Exception:
            pass

        # ★ 电脑体检（新数据源）：只给百分比与次数 —— 进程名/窗口标题/文件名一律不出本机
        pc = {}
        for m, k in (("pc.disk_free_percent", "disk_free_percent"),
                     ("pc.mem_percent", "mem_percent"),
                     ("pc.uptime_hours", "uptime_hours"),
                     ("pc.window_switches_today", "window_switches_today")):
            v = _latest_metric(c, m, days=1)
            if v and v.get("value") is not None:
                pc[k] = round(float(v["value"]), 1)
        # ★ 个人基线：相对他自己历史的偏离（只有类别/指标名 + 百分比，不含 App 名）
        try:
            _sur = surprise_report()
            _flags = {k: v["text"] for k, v in _sur.items() if v.get("flag")}
            if _flags:
                ctx["surprise"] = _flags
                _top = top_surprise(_sur)
                if _top:
                    ctx["most_notable"] = _top
                dropped.append("基线对比：只给类别名与百分比（不给 App 名、不给原始历史）")
        except Exception:
            pass
        if pc:
            ctx["pc_health"] = pc
            dropped.append("电脑：只给百分比/次数（不给进程名、窗口标题、文件名）")

        # 游戏：单独拎出来，**带名字**（"今天明日方舟 72 分钟"才有价值）；少于 10 分钟不提
        games = {name: mins for pkg, (name, mins) in per_app.items()
                 if is_game(name, pkg) and mins >= int(CFG.get("privacy", {}).get("game_min_minutes", 10))}
        if games:
            ctx["games_minutes_today"] = dict(sorted(games.items(), key=lambda kv: -kv[1]))
            dropped.append("非游戏的 App 名（游戏名保留：点评游戏必须点名）")

        # 订单/快递：**只给聚合**，不给商品名、不给精确金额（隐私红线）
        try:
            d0 = (datetime.now(TZ) - timedelta(days=7)).strftime("%Y-%m-%d")
            rows = c.execute("SELECT value, meta FROM metrics WHERE metric='order.event' "
                             "AND day >= ?", (d0,)).fetchall()
            ship = pay = 0
            bands = {}
            for r in rows:
                try:
                    m = json.loads(r["meta"] or "{}")
                except Exception:
                    m = {}
                t = m.get("type", "")
                if t in ("发货", "派送", "签收"):
                    ship += 1
                elif t == "支付":
                    pay += 1
                    b = m.get("band") or "未知"
                    bands[b] = bands.get(b, 0) + 1
            if ship or pay:
                ctx["deliveries_7d"] = ship
                ctx["orders_7d"] = pay
                if bands:
                    ctx["order_amount_bands_7d"] = bands   # 只给区间计数，绝不给具体金额
                dropped.append("订单商品名、店铺、精确金额（只留类型与区间）")
        except Exception:
            pass

        pv = CFG.get("privacy", {})
        talk = tuple(pv.get("talkative_categories") or ("短视频/视频", "游戏", "社交", "购物/生活"))
        min_min = int(pv.get("min_talk_minutes", 30))
        # 只保留"值得说"的分类，且用量得够看（学习/办公/工具/其他 全部不进上下文）
        cats = {k: v for k, v in cats.items() if k in talk and v >= min_min}
        if cats:
            ctx["screen_usage_minutes_by_category"] = dict(
                sorted(cats.items(), key=lambda kv: -kv[1]))
            dropped.append("具体 App 名（只保留分类）")
            dropped.append("学习/办公/工具/其他 等无依据的分类")
        else:
            ctx["screen_usage_note"] = "今天没有值得评价的娱乐/社交用量（少于 30 分钟或只有工具类）"
        if _n:
            ctx["screen_total_minutes_today"] = int(screen_peak)
        # 天气（今天/明天）—— 出门带伞、降温加衣这类提醒的依据
        # 当前活跃设备（谁最近在上报）→ 它的城市；没有就默认城市
        try:
            with db() as c:
                row = c.execute("SELECT device FROM metrics WHERE metric LIKE 'screen%' OR metric='app.usage_minutes' "
                                "ORDER BY ts DESC LIMIT 1").fetchone()
            active = row["device"] if row else None
            want = ((CFG.get("devices") or {}).get(active) or {}).get("city_code") \
                or (CFG.get("privacy") or {}).get("weather_city_code") or "101280101"
            ctx["weather_city"] = {"code": str(want), "for_device": active}
        except Exception:
            want = None

        def _match(m):
            return (not want) or str(m.get("city_code") or want) == str(want)

        w0, w1 = None, None
        _days = []
        try:
            with db() as c:
                rows = c.execute("SELECT ts, metric, value, meta FROM metrics WHERE metric IN ('weather.now','weather.day','weather.alert','weather.life') ORDER BY ts DESC LIMIT 40").fetchall()
            for r in rows:
                if not _match(json.loads(r["meta"] or "{}")):
                    continue
                m = json.loads(r["meta"] or "{}")
                age = (datetime.now(TZ) - datetime.fromisoformat(r["ts"])).total_seconds() / 60
                if r["metric"] == "weather.now" and age < 180 and "weather_now" not in ctx:
                    # 白名单：只放"城市级"信息。
                    # 存储里的 meta 还带着 lat/lon（那是给天气接口用的），**绝不能让它们进模型**
                    # —— 注入式脱敏测试就是这么抓到这条泄漏的。
                    _keep = ("city", "desc", "humidity", "wind", "rain_1h", "rain_24h",
                             "aqi", "vis_km", "observed_at")
                    ctx["weather_now"] = {k: m[k] for k in _keep if k in m}
                    ctx["weather_now"]["age_minutes"] = int(age)
                    if any(k in m for k in ("lat", "lon")):
                        dropped.append("位置：只给城市级天气（精确经纬度只用于取天气，不进模型）")
                elif r["metric"] == "weather.alert" and age < 720 and "weather_alert" not in ctx:
                    ctx["weather_alert"] = m
                elif r["metric"] == "weather.life" and age < 720 and "weather_life_tips" not in ctx:
                    ctx["weather_life_tips"] = {k: v for k, v in m.items() if k not in ("src", "city_code", "city")}
                elif r["metric"] == "weather.day" and age < 720:
                    _days.append(m)
        except Exception:
            pass
        # ★ 天气的"今天/明天"必须按**真实日期**取（2026-09-29 修的真 bug）：
        #   原来直接对 date 做字符串排序，而主源给的是**不补零**的 "9/28" →
        #   "10/1" < "9/28" ⇒ 9→10 月交界时"今天"被挑成三天后、"明天"被挑成昨天 ✗
        #   （真实语料里"明天有冰雹""早上提醒明早的干嘛"就是这么来的）
        #   而且**不能按索引取第 N 条**：今天的行一旦缺失，"第 0 条"就成了明天
        #   ⇒ 后天的天气被当成今天讲。所以按 for_day 精确匹配，缺了就**不给**。
        _wd = weather_days(city_code=want)
        _by_day = {str(x.get("for_day")): x for x in _wd}
        _t = datetime.now(TZ).date()
        w0 = w0 or _by_day.get(_t.isoformat())
        w1 = w1 or _by_day.get((_t + timedelta(days=1)).isoformat())
        if w0 is None:
            w0 = weather_of(0)
        if w1 is None:
            w1 = weather_of(1)
        if w0:
            ctx["weather_today"] = w0
            dropped.append("位置：只用城市级天气，不采集定位")
        if w1:
            ctx["weather_tomorrow"] = w1

        # 电量 / 充电 / 下一个闹钟（手机端报上来的）
        try:
            with db() as c:
                b = c.execute("SELECT value, ts FROM metrics WHERE metric='device.battery_percent' "
                              "ORDER BY ts DESC LIMIT 1").fetchone()
                ch_ = c.execute("SELECT value, ts FROM metrics WHERE metric='device.charging' "
                                "ORDER BY ts DESC LIMIT 1").fetchone()
                al = c.execute("SELECT value, meta, ts FROM metrics WHERE metric='device.next_alarm' "
                               "ORDER BY ts DESC LIMIT 1").fetchone()
            if b and (datetime.now(TZ) - datetime.fromisoformat(b["ts"])).total_seconds() < 6 * 3600:
                ctx["battery_percent"] = int(float(b["value"]))
                if ch_:
                    ctx["battery_charging"] = bool(float(ch_["value"]))
            # 蓝牙外设电量（耳机/手表/键鼠）—— 按设备名取最新值
            bt = {}
            try:
                rows = c.execute("SELECT ts, value, meta FROM metrics WHERE metric='bt.battery_percent' "
                                 "ORDER BY ts DESC LIMIT 20").fetchall()
                for r in rows:
                    try:
                        m = json.loads(r["meta"] or "{}")
                    except Exception:
                        m = {}
                    nm = (m.get("name") or "").strip()
                    if not nm or nm in bt:
                        continue
                    if (datetime.now(TZ) - datetime.fromisoformat(r["ts"])).total_seconds() > 12 * 3600:
                        continue
                    bt[nm] = {"percent": int(float(r["value"] or 0)), "kind": m.get("kind") or "蓝牙设备"}
            except Exception:
                pass
            if bt:
                ctx["bluetooth_batteries"] = bt
                dropped.append("蓝牙 MAC（只留设备名与电量）")

            if al:
                try:
                    m = json.loads(al["meta"] or "{}")
                except Exception:
                    m = {}
                mins = int(float(al["value"]))
                if 0 <= mins < 1440 and (datetime.now(TZ) - datetime.fromisoformat(al["ts"])).total_seconds() < 24 * 3600:
                    ctx["next_alarm_at"] = f"{mins // 60:02d}:{mins % 60:02d}"
        except Exception:
            pass

        ctx["_unit_note"] = ("所有 *_minutes 字段单位都是分钟，是当天累计值；不要自行推断单位或比较条数。"
                             "只列了值得说的分类 —— 学习/办公/工具这类没有判定依据，不要评价")

        # 课程：课名可能带班级/教师等，只给"第几节 + 类型"
        courses = courses_on(datetime.now(TZ).date())
        if courses:
            ctx["classes"] = [{"start_hour": c["start"][:2] + "时", "periods": c["periods"],
                               "kind": "上课"} for c in courses]
            dropped.append("课程名 / 教师 / 教室（只保留节次与类型）")

        # 日程：标题 → 类型
        events = calendar_today(day)
        if events:
            ctx["calendar"] = [{"type": cat_event(e["title"]), "start_hour": (e.get("start") or "")[:2] + "时"}
                               for e in events]
            dropped.append("日程标题原文 / 地点（只保留类型）")

        # 设备健康度（只给"活没活"，不给设备标识）
        rows = c.execute("SELECT device, MAX(ts) last FROM metrics GROUP BY device").fetchall()
        ctx["sources"] = sorted({blur_device(r["device"]) for r in rows})

    # ★ P0 数据健康度：告诉模型"这台设备今天的数据可不可信"。
    #   为什么必须给模型：实测有些日子采集器基本没跑（只有 2 条上报），
    #   若当成真实行为，她会说"主人今天几乎没碰手机"——那是**编的**。
    # ★ P3 主动提问：如果这轮本来就要主动开口，可以把它变成一个**有数字**的问题
    try:
        ctx["ask"] = question_now() or {}
        ctx["_ask_note"] = ("ask 有内容时：这轮可以**用一句话问她一个问题**（带上里面的数字）；"
                            "没有内容就别硬找话题。问题一天最多一个。")
    except Exception:
        ctx["ask"] = {}
    # ★ P1 情节记忆：她能"记得住"——只给她**检索出来的那几条摘要**，不是整库
    try:
        _ep = episodes_recent(days=14, limit=5)
        ctx["memory"] = [e["summary"] for e in _ep]
        ctx["_memory_note"] = ("memory 是最近说过/发生过的事，可以用来说「你上周说过…」这种连续性的话；"
                               "但**不要逐条复述**，也不要把它当今天的事实（今天的看上面的字段）。")
    except Exception:
        ctx["memory"] = []
    try:
        _h = source_health()
        ctx["data_health"] = {s: {"verdict": v["verdict"], "note": v["note"]}
                              for s, v in _h.items() if v["verdict"] != "ok"}
        _bad = [s for s, v in _h.items() if v["verdict"] != "ok"]
        ctx["_data_note"] = (
            "data_health 里列出的源，今天的数据**不完整或已过期，不要据此评价主人**（宁可不说）；"
            "只有列出来的才有问题，没列出来的正常。" if _bad else
            "所有数据源今天的上报都完整，可以正常使用。")
    except Exception as e:
        ctx["data_health"] = {}
        ctx["_data_note"] = "数据健康度算不出来（不影响其他字段）"
        print(f"[health] {str(e)[:80]}", flush=True)
        dropped.append("设备名与型号")

        # 待办：只给条数
        n_todo = c.execute("SELECT COUNT(*) n FROM metrics WHERE day=? AND metric='task.todo'",
                           (day,)).fetchone()["n"]
        if n_todo:
            ctx["todo_count"] = int(n_todo)
            dropped.append("待办原文（只保留条数）")

    ctx["_privacy"] = "以上内容不含通知原文/日程标题/App 名/分钟级时间；原始数据只留在本服务器"
    return ctx, sorted(set(dropped))


