# ----------------------------------------------------------------- 规则引擎
def _latest_metric(c, metric, days=3):
    """取最近一条该指标（往回找 days 天）。"""
    since = (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    row = c.execute(
        "SELECT * FROM metrics WHERE metric=? AND day>=? ORDER BY ts DESC LIMIT 1",
        (metric, since)).fetchone()
    return dict(row) if row else None


def _sum_metric(c, day, metric):
    row = c.execute(
        "SELECT COALESCE(SUM(value),0) AS v, COUNT(*) AS n FROM metrics WHERE day=? AND metric=?",
        (day, metric)).fetchone()
    return float(row["v"] or 0), int(row["n"] or 0)


def _streak_low_sleep(c, threshold, days=5):
    """连续多少天睡眠低于阈值（用每天最后一条睡眠总时长）。"""
    streak = 0
    for i in range(0, days):
        day = (datetime.now(TZ) - timedelta(days=i + 1)).strftime("%Y-%m-%d")
        row = c.execute(
            "SELECT value FROM metrics WHERE day=? AND metric='sleep.total_minutes' ORDER BY ts DESC LIMIT 1",
            (day,)).fetchone()
        if row and row["value"] is not None and row["value"] < threshold:
            streak += 1
        else:
            break
    return streak


def _deep_night_active(c, day):
    """当天深度时段（如 23:00-02:00）是否有活跃记录。"""
    a, b = CFG["rules"]["deep_night_hours"]
    rows = c.execute(
        "SELECT ts FROM metrics WHERE day=? AND metric IN ('screen.active','pc.active')", (day,)).fetchall()
    for r in rows:
        try:
            h = datetime.fromisoformat(r["ts"]).hour
        except Exception:
            continue
        if a <= h <= 23 or 0 <= h < b:
            return True
    return False


def _peak_metric(c, day, metric):
    """累计型指标（屏幕时长 / 步数 / 各 App 使用）取当日**最大值**，不是求和。

    踩过的坑：设备每轮上报的是"当天累计值"（例如 429 分钟，下一轮还是 429），
    求和会把它翻倍 → 会误判成"今天屏幕前 14 小时"。
    """
    try:
        row = c.execute("SELECT MAX(value) AS v, COUNT(*) AS n FROM metrics WHERE day=? AND metric=?",
                        (day, metric)).fetchone()
        return (row["v"] or 0), int(row["n"] or 0)
    except Exception:
        return 0, 0


def daily_digest(day=None):
    """当日客观小结（只报数，不判断），给简报和终端用。"""
    day = day or today_str()
    with db() as c:
        screen, n_screen = _peak_metric(c, day, "screen.active_minutes")
        sleep = _latest_metric(c, "sleep.total_minutes")
        ev = c.execute("SELECT COUNT(*) AS n FROM metrics WHERE day=? AND metric='calendar.event'",
                       (day,)).fetchone()["n"]
    try:
        ncls = len(courses_on(datetime.now(TZ).date()))
    except Exception:
        ncls = 0
    return {
        "screen_minutes": int(screen) if n_screen else None,
        "sleep_minutes": int(sleep["value"]) if sleep and sleep.get("value") is not None else None,
        "calendar_events": int(ev or 0),
        "classes_today": ncls,
    }


def digest_line(day=None):
    d = daily_digest(day)
    bits = []
    if d["screen_minutes"] is not None:
        bits.append(f"屏幕 {d['screen_minutes'] // 60} 小时 {d['screen_minutes'] % 60} 分")
    if d["sleep_minutes"] is not None:
        bits.append(f"睡眠 {d['sleep_minutes'] // 60} 小时 {d['sleep_minutes'] % 60} 分")
    if d["classes_today"]:
        bits.append(f"课程 {d['classes_today']} 节")
    if d["calendar_events"]:
        bits.append(f"日程 {d['calendar_events']} 项")
    return ("· 数据小结：" + " · ".join(bits)) if bits else ""


def est_bedtime():
    """从睡眠记录推算"通常几点睡"：入睡 ≈ 通知到达时刻 − 睡眠时长。返回 (分钟数, 样本数)。

    为什么这么算：vivo 健康的睡眠通知是**起床时**推来的，自带时长 —— 两者一减就是入睡时刻。
    """
    with db() as c:
        rows = c.execute("SELECT ts, value FROM metrics WHERE metric='sleep.total_minutes' "
                         "ORDER BY ts DESC LIMIT 7").fetchall()
    beds = []
    for r in rows:
        try:
            if not r["value"]:
                continue
            t = datetime.fromisoformat(r["ts"])
            bed = (t.hour * 60 + t.minute - int(float(r["value"]))) % 1440
            beds.append(bed)
        except Exception:
            continue
    if len(beds) < 2:
        return 22 * 60 + 30, len(beds)          # 样本不足：保守回落 22:30
    beds.sort()
    return beds[len(beds) // 2], len(beds)


def bedtime_brief():
    """睡前小总结：**简单**为主 —— 今天屏幕多久、上了几节课、明早第一节几点。"""
    d = daily_digest()
    bed_min, n = est_bedtime()
    lines = ["（认真）今天到这儿，鲸鲸给你收个尾："]
    bits = []
    if d["screen_minutes"] is not None:
        bits.append(f"屏幕 {d['screen_minutes'] // 60} 小时 {d['screen_minutes'] % 60} 分")
    if d["classes_today"]:
        bits.append(f"上了 {d['classes_today']} 节课")
    if bits:
        lines.append("· " + " · ".join(bits))
    # ⚠️ 只看"明天"这一天：明天没课就**一句话都不提**（别拿后天的课冒充明早）
    tm = courses_tomorrow()
    if tm:
        lines.append(f"· 明早第一节 {tm[0]['start']}（{tm[0].get('room') or '教室'}）")
    lines.append(f"· 睡点 {bed_min // 60:02d}:{bed_min % 60:02d}"
                 + ("" if n >= 2 else "（先按默认，攒几天睡眠数据就更准）"))
    lines.append("晚安，早点睡。")
    return "\n".join(lines)


def human_close(kind="brief_evening"):
    """收尾的一句人话：把关键数字揉进自然语序，而不是"· 数据小结：A · B"。"""
    d = daily_digest()
    bits = []
    if kind == "brief_morning":
        nxt = course_next_today()     # ⚠️ 今天没有课就什么也不说（别报明天的）
        if nxt and nxt.get("start"):
            bits.append(f"第一节 {nxt['start']} 在 {nxt.get('room') or '教室'}")
        return ("（翻了下你的课表）" + "，".join(bits) + "。") if bits else ""
    # 复盘（默认）
    if d["screen_minutes"] is not None:
        h, m = d["screen_minutes"] // 60, d["screen_minutes"] % 60
        bits.append(f"今天屏幕 {h} 小时" + (f" {m} 分" if m else ""))
    if d["classes_today"]:
        bits.append(f"上了 {d['classes_today']} 节课")
    tm = courses_tomorrow()          # ⚠️ 只看明天；明天没课就只说晚安，不提课
    tail = f"明早第一节 {tm[0]['start']}，早点休息" if tm else "早点休息，晚安"
    if bits:
        return "（小声提醒）" + "，".join(bits) + " —— " + tail + "。"
    return "（小声提醒）" + tail + "。"


def analyze(day=None):
    """算出当天的提醒清单（确定性规则，不烧 token）。返回 [{'level','text'}]"""
    day = day or today_str()
    r = CFG["rules"]
    out = []
    with db() as c:
        # ① 睡眠
        sleep = _latest_metric(c, "sleep.total_minutes")
        if sleep and sleep["value"] is not None:
            mins = int(sleep["value"])
            if mins < r["sleep_low_minutes"]:
                streak = _streak_low_sleep(c, r["sleep_low_minutes"])
                lvl = "urgent" if streak >= r["sleep_low_streak_days"] else "warn"
                extra = f"，已经连着 {streak} 天了" if streak >= 1 else ""
                out.append({"level": lvl,
                            "text": f"（担心）昨晚只睡了 {mins // 60} 小时 {mins % 60} 分{extra}，今天别硬撑。"})
            elif mins >= 480:
                out.append({"level": "info", "text": f"（满意）昨晚睡了 {mins // 60} 小时，睡得不错。"})

        # ② 屏幕 / 使用时长
        active, n = _peak_metric(c, day, "screen.active_minutes")
        if n and active > r["screen_high_minutes"]:
            out.append({"level": "warn",
                        "text": f"（小声提醒）屏幕前坐了 {int(active) // 60} 小时了，起来走两步。"})

        # ②.5 健康数据太久没更新 → 提醒去同步（不然她"看不见你"）
        h2 = CFG.get("health", {})
        watch = _latest_metric(c, "health.heart_rate", days=7) or _latest_metric(c, "sleep.total_minutes", days=7)
        if watch:
            try:
                age_h = (datetime.now(TZ) - datetime.fromisoformat(watch["ts"])).total_seconds() / 3600
                if age_h > h2.get("stale_after_minutes", 180) / 60:
                    out.append({"level": "info",
                                "text": f"（歪头）手表已经 {int(age_h)} 小时没同步了，打开 vivo 健康刷一下。"})
            except Exception:
                pass

        # ③ 深夜还活跃
        if _deep_night_active(c, day):
            out.append({"level": "warn", "text": "（认真）这么晚还亮着屏，该睡了。"})

        # ④ 久坐（最近一条连续活跃时长）
        sit = _latest_metric(c, "pc.continuous_active_minutes", days=1)
        if sit and sit["value"] and sit["value"] >= r["sit_continuous_minutes"]:
            out.append({"level": "warn",
                        "text": f"（提醒你一句）连着坐了 {int(sit['value'])} 分钟，站起来抻一下。"})

        # ⑤ 待办（task.todo：value=1，meta.text 写内容）
        todos = c.execute(
            "SELECT value, meta FROM metrics WHERE day=? AND metric='task.todo' ORDER BY ts ASC", (day,)).fetchall()
        done = c.execute(
            "SELECT COUNT(*) AS n FROM metrics WHERE day=? AND metric='task.done'", (day,)).fetchone()["n"]
        if todos:
            items = []
            for t in todos:
                try:
                    txt = (json.loads(t["meta"] or "{}") or {}).get("text") or ""
                except Exception:
                    txt = ""
                if txt:
                    items.append(txt)
            if items:
                out.append({"level": "info",
                            "text": f"（翻开记事本）今天要做：{'、'.join(items[:6])}"
                                    + (f"，已完成 {done} 项" if done else "")})

        # ⑥ 健康异常（阈值命中 → urgent，会直推）
        h = CFG.get("health", {})
        checks = [
            ("health.heart_rate", "gt", h.get("heart_rate_high", 110), "（有点急）心率到 {v} 了，先坐下缓一缓；要是持续偏高，去校医院看看。"),
            ("health.heart_rate", "lt", h.get("heart_rate_low", 45), "（皱起眉）心率只有 {v}，偏低——如果还伴随头晕，尽快找人陪着。"),
            ("health.spo2", "lt", h.get("spo2_low", 92), "（认真）血氧只有 {v}%，深呼吸、通风；低于 90 要就医。"),
            ("health.stress", "gt", h.get("stress_high", 80), "（担心）压力值 {v}，偏高。手上的事放 10 分钟，喝口水再继续。"),
            ("sleep.total_minutes", "lt", h.get("sleep_low_minutes", 300), "（看了下你的睡眠数据）昨晚只睡了 {v} 分钟，太少了——今天别硬撑，晚上早睡。"),
        ]
        for metric, op, limit, tpl in checks:
            m = _latest_metric(c, metric, days=1)
            if not m or m.get("value") is None:
                continue
            v = float(m["value"])
            try:
                ts = datetime.fromisoformat(m["ts"])
            except Exception:
                continue
            if ts < datetime.now(TZ) - timedelta(hours=6):      # 只关心最近 6 小时的数据
                continue
            hit = (v > float(limit)) if op == "gt" else (v < float(limit))
            if hit:
                out.append({"level": "urgent", "text": tpl.format(v=int(v) if abs(v) >= 20 else round(v, 1))})

        # ⑦ 课表：今天有课 & 明早第一节课
        tt = _timetable()
        if tt:
            today_courses = courses_on(datetime.now(TZ).date(), tt)
            if today_courses:
                first = today_courses[0]
                out.append({"level": "info",
                            "text": f"（翻了下你的课表）今天 {len(today_courses)} 节课，第一节 {first['start']} {first['name']}"
                                    + (f"（{first['room']}）" if first["room"] else "") + "。"})
            tomorrow = courses_on((datetime.now(TZ) + timedelta(days=1)).date(), tt)
            if tomorrow:
                t_first = tomorrow[0]
                try:
                    hh = int(t_first["start"].split(":")[0])
                except Exception:
                    hh = 9
                # ★ 2026-09-27 修（用户原话"早上提醒明早的干嘛"）：
                #   原来只看"明天第一节早于 9 点" ✗ **不看现在几点** ✓
                #   → 早间简报里也冒出"明早 08:00 有课…今晚别熬太晚" ✗ 时间和语境全错位 ✓
                #   修法：按**时间语境**分流 ——
                #     傍晚/深夜（18:00~05:00）：睡前口径"明早…今晚别熬太晚" ✓
                #     白天（含早间简报）：中性口径"明天 X 第一节有课" ✓（信息保留，话别说错 ✓）
                now_h = datetime.now(TZ).hour
                if hh < 9:
                    if now_h >= 18 or now_h < 5:
                        out.append({"level": "warn",
                                    "text": f"明早 {t_first['start']} 有课（{t_first['name']}），今晚别熬太晚。"})
                    else:
                        out.append({"level": "info",
                                    "text": f"明天 {t_first['start']} 第一节有课（{t_first['name']}）。"})

        # ⑦ 日程：今天还有什么安排
        events = calendar_today(day)
        if events:
            titles = "、".join(f"{e['start']} {e['title']}" for e in events[:5])
            out.append({"level": "info", "text": f"今天日程：{titles}。"})

        # ⑧ 设备失联
        limit = datetime.now(TZ) - timedelta(hours=CFG["rules"]["device_offline_hours"])
        rows = c.execute("SELECT device, MAX(ts) AS last FROM metrics GROUP BY device").fetchall()
        for row in rows:
            try:
                last = datetime.fromisoformat(row["last"])
            except Exception:
                continue
            if last < limit:
                age_h = (datetime.now(TZ) - last).total_seconds() / 3600
                # ★ 2026-09-25 修：这条曾经**反复念**（用户原话"怎么一直提醒我卡住了"）
                #   原因有二：
                #   ① 文案里带小时数（101 → 110）✗ 而下游去重是"原文完全相同才跳过" ✓
                #      → 数字一变就被当成新消息 ✗ → 每轮都发 ✓ 所以改成按**天**取值 ✓
                #      这样同一台设备在一天之内文案完全一致 ✓ 去重就能拦住 ✓
                #   ② 已弃用的设备会**永远**满足"失联" ✓ 一直念 ✓
                #      → 失联超过 3 天就不再提醒（那不是"没同步"，是设备不用了 ✓）
                if age_h > CFG["rules"].get("device_abandon_hours", 72):
                    continue
                days = max(1, int(age_h // 24))
                out.append({"level": "warn",
                            "text": f"{row['device']} 的数据已经 {days} 天没同步了，看一眼设备。"})
    return out


def persona_line(level):
    """按人设给提醒配一句开场（先用模板，接入 LLM 后换成模型生成）。"""
    return {
        "urgent": "（有点急）主人，先说要紧的 ——",
        "warn": "（认真）主人，鲸鲸提醒你一下 ——",
        "info": "（看了下今天的数据）主人，是这样：",
    }.get(level, "")


def compose_brief(kind="brief_morning"):
    """生成一条当日简报并入库（发队列）。"""
    # 只挑最重要的 3 条（urgent → warn → info），多了像报告，不像人说话
    order = {"urgent": 0, "warn": 1, "info": 2}
    raw_items = sorted(analyze(), key=lambda i: order.get(i["level"], 3))
    # 已经单独推过的异常，不再塞进简报里重复说一遍（一条消息只说一件事）
    with db() as c:
        already = {r["text"] for r in c.execute(
            "SELECT text FROM reminders WHERE day=? AND kind='alert'", (today_str(),)).fetchall()}
    items = [i for i in raw_items if i["text"] not in already][:2]
    head = "（把今天理了理）主人早。" if kind == "brief_morning" else "（整理了下今天）今天到这儿。"
    lines = [head] + [i["text"] for i in items]
    tail = human_close(kind)
    if tail:
        lines.append(tail)
    lines = [ln.strip() for ln in lines if ln and ln.strip()]
    text = "\n".join(lines)
    level = "warn" if any(i["level"] in ("warn", "urgent") for i in items) else "info"
    # ★ 拆分入队：一句一条（打招呼 / 每个提醒 / 收尾各自一条）
    #   这样微信端是"一条一条来"，每条只做一件事、带自己的（动作），不会被糊成一大段
    ids = []
    with db() as c:
        for i, line in enumerate(lines):
            cur = c.execute(
                "INSERT INTO reminders(day, kind, level, text, created_at, status) VALUES (?,?,?,?,?,'new')",
                (today_str(), kind if i == 0 else kind + "_part", level, line, now_iso()))
            ids.append(cur.lastrowid)
    rid = ids[0]
    print(f"[brief] #{rid} {kind} 拆成 {len(lines)} 条入队", flush=True)
    push_terminals(rid, text, level, kind)          # 终端（webhook）拿全文，自行决定怎么显示
    push_wecom(text)                                 # 企业微信发全文（那边不是主通道）
    return rid, text


def band_now(when=None):
    """场景桶 = 星期类 × 时段带。

    ★ 必须与说话层 `whale_adapt.band_key()` **逐字一致**（工作日/周末 × 早上/白天/睡前/深夜）。
      两边名字对不上，桶后验就永远取不到 → 分桶 Thompson 静默退化成全局后验。
      以前的坑就在这：反馈（挂件点 ✓/✗）上报时不带桶，全部落进 `(无桶)`，
      桶里永远没样本。现在中枢自己按上报时刻算，客户端一行都不用改。
      一致性有跨模块测试盯着：tests/test_band_consistency.py。
    """
    dt = when or datetime.now(TZ)
    wk = "周末" if dt.weekday() >= 5 else "工作日"
    m = dt.hour * 60 + dt.minute
    if 6 * 60 + 30 <= m < 10 * 60:
        band = "早上"
    elif m >= 21 * 60 + 30 or m < 30:
        band = "睡前"
    elif m < 6 * 60 + 30:
        band = "深夜"
    else:
        band = "白天"
    return "%s·%s" % (wk, band)
