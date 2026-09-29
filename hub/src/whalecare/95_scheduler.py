# ----------------------------------------------------------------- 调度线程
def _close_recently(last: dict, now) -> bool:
    """两小时内是否已经收过尾（晚间简报 22:30 / 睡前总结 22:15 内容重叠 ✗ 只留一条 ✓）。"""
    try:
        t = datetime.fromisoformat(last.get("close_at") or "")
        return (now - t).total_seconds() < 2 * 3600
    except Exception:
        return False


def scheduler():
    """每 5 秒看一眼：到点生成简报 / 发现异常立刻说（不依赖外部 cron）。

    为什么 5 秒不是 30 秒：异常数据从"到达中枢"到"你手机收到"要让用户感觉是实时的，
    规则引擎的巡检间隔直接叠加在总延迟上。代价只是每 5 秒几条 SQLite 查询，很便宜。
    """
    last = load_state()          # ⑥ 落盘状态：重启不重复发、也不漏发
    while True:
        try:
            now = datetime.now(TZ)
            hm = now.strftime("%H:%M")
            day = now.strftime("%Y-%m-%d")
            # ---- ⑦ 自动清理超期数据（默认 365 天；retention_days=0 表示永久保留）----
            try:
                keep_days = int(CFG.get("privacy", {}).get("retention_days", 365) or 0)
                if keep_days > 0 and last.get("pruned") != day:
                    cutoff = (datetime.now(TZ) - timedelta(days=keep_days)).strftime("%Y-%m-%d")
                    with db() as c:
                        n = c.execute("DELETE FROM metrics WHERE day < ?", (cutoff,)).rowcount
                        n2 = c.execute("DELETE FROM reminders WHERE day < ?", (cutoff,)).rowcount
                    if n or n2:
                        print(f"[retention] 清理 {keep_days} 天前数据：metrics {n} 行 / reminders {n2} 行", flush=True)
                    last["pruned"] = day
                    save_state(last)
            except Exception as e:
                print(f"[retention] {e}", flush=True)

            # ---- 天气：每 3 小时刷一次 ----
            try:
                if not last.get("weather_at") or (now - last["weather_at"]).total_seconds() > 3 * 3600:
                    fetch_weather()
                    last["weather_at"] = now
                    save_state(last)
            except Exception as e:
                print(f"[weather] {e}", flush=True)

            # ---- 睡前小总结：每天在你"推算的入睡时刻"前 15 分钟发一次 ----
            try:
                bed_min, _n = est_bedtime()
                fire_hm = f"{(bed_min - 15) % 1440 // 60:02d}:{(bed_min - 15) % 1440 % 60:02d}"
                if hm == fire_hm and last.get("bed") != day:
                    # ★ 2026-09-28 修（用户："又结尾了两次"）：晚间简报与睡前总结**内容重叠** ✗
                    #   evening=22:30 · 睡前=推算入睡-15分(实测 22:15) → 相隔 15 分钟说两遍 ✓
                    #   共用一个"今天已收尾"标记：两小时内只收一次 ✓（隔得久算两件事 ✓ 照发 ✓）
                    if _close_recently(last, now):
                        last["bed"] = day
                        save_state(last)
                        print("[sched] 刚收过尾（2 小时内）→ 跳过睡前总结，避免一天收两次", flush=True)
                    txt = bedtime_brief()
                    say(txt, "info", kind="brief_bedtime", key=f"bed:{day}", now=now, force=True)
                    last["bed"] = day
                    last["close_at"] = now.isoformat(timespec="seconds")   # ★ 记"今天已收尾"
                    save_state(last)
            except Exception as e:
                print(f"[bed] {e}", flush=True)

            # ---- 定点提醒（POST /remind 设的；到点就发，force 绕开免打扰）----
            try:
                with db() as c:
                    due = c.execute("SELECT * FROM scheduled WHERE fired_at IS NULL AND at_iso <= ? "
                                    "ORDER BY at_iso ASC LIMIT 5", (now.isoformat(),)).fetchall()
                for r in due:
                    say(r["text"], "warn", kind="scheduled", key=f"sched:{r['id']}", now=now, force=True)
                    with db() as c:
                        if r["daily"]:
                            nx = datetime.fromisoformat(r["at_iso"]) + timedelta(days=1)
                            c.execute("UPDATE scheduled SET at_iso=? WHERE id=?", (nx.isoformat(), r["id"]))
                        else:
                            c.execute("UPDATE scheduled SET fired_at=? WHERE id=?", (now_iso(), r["id"]))
            except Exception as e:
                print(f"[sched] {e}", flush=True)

            # ---- 健康异常：直接推（force，绕开免打扰与限额；同一天同一项只推一次）----
            try:
                with db() as c:
                    spot = [i["text"] for i in analyze(day) if i["level"] == "urgent"]
                for text in spot[:3]:
                    say(text, "urgent", kind="alert", key=f"health:{day}:{text[:18]}", now=now, force=True)
            except Exception as e:
                print(f"[health] {e}", flush=True)

            # ---- 主动关心（全部走去重键，同一件事当天只说一次）----
            # a) 久坐 —— ★ 指数退避重提醒（告警系统的标准做法：50 → 100 → 200 → 400 分钟）
            #    原来按 value//30 做去重键，而"连续活跃"是**只增不减**的累计值：
            #    坐 11 小时 = 660 分钟 → 跨 22 个档 → 一天刷 22 条（实测历史里 23 条同一件事）。
            #    现在同一场久坐最多提 4 次，且间隔逐次加倍；他一起身（值回落）自然就不提了。
            try:
                base = int(CFG["rules"]["sit_continuous_minutes"])
                with db() as c:
                    sit = _latest_metric(c, "pc.continuous_active_minutes", days=1)
                    nags = c.execute("SELECT COUNT(*) n FROM fired WHERE key LIKE ?",
                                     (f"sit:{day}:%",)).fetchone()["n"]
                if sit and sit["value"] and sit["value"] >= base and nags < 4:
                    need = base * (2 ** nags)            # 50 / 100 / 200 / 400
                    if sit["value"] >= need:
                        say(f"连续坐了 {int(sit['value'])} 分钟了，起来活动 5 分钟。", "warn",
                            key=f"sit:{day}:{nags + 1}")
            except Exception as e:
                print(f"[care:sit] {e}", flush=True)

            # b) 深夜还亮着（22:00–次日 02:00 有活动就劝睡，当晚只劝一次）
            try:
                if now.hour >= 22 or now.hour < 2:
                    with db() as c:
                        active, n = _sum_metric(c, now.strftime("%Y-%m-%d"), "screen.active_minutes")
                    if n:
                        say("这会儿还在用电脑，早点收，明天还要早起。", "warn", key=f"late:{day}")
            except Exception as e:
                print(f"[care:late] {e}", flush=True)

            # c) 喝水（09:00–21:00，每 water_every_hours 小时一次）
            try:
                if 9 <= now.hour <= 21 and now.minute < 1:
                    slot = now.hour // int(CFG["care"]["water_every_hours"])
                    if now.hour % int(CFG["care"]["water_every_hours"]) == 0:
                        say("喝口水吧，顺便看看远处。", "info", key=f"water:{day}:{slot}")
            except Exception as e:
                print(f"[care:water] {e}", flush=True)

            # d) 天气（早报时段顺带一条；只在下雨/可能下雨时说）
            try:
                if hm == CFG["schedule"]["morning"]:
                    w = weather_line()
                    if w and ("雨" in w):
                        say(w, "info", key=f"weather:{day}")
            except Exception as e:
                print(f"[care:weather] {e}", flush=True)

            # e) 每日随机关心（默认 1 句，从人设的话题池里抽，不重复）
            try:
                topics = CFG["persona"].get("care_topics") or []
                want = int(CFG["care"].get("random_care_per_day", 0))
                if topics and want > 0 and 10 <= now.hour <= 20:
                    with db() as c:
                        used = {r["text"] or "" for r in c.execute(
                            "SELECT text FROM reminders WHERE day=? AND kind='care'", (day,)).fetchall()}
                    pool = [t for t in topics if t not in used]
                    if pool:
                        import random as _r
                        say(_r.choice(pool), "info", key=f"care:{day}",
                            now=now)
            except Exception as e:
                print(f"[care:random] {e}", flush=True)

            # 上课前提醒（`class_remind_minutes` = 提前多少分钟提醒；**0 = 关掉**）
            # ⚠️ 用户原话：「还在提醒我 30 分钟后上课」「一句一句挤在一起，一秒连放好几条啥也看不到」
            #    → ① 默认关掉 ② 就算开着，也要把进入窗口的**所有课合并成一条**，
            #      绝不再出现"同一分钟连弹两条"。
            try:
                lead = int(CFG["rules"].get("class_remind_minutes") or 0)
                if lead > 0:
                    soon = []
                    for c_ in courses_on(datetime.now(TZ).date()):
                        try:
                            hh_, mm_ = (int(x) for x in c_["start"].split(":"))
                        except Exception:
                            continue
                        st_ = datetime.now(TZ).replace(hour=hh_, minute=mm_, second=0, microsecond=0)
                        mins_ = int((st_ - datetime.now(TZ)).total_seconds() // 60)
                        if 0 < mins_ <= lead:
                            soon.append((mins_, c_))
                    if soon:
                        soon.sort(key=lambda x: x[0])
                        key = "classlead:%s:%s" % (
                            datetime.now(TZ).date().isoformat(),
                            ",".join("%s@%s" % (c_["name"], c_["start"]) for _, c_ in soon))
                        with db() as c:
                            hit = c.execute("SELECT key FROM fired WHERE key=?", (key,)).fetchone()
                            if not hit:
                                c.execute("INSERT INTO fired(key, at) VALUES (?,?)", (key, now_iso()))
                                parts = ["%d 分钟后 %s" % (m_, c_["name"])
                                         + (f"（{c_['room']}）" if c_["room"] else "") for m_, c_ in soon]
                                c.execute("INSERT INTO reminders(day, kind, level, text, created_at, status) "
                                          "VALUES (?,?,?,?,?,'new')",
                                          (datetime.now(TZ).date().isoformat(), "alert", "warn",
                                           "（提醒你）" + "；".join(parts) + "，准备出发。", now_iso()))
                                print(f"[class] 上课提醒（合并 {len(soon)} 节）", flush=True)
            except Exception as e:
                print(f"[class] {e}", flush=True)

            # ★ P0 运维三件：每日备份（04:00）/ 日志轮转（每小时看一眼）/ 自检（08:00，异常就推）
            try:
                if hm == "04:00" and last.get("backup") != day:
                    last["backup"] = day
                    save_state(last)
                    print(f"[backup] {make_backup() or '失败'}", flush=True)
                rotate_log()
                if hm == "08:00" and last.get("selfcheck") != day:
                    last["selfcheck"] = day
                    save_state(last)
                    sc = selfcheck()
                    if not sc["ok"]:
                        say("（自己检查了一遍）我这边的状态不太对：" + "；".join(sc["issues"]) +
                            "。主人有空看一眼。", "warn", kind="selfcheck", key="selfcheck:" + day)
                        print(f"[selfcheck] 异常：{sc['issues']}", flush=True)
                    else:
                        print(f"[selfcheck] 正常（最近发言 {sc.get('last_said_hours')} 小时前）", flush=True)
            except Exception as e:
                print(f"[ops] {str(e)[:90]}", flush=True)

            # ★ P1 复盘：周报（周日 20:30）/ 月报（每月 1 号 09:00）
            try:
                _sc = CFG.get("schedule") or {}
                if now.weekday() == 6 and hm == (_sc.get("review_week") or "20:30") \
                        and last.get("review_w") != day:
                    last["review_w"] = day
                    save_state(last)
                    review("week", speak=True)
                    print("[review] 周报已生成", flush=True)
                elif now.day == 1 and hm == (_sc.get("review_month") or "09:00") \
                        and last.get("review_m") != day:
                    last["review_m"] = day
                    save_state(last)
                    review("month", speak=True)
                    print("[review] 月报已生成", flush=True)
            except Exception as e:
                print(f"[sched:review] {str(e)[:90]}", flush=True)

            for key, kind in (("morning", "brief_morning"), ("evening", "brief_evening")):
                if hm == CFG["schedule"][key] and last[key] != day:
                    # ★ 白天那条照发；晚间这条若两小时内已收过尾就跳过（避免"结尾两次" ✓）
                    if key == "evening" and _close_recently(last, now):
                        last[key] = day
                        save_state(last)
                        print("[sched] 刚收过尾（2 小时内）→ 跳过晚间简报，避免一天收两次", flush=True)
                        continue
                    last[key] = day
                    if key == "evening":
                        last["close_at"] = now.isoformat(timespec="seconds")
                    save_state(last)
                    compose_brief(kind)
        except Exception as e:
            print(f"[sched] {e}", flush=True)
        time.sleep(5)


