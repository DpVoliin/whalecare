# ----------------------------------------------------------------- HTTP
# ---- 简易防护（pentest 之后补的）----
MAX_BODY = 1024 * 1024        # 单请求最大 1MB（原来不限，3MB 也照收）
RATE_WINDOW = 60              # 秒
RATE_MAX = 240               # 每个来源每分钟 240 次；正常用量远低于此
_hits = {}


def rate_limited(ip):
    """够用就好的简易限流：只防"被刷"，不追求精确。"""
    now = time.time()
    arr = _hits.setdefault(ip, [])
    arr[:] = [t for t in arr if now - t < RATE_WINDOW]
    if len(arr) >= RATE_MAX:
        return True
    arr.append(now)
    return False


def ingest_items(body):
    """★ 唯一的入库闸口：HTTP /ingest、单片机 /api/mcu、外挂扩展**都走这里**。

    这样"去重 / 不落原文 / 单位口径"只有一处实现，新数据源不可能绕过规则。
    返回 (ok, skipped)。
    """
    items = body if isinstance(body, list) else [body]
    ok = 0
    skipped = 0
    # ★ 敏感健康数据要**显式同意**才能入库（PIPL / GDPR Art.9 单独同意）。
    #   闸口放这里 = HTTP /ingest、单片机 /api/mcu、外挂扩展**都绕不过**。
    no_consent = 0
    if not consent_granted("health"):
        _kept = []
        for _it in items:
            _m = str((_it or {}).get("metric") or "")
            if _m.startswith("health.") or _m.startswith("sleep."):
                no_consent += 1
            else:
                _kept.append(_it)
        items = _kept
    with db() as c:
        # ★ P0 事件级幂等：网络重试必然导致重复投递。采集端带 event_id 时按 id 去重
        #   （比"值相同 + 60 秒窗口"更严：两个不同事件值恰好相同时不会互相吃掉）。
        #   没带 event_id 的旧客户端 → 自动退回下面的旧规则，向后兼容。
        def _seen(eid):
            if not eid:
                return False
            try:
                if c.execute("SELECT 1 FROM seen_events WHERE event_id=?", (str(eid),)).fetchone():
                    return True
                c.execute("INSERT OR REPLACE INTO seen_events(event_id, ts) VALUES (?,?)",
                          (str(eid)[:80], now_iso()))
                return False
            except Exception:
                return False
        for it in items:
            if not isinstance(it, dict) or not it.get("device") or not it.get("metric"):
                continue
            if it.get("v") is not None:            # 报文版本：记进 meta，以后改字段能判断对面哪一版
                _m = dict(it.get("meta") or {})
                _m["_v"] = it["v"]
                it["meta"] = _m
            if _seen(it.get("event_id") or it.get("eid")):
                skipped += 1
                continue
            ts = it.get("ts") or now_iso()
            try:
                day = datetime.fromisoformat(ts).astimezone(TZ).strftime("%Y-%m-%d")
            except Exception:
                day = today_str()

            # 去重：设备会重发未确认的批次、也可能同一轮上报两次
            #       → 同一设备/指标/数值在 60 秒内只留一条
            is_dup = False
            try:
                recent = c.execute(
                    "SELECT ts, value, meta FROM metrics WHERE device=? AND metric=? AND day=? "
                    "ORDER BY ts DESC LIMIT 3",
                    (it["device"], it["metric"], day)).fetchall()
                for row in recent:
                    # 事件类指标（订单/快递/签到…）值常常都是 1 → 必须把 meta 一起比，
                    # 否则"同一秒的三种不同事件"会被当成重复丢掉（实测被吃掉两笔订单）
                    same_meta = (json.loads(row["meta"] or "{}") == (it.get("meta") or {}))
                    same_val = (row["value"] == it.get("value")) and same_meta
                    try:
                        gap = abs((datetime.fromisoformat(ts)
                                   - datetime.fromisoformat(row["ts"])).total_seconds())
                    except Exception:
                        gap = 999
                    if same_val and gap < 60:
                        is_dup = True
                        break
            except Exception:
                is_dup = False
            if is_dup:
                skipped += 1
                continue

            # ② 默认**不落原文**：通知原文这类内容不进库（要排障时把 store_raw_text 打开）
            _meta = dict(it.get("meta") or {})
            if not CFG.get("privacy", {}).get("store_raw_text", False):
                _meta.pop("raw", None)
                _meta.pop("text", None)
            try:
                c.execute(
                    "INSERT OR REPLACE INTO metrics(ts, day, device, metric, value, unit, source, confidence, meta) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (ts, day, it["device"], it["metric"], it.get("value"), it.get("unit", ""),
                     it.get("source", ""), float(it.get("confidence", 1.0)),
                     json.dumps(_meta, ensure_ascii=False)))
                ok += 1
            except Exception as e:
                print(f"[ingest] 跳过：{e}", flush=True)
    if no_consent:
        skipped += no_consent
        try:
            audit("ingest_no_consent", target="health", actor="ingest",
                  result="skipped", note="无健康数据同意，丢弃 %d 条" % no_consent)
        except Exception:
            pass
    return ok, skipped


class Handler(BaseHTTPRequestHandler):
    server_version = f"hub/{VERSION}"

    # ★ 凭据绝不能进日志（2026-09-29 安全审查 C4）：
    #   /api/mcu 允许在 query 里带 token（给最小设备用）、配对链接里带一次性码，
    #   而基类默认会把**整行请求**（含 query）打进日志 → token/码就这样落盘。
    #   只对**凭据参数**打码，其余照旧（可观测性不受影响）。
    _SECRET_Q = re.compile(r"(?i)\b(token|code|t|pin|key|secret)=([^&\s]*)")

    def log_message(self, fmt, *args):
        try:
            msg = fmt % args if args else str(fmt)
        except Exception:
            msg = str(fmt)
        masked = self._SECRET_Q.sub(r"\1=***", msg)
        print(f"[http] {self.address_string()} {masked}", flush=True)

    # ---- 工具
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        # ★ 三种 body 分开处理，别一律 json.dumps：
        #   · bytes → 原样发
        #   · str   → 调用方**已经备好原文**（HTML / 纯文本），直接 utf-8 发
        #   · 其它（dict / list）→ 才是 JSON
        #   踩过的坑：以前对 str 也 json.dumps，于是 HTML 变成
        #   "\"<!doctype html>…\n<meta …>\"" —— 前导多一个引号、真换行变**字面量 \n**、
        #   CSS 里的 "Segoe UI" 被转义成 \" → 页面"能打开但全是坏的"（/dash 从写出来就这样，
        #   管理台也中招；只有在浏览器里真看一眼才发现）。
        if isinstance(body, (bytes, bytearray, memoryview)):
            data = bytes(body)
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _client(self):
        return self.client_address[0]

    def _cookie_sess(self):
        """管理页的会话 cookie（值 = HMAC(token)）。**只给浏览器用**；API 仍然只认 header。"""
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "whale_admin":
                return v.strip()
        return ""

    def _switch(self):
        """当前请求的接口路径（审计用；不含 query，免得把参数写进日志）。"""
        return (self.path or "").split("?")[0][:80]

    def _auth(self, q):
        # 只认 header 里的 X-Token：?token= 会进服务器日志、也会留在浏览器历史/代理记录里
        tok = self.headers.get("X-Token") or ""
        if not tok and (q.get("token") or [""])[0]:
            print(f"[warn] {self.client_address[0]} 试图用 query 里的 token（已拒绝）", flush=True)
            audit("auth_fail", target=self._switch(), actor=self._client(),
                  result="denied", note="试图用 query 传 token")
        ok = (tok == CFG["token"]) or (bool(self._cookie_sess()) and self._cookie_sess() == admin_session())
        if not ok:
            # ★ 审计：鉴权失败只记「谁 + 打哪个接口 + 结果」，不记他发了什么内容
            audit("auth_fail", target=self._switch(), actor=self._client(),
                  result="denied", note="token 不匹配" if tok else "没带 token")
            self._send(401, {"error": "token 不对"})   # ⚠️ 别把服务端路径写进报错（安全自测抓到：路径泄露）
            return False
        return True

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {}

    def _touch(self, name):
        if not name:
            return
        with db() as c:
            c.execute("INSERT INTO terminals(name, last_seen) VALUES (?,?) "
                      "ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen", (name, now_iso()))

    # ---- 路由
    def _guard(self):
        """统一防线：限流在最前面 —— 不然 /health 这类免鉴权接口会被拿来刷。"""
        if rate_limited(self.client_address[0]):
            self._send(429, {"error": "太频繁了，缓一下"})
            return False
        return True


    def _export(self, q):
        """GDPR Art.20 数据可携带：机器可读的全量导出（SQLite 本身就是标准格式，这里是 JSON 版）。

        ?redact=1 → 顺手脱敏（去掉通知原文这类内容），方便你把数据分享/交给别人分析。
        """
        redact = (q.get("redact") or ["0"])[0] == "1"
        out = {"version": VERSION, "exported_at": now_iso(), "redacted": redact, "tables": {}}
        with db() as c:
            names = [r["name"] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "AND name NOT LIKE '%_fts_%' AND name NOT LIKE '%_fts' ORDER BY name")]   # 派生索引不导出
            def _jsonable(v):
                """SQLite 行里可能有 bytes（BLOB）→ 转成可 JSON 序列化的形式。"""
                if isinstance(v, (bytes, bytearray, memoryview)):
                    import base64 as _b64  # 局部导入：不依赖文件顶部的导入顺序
                    return _b64.b64encode(bytes(v)).decode("ascii")
                return v

            for t in names:
                rows = [{k: _jsonable(v) for k, v in dict(r).items()}
                        for r in c.execute("SELECT * FROM %s" % t)]
                if redact:
                    for row in rows:
                        for k in ("meta", "text", "content", "raw", "note"):
                            if k not in row or not row[k]:
                                continue
                            if k == "meta" and isinstance(row[k], str) and row[k].startswith("{"):
                                try:
                                    m = json.loads(row[k])
                                    for drop in ("raw", "text", "title", "store", "tracking", "window",
                                                 "process", "app", "artist", "playlist"):
                                        m.pop(drop, None)
                                    row[k] = json.dumps(m, ensure_ascii=False)
                                except Exception:
                                    row[k] = "{}"
                            elif k != "meta":
                                row[k] = None
                out["tables"][t] = rows
        return self._send(200, out)

    def _erase(self, body):
        """GDPR Art.17 删除权：真的把数据删掉（不是标记）。

        必须显式带 {"confirm": "ERASE-ALL"} —— 防止误触。
        删之前自动做一次备份（如果备份函数可用），删完 VACUUM 回收空间。
        """
        if (body or {}).get("confirm") != "ERASE-ALL":
            return self._send(400, {"ok": False, "error": "要删除必须带 confirm=ERASE-ALL",
                                    "note": "scope 可选 all/metrics/episodes/chats/reminders，默认 all"})
        scope = str((body or {}).get("scope") or "all")
        tables = {
            "all": ["metrics", "reminders", "chats", "episodes", "decisions", "feedback", "fired", "scheduled"],
            "metrics": ["metrics"],
            "episodes": ["episodes"],
            "chats": ["chats"],
            "reminders": ["reminders", "scheduled", "fired"],
        }.get(scope)
        if not tables:
            return self._send(400, {"ok": False, "error": "scope 不认识：%s" % scope})
        backup = "未做"
        try:
            if callable(globals().get("make_backup")):
                backup = "已备份到 hub/backup/"
                make_backup()                      # 万一删错还能捞回来
        except Exception as e:
            backup = "备份失败：%s" % type(e).__name__
        deleted = {}
        with db() as c:
            for t in tables:
                try:
                    deleted[t] = c.execute("DELETE FROM %s" % t).rowcount
                except Exception:
                    pass
            try:
                c.execute("VACUUM")
            except Exception:
                pass
        print("[erase] scope=%s 删除 %s（备份：%s）" % (scope, deleted, backup), flush=True)
        return self._send(200, {"ok": True, "scope": scope, "deleted_rows": deleted,
                                "backup": backup,
                                "note": "已物理删除并 VACUUM。原始数据只在你自己的服务器上，删掉即彻底消失。"})


    def do_GET(self):
        if not self._guard():
            return
        # 单片机极简口：/api/mcu?d=dev&m=temp&v=25.3&u=C&t=TOKEN
        #   多个指标可以逗号并列：m=temp,hum&v=25.3,60（省一次往返）
        #   回一行纯文本 ok / err:xxx —— 单片机不用解析 JSON
        if self.path.startswith("/api/mcu"):
            return self._mcu(urlparse(self.path).query)
        # 屏幕/音箱类设备的下发口（同样允许 query token：单片机上带自定义 header 很麻烦）
        if self.path.startswith("/mcu/inbox"):
            return self._mcu_inbox(parse_qs(urlparse(self.path).query))
        if self.path.startswith("/mcu/ack"):
            return self._mcu_ack(parse_qs(urlparse(self.path).query))
        if self.path.startswith("/api/pair"):
            return self._pair(urlparse(self.path).query)     # 设备友好：换回来是一行纯文本

        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        # ★ 下面三条**在鉴权之前**：登录页本身不能要求已登录
        if path == "/login":
            return self._login(q)
        if path == "/logout":
            return self._logout()
        if path in ("/", "/index.html", "/admin"):
            return self._admin_page(q)
        if path == "/health":
            return self._send(200, self._health())
        if not self._auth(q):
            return
        if path == "/audit":
            lim = min(500, int((q.get("limit") or ["100"])[0] or 100))
            return self._send(200, {"stats": audit_stats(),
                                    "items": audit_recent(lim, (q.get("action") or [""])[0] or None)})
        if path == "/ext":
            return self._send(200, {
                "dir": EXT_DIR,
                "loaded": EXT["loaded"],
                "errors": EXT["errors"],
                "sources": [{"file": s["file"], "name": s["name"], "device": s["device"],
                             "interval_minutes": s["interval"], "last_n": s["last_n"],
                             "last_err": s["last_err"]} for s in EXT["sources"]],
                "hooks": sorted(EXT["hooks"].keys()),
                "note": "外挂扩展：加一个文件就多一个数据源，中枢核心不需要改；扩展报错不影响主流程",
            })
        if path == "/export":
            audit("export", target="redact=%s" % (1 if (q.get("redact") or ["0"])[0] == "1" else 0),
                  actor=self._client(), note="全量导出（GDPR Art.20）")
            return self._export(q)
        if path == "/today":
            return self._today(q)
        if path == "/feedback":
            lim = min(50, int((q.get("limit") or ["20"])[0] or 20))
            consume = (q.get("consume") or ["0"])[0] == "1"
            with db() as c:
                rows = c.execute("SELECT id, ts, verdict, band, note, w, src FROM feedback "
                                 "WHERE consumed=0 ORDER BY id ASC LIMIT ?", (lim,)).fetchall()
                if consume and rows:
                    c.execute("UPDATE feedback SET consumed=1 WHERE id IN (%s)"
                              % ",".join("?" * len(rows)), [r["id"] for r in rows])
            return self._send(200, {"count": len(rows), "items": [dict(r) for r in rows]})
        if path in ("/dash", "/看数据"):
            return self._send(200, dash_html(), "text/html; charset=utf-8")
        if path == "/decisions":
            lim = min(500, int((q.get("limit") or ["100"])[0] or 100))
            with db() as c:
                rows = c.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (lim,)).fetchall()
            return self._send(200, {"count": len(rows), "items": [dict(r) for r in rows]})
        if path.startswith("/asset/"):
            # ★ 2026-09-29 加：把"她的形象素材"从中枢发给 app / 挂件 ✓
            #   为什么走中枢而不是放进仓库：素材是主人的私有资源 ✗
            #   公开仓库只留**占位图** ✓ 真图存在中枢本地目录 ✓ 换图不用重发版 ✓
            #   鉴权：**只认 header X-Token** ✓（和全局一致 ✓
            #   为什么不做 ?t= 兜底：那种写法会把 token 写进服务器日志/浏览器历史 ✓ 全局已明确禁止 ✓
            #   → app 取素材必须走代码（带 header ✓）不能直接塞进 <img src> ✓）
            #   安全：**白名单名字** + 只取 basename ✓ 杜绝 ../ 穿越 ✓
            _name = os.path.basename(path[len("/asset/"):])
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,48}\.(png|jpg|jpeg|webp|ico|crt|wav|ogg|ttf|otf)", _name, re.I):
                return self._send(400, {"ok": False, "error": "文件名不合规"})
            # 第二道防线（入口本已全局鉴权 ✓ 这里再确认一次 ✓ 万一将来白名单松动也不至于裸奔 ✓）
            if not self._auth(parse_qs(urlparse(self.path).query)):
                return
            _dir = pathlib.Path(os.getenv("WHALE_ASSETS") or (pathlib.Path(os.getenv("WHALE_HOME") or ".") / "assets"))
            _f = _dir / _name
            if not _f.is_file():
                return self._send(404, {"ok": False, "error": "not found", "dir": str(_dir)})
            _b = _f.read_bytes()
            _ct = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
                   "ico": "image/x-icon", "crt": "application/x-x509-ca-cert", "wav": "audio/wav",
                   "ogg": "audio/ogg", "ttf": "font/ttf", "otf": "font/otf"}.get(_name.rsplit(".", 1)[-1].lower(), "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", _ct)
            self.send_header("Content-Length", str(len(_b)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(_b)
            return
        if path == "/bands":
            # ★ 这里原来是**只挂在 do_POST** 的：说话层用 GET 调（它无 body 时就走 GET），
            #   于是永远 404 → 分桶后验静默失效、一直退回全局后验（今天才查出来）。
            #   读类接口就该 GET；POST 那份保留，向后兼容已有调用方。
            return self._send(200, band_stats())
        if path == "/memory":
            qq = (q.get("q") or [""])[0]
            return self._send(200, {"q": qq, "items": episode_search(qq) if qq else episodes_recent()})
        if path == "/question":
            qn = question_now()
            return self._send(200, {"has_question": bool(qn), "question": qn or {},
                                    "note": "一天最多一个；有数据支撑才问；问过记情节不重复"})
        if path == "/review":
            k = (q.get("kind") or ["week"])[0]
            if k not in ("week", "month"):
                return self._send(400, {"ok": False, "error": "kind 只能是 week / month"})
            return self._send(200, review(k))
        if path == "/personas":
            return self._send(200, {"active": active_pack(), "dir": PERSONA_DIR,
                                    "packs": persona_packs(),
                                    "note": "换人设＝换目录：personas/<id>/{persona.json,card.json}"})
        if path == "/persona/card":
            pk = (q.get("pack") or [""])[0]
            return self._send(200, load_persona_card(pk or None) or {"error": "没有角色卡"})
        if path == "/persona":
            pk = (q.get("pack") or [""])[0]
            if pk:
                p = persona_pack_path(pk, "persona.json")
                if not p:
                    return self._send(404, {"ok": False, "error": f"没有人设包 {pk}"})
                return self._send(200, {"pack": pk, "persona": json.load(open(p, encoding="utf-8"))})
            return self._send(200, CFG["persona"])
        if path == "/pending":
            return self._pending(q)
        if path == "/devices":
            return self._send(200, self._devices())
        if path == "/channels":
            return self._send(200, channels_status())
        if path == "/analysis":
            # 分析出口（**数据出口**）：拉最近 N 份 AI 分析结果 —— 没配 webhook/文件也能用
            return self._send(200, analysis_view(q))
        if path == "/analysis/schema":
            # 数据格式规范**本身**（机器可读）—— 消费者不必去翻仓库，直接拿这份
            return self._send(200, json.loads(ANALYSIS_SCHEMA_JSON))
        if path == "/remind":
            with db() as c:
                rows = c.execute("SELECT * FROM scheduled WHERE fired_at IS NULL "
                                 "ORDER BY at_iso ASC").fetchall()
            return self._send(200, {"items": [dict(r) for r in rows]})
        if path == "/llm-preview":
            ctx, dropped = llm_context()
            return self._send(200, {
                "privacy_enabled": bool(CFG["privacy"].get("enabled", True)),
                "would_send_to_model": ctx,
                "what_model_never_sees": dropped,
            })
        if path == "/timetable":
            tt = _timetable()
            return self._send(200, tt or {"error": "还没收到课表（在岛课表里导出备份后 POST /timetable）"})
        if path == "/timetable/today":
            tt = _timetable()
            today = datetime.now(TZ).date()
            return self._send(200, {"date": today.strftime("%Y-%m-%d"),
                                    "week": _week_of(tt, today) if tt else None,
                                    "courses": courses_on(today, tt)})
        if path == "/timetable/next":
            return self._send(200, {"next": next_course_from()})
        if path == "/metrics":
            return self._metrics(q)
        return self._send(404, {"error": "没有这个接口"})

    def do_POST(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        # 体积上限：先掐掉超大请求（防"一个 100MB 的包把内存吃光"）
        try:
            _n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            _n = 0
        if _n > MAX_BODY:
            return self._send(413, {"error": f"请求太大，上限 {MAX_BODY // 1024}KB"})
        # 登录/管理台要在鉴权之前（登录本身就是"还没登录"时做的）
        if path == "/login":
            return self._login_post()
        if path == "/admin":
            return self._admin_post(q)
        if not self._auth(q):
            return
        # ★ 它是**状态写**端点（翻转特殊类别数据的同意），必须在鉴权之后（安全审查 C3）
        if path == "/consent":
            # POST {"what":"health","granted":true} → 记录**显式同意**（采集器打开健康开关时调）
            # ★ 两个坑都踩过（记下来）：
            #   ① 第一版放在 do_GET 里 → POST 404（写操作必须在 do_POST）
            #   ② 用了不存在的 self._read_body() → 永远拿到空 body，误报"只支持 what=health"
            #   正确姿势是本文件其它路由一致的：self._body()（已解析好的 dict/list）
            _b = self._body() or {}
            if not isinstance(_b, dict):
                _b = {}
            _what = str((_b or {}).get("what") or "").strip().lower()
            if _what != "health":
                return self._send(400, {"ok": False, "error": "只支持 what=health"})
            consent_set(_what, bool((_b or {}).get("granted")),
                        source=(_b or {}).get("device") or self._client(),
                        version=(_b or {}).get("version") or "")
            return self._send(200, {"ok": True, "what": _what, "granted": consent_granted(_what)})
        if path == "/bands":
            # 分桶接受率（说话层用它做期望效用 gate；样本不足的桶会被标 reliable=false）
            return self._send(200, band_stats())
        if path == "/decision":
            # 说话层把"为什么这么决定"上报进来（回放器靠它；此前后端没开这个路由，日志一直是 0 条）
            b = self._body() or {}
            try:
                decision_log(str(b.get("kind") or "speak"), float(b.get("gap_sec") or 0),
                             str(b.get("reason") or "")[:200], int(b.get("material") or 0),
                             int(b.get("said") or 0), str(b.get("band") or ""),
                             b.get("ctx"))
                return self._send(200, {"ok": True})
            except Exception as e:
                return self._send(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:80])})
        if path == "/erase":
            b = self._body()
            b = b if isinstance(b, dict) else {}
            _ok = (b.get("confirm") == "ERASE-ALL")
            audit("erase", target=str(b.get("scope") or "all"), actor=self._client(),
                  result="ok" if _ok else "denied",
                  note="已执行物理删除" if _ok else "缺 confirm，已拒绝")
            return self._erase(b)
        if path == "/ingest":
            return self._ingest(self._body())
        if path == "/timetable":
            body = self._body()
            raw = body.get("timetable") if isinstance(body, dict) and "timetable" in body else body
            if not isinstance(raw, dict) or "courses" not in raw:
                return self._send(400, {"error": "要传岛课表导出的那份 JSON（含 courses / periods）"})
            with db() as c:
                c.execute("INSERT INTO timetable(id, raw, source, updated_at) VALUES (1,?,?,?) "
                          "ON CONFLICT(id) DO UPDATE SET raw=excluded.raw, source=excluded.source, "
                          "updated_at=excluded.updated_at",
                          (json.dumps(raw, ensure_ascii=False), raw.get("source", ""), now_iso()))
            return self._send(200, {"ok": True, "courses": len(raw.get("courses", [])),
                                    "term_start": raw.get("termStartDate", "")})
        if path == "/feedback":
            b = self._body() or {}
            v = str(b.get("verdict") or "").strip().lower()
            if v not in ("good", "bad"):
                return self._send(400, {"ok": False, "error": "verdict 只能是 good / bad"})
            # ★ 客户端的口子只传 verdict（挂件/App 都不知道"当前场景桶"是什么）；
            #   桶由**中枢按上报时刻自己算** → 分桶 Thompson 才真能攒到样本。
            band = str(b.get("band") or "").strip()[:24] or band_now()
            # ★ 证据强度：手动点 = 1.0；隐式推断（回话/没回话）默认 0.5
            try:
                w = float(b.get("w", 1.0))
            except (TypeError, ValueError):
                w = 1.0
            w = max(0.05, min(1.0, w))
            src = str(b.get("src") or "manual").strip()[:16] or "manual"
            with db() as c:
                c.execute("INSERT INTO feedback(ts, verdict, band, note, w, src) VALUES (?,?,?,?,?,?)",
                          (now_iso(), v, band, str(b.get("note") or "")[:200], w, src))
            print(f"[feedback] {v} w={w} src={src} band={band}", flush=True)
            return self._send(200, {"ok": True, "verdict": v, "band": band, "w": w, "src": src})
        if path == "/persona":
            body = self._body()
            if isinstance(body, dict) and body:
                CFG["persona"].update(body)
                with open(CFG_PATH, "w", encoding="utf-8") as f:
                    json.dump(CFG, f, ensure_ascii=False, indent=2)
                audit("config_change", target="persona:" + ",".join(sorted(body)[:8]),
                      actor=self._client(), note="改了人设字段 %d 个" % len(body))
                return self._send(200, {"ok": True, "persona": CFG["persona"]})
            return self._send(400, {"error": "body 要是一个对象"})
        if path == "/brief":
            kind = (q.get("kind") or ["brief_evening"])[0]
            rid, text = compose_brief(kind)
            return self._send(200, {"ok": True, "id": rid, "kind": kind, "text": text})
        if path == "/ack":
            body = self._body()
            body = body if isinstance(body, dict) else {}
            ids = body.get("ids") if body.get("ids") is not None else (
                [body["id"]] if body.get("id") is not None else [])
            ids = [int(i) for i in (ids or [])]
            term = (q.get("for") or [body.get("for") or "weixin"])[0]
            with db() as c:
                c.executemany("UPDATE reminders SET status='delivered', delivered_to=? WHERE id=?",
                              [(term, i) for i in ids])
            return self._send(200, {"ok": True, "acked": len(ids), "for": term})
        if path == "/channels":
            body = self._body()
            ch = CFG.setdefault("channels", {})
            for k in CHANNEL_KEYS:
                if k in body:
                    ch[k] = str(body[k]).strip()
            with open(CFG_PATH, "w", encoding="utf-8") as f:
                json.dump(CFG, f, ensure_ascii=False, indent=2)
            audit("config_change", target="channels:" + ",".join(sorted(body)[:8]),
                  actor=self._client(), note="改了出口字段 %d 个" % len(body))
            return self._send(200, {"ok": True, "channels": {k: ("已设置" if v else "空") for k, v in ch.items()}})
        if path == "/push":
            # 直发一条到已配置出口（企业微信群机器人 / 通用 webhook），不经 Hermes 网关
            b = self._body() or {}
            text = str(b.get("text") or "").strip()
            if not text:
                return self._send(400, {"ok": False, "error": "要传 {text}"})
            return self._send(200, {"ok": True, "result": channel_send(text),
                                    "channels": channels_status()})
        if path == "/analysis":
            # 分析出口的**写入端**：说话层（本机，持有模型 key）把 AI 分析结果推进来
            # → 先按 docs/analysis.schema.json **硬校验**（不合规拒收）→ 落库 + 分发到数据出口
            #   ?validate=1 只干跑不落库（写端先自查）
            _vo = str((q.get("validate") or ["0"])[0]).lower() in ("1", "true", "yes")
            return self._send(200, analysis_ingest(self._body(), self._client(), validate_only=_vo))
        if path == "/push/test":
            return self._send(200, {"ok": True, "result": channel_test(), "channels": channels_status()})
        if path == "/push/register":
            body = self._body()
            name, url = (body.get("name") or "").strip(), (body.get("url") or "").strip()
            if not name or not url.startswith("http"):
                return self._send(400, {"error": "要 {name, url}"})
            with db() as c:
                c.execute("INSERT INTO terminals(name, last_seen, note) VALUES (?,?,?) "
                          "ON CONFLICT(name) DO UPDATE SET note=excluded.note, last_seen=excluded.last_seen",
                          (name, now_iso(), url))
            return self._send(200, {"ok": True, "note": "有主动提醒就 POST 到这个地址，请求头带 X-Token"})
        if path == "/ack":
            body = self._body()
            with db() as c:
                c.execute("UPDATE reminders SET status=? WHERE id=?", (body.get("status", "done"), body.get("id")))
            return self._send(200, {"ok": True})
        if path == "/remind":
            body = self._body()
            body = body if isinstance(body, dict) else {}
            text = str(body.get("text") or "").strip()
            at = str(body.get("at") or "").strip()
            daily = 1 if body.get("daily") else 0
            if not text or not at:
                return self._send(400, {"error": "需要 text 和 at（at 可为 HH:MM 或 ISO 时间）"})
            now = datetime.now(TZ)
            try:
                if len(at) <= 5:                       # HH:MM → 今天该时刻；已过则顺延明天
                    hh, mm = [int(x) for x in at.split(":")]
                    t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    if t <= now:
                        t = t + timedelta(days=1)
                else:
                    t = datetime.fromisoformat(at)
            except Exception as e:
                return self._send(400, {"error": f"时间格式不对：{at}（{e}）"})
            with db() as c:
                cur = c.execute("INSERT INTO scheduled(at_iso, text, daily, created_at) VALUES (?,?,?,?)",
                                (t.isoformat(), text, daily, now_iso()))
                rid = cur.lastrowid
            audit("config_change", target="scheduled#%s" % rid, actor=self._client(),
                  note="定点 %s%s" % (at, "（每天）" if daily else ""))
            return self._send(200, {"ok": True, "id": rid, "at": t.isoformat(),
                                    "daily": bool(daily), "text": text})
        if path == "/chat":
            return self._chat(self._body())
        return self._send(404, {"error": "没有这个接口"})

    def _send_text(self, code, text, enc="utf8"):
        """纯文本响应。enc=gb2312 给 SYN6288 / XFS5152 这类中文 TTS 模块直接可用。"""
        cs = "gb2312" if str(enc).lower() in ("gb2312", "gbk") else "utf-8"
        try:
            data = (text + "\n").encode(cs)
        except Exception:
            cs, data = "utf-8", (text + "\n").encode("utf-8")   # 生僻字/emoji 编不进 gb2312 时兜底
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=" + cs)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _mcu(self, query):
        """单片机上报口。参数：d 设备 / m 指标(可逗号) / v 值(可逗号) / u 单位 / t token。"""
        q = parse_qs(query)
        g = lambda k, i=0: ((q.get(k) or [""])[0] if i == 0 else (q.get(k) or [""])[i])
        tok = g("t") or self.headers.get("X-Token") or ""
        mcu_tok = (CFG.get("mcu") or {}).get("token") or CFG["token"]
        if tok not in (CFG["token"], mcu_tok):
            print(f"[mcu] {self.client_address[0]} token 不对", flush=True)
            audit("auth_fail", target="/api/mcu", actor=self._client(),
                  result="denied", note="单片机 token 不对")
            return self._send_text(401, "err:token")
        dev = (g("d") or "").strip()
        # ⑤ 可选的校验和与序号（单片机稳一点）：
        #    c = 各字符 ASCII 之和 mod 256（C 里一行 for 就能算，比 CRC16 省事）
        #    s = 递增序号（中继/中枢用它去重，防丢包重发造成的重复）
        csum = (g("c") or "").strip()
        seq = (g("s") or "").strip()
        if csum:
            raw_line = f"{g('d')}{g('m')}{g('v')}"
            calc = sum(raw_line.encode()) % 256
            try:
                if int(csum) != calc:
                    print(f"[mcu] {self.client_address[0]} 校验和不符（给的 {csum}，算的 {calc}）", flush=True)
                    return self._send_text(400, "err:crc")
            except ValueError:
                return self._send_text(400, "err:crc")
        metrics = [x.strip() for x in (g("m") or "").split(",") if x.strip()]
        vals = [x.strip() for x in (g("v") or "").split(",")]
        unit = (g("u") or "").strip()
        if not dev or not metrics or not vals:
            return self._send_text(400, "err:params")
        items = []
        for i, m in enumerate(metrics[:8]):
            raw = vals[i] if i < len(vals) else vals[0]
            try:
                num = float(raw)
            except ValueError:
                num = raw          # 非数值也收（比如状态字符串），存 meta
            _m = {"seq": seq} if seq else {}
            items.append({"device": dev, "metric": m, "value": num if isinstance(num, (int, float)) else None,
                          "unit": unit, "source": "mcu", "meta": _m})
        print(f"[mcu] {dev} ← " + " ".join(f"{m}={v}" for m, v in zip(metrics, vals, strict=False)), flush=True)
        # 直接复用 /ingest 的入库逻辑（它自己会回响应 —— 单片机只看 HTTP 200 就够了）
        return self._ingest(items)

    # ---- 各接口实现
    def _health(self):
        with db() as c:
            m = c.execute("SELECT COUNT(*) n FROM metrics").fetchone()["n"]
            r = c.execute("SELECT COUNT(*) n FROM reminders").fetchone()["n"]
        _r = CFG.get("rules") or {}
        return {"ok": True, "version": VERSION, "now": now_iso(), "metrics": m, "reminders": r,
                "code": code_fingerprint(),
                "rules": {"class_remind_minutes": _r.get("class_remind_minutes"),
                          "sit_continuous_minutes": _r.get("sit_continuous_minutes")},
                "uptime_note": "hub 在跑", "endpoints": ["/consent", "/ack",
                                                         "/analysis",
                                                         "/analysis/schema",
                                                         "/api/mcu",
                                                         "/api/pair",
                                                         "/audit",
                                                         "/bands",
                                                         "/brief",
                                                         "/channels",
                                                         "/chat",
                                                         "/dash",
                                                         "/decision",
                                                         "/decisions",
                                                         "/devices",
                                                         "/erase",
                                                         "/export",
                                                         "/ext",
                                                         "/feedback",
                                                         "/health",
                                                         "/ingest",
                                                         "/llm-preview",
                                                         "/mcu/ack",
                                                         "/mcu/inbox",
                                                         "/memory",
                                                         "/metrics",
                                                         "/pending",
                                                         "/persona",
                                                         "/persona/card",
                                                         "/personas",
                                                         "/push",
                                                         "/push/register",
                                                         "/push/test",
                                                         "/question",
                                                         "/remind",
                                                         "/review",
                                                         "/timetable",
                                                         "/timetable/next",
                                                         "/timetable/today",
                                                         "/today",
                                                         "/看数据"]}

    def _ingest(self, body):
        ok, skipped = ingest_items(body)
        return self._send(200, {"ok": True, "accepted": ok, "skipped": skipped,
                                "total": len(body if isinstance(body, list) else [body])})

    def _today(self, q):
        term = (q.get("terminal") or [""])[0]
        self._touch(term)
        with db() as c:
            rows = c.execute("SELECT * FROM reminders WHERE day=? ORDER BY id DESC LIMIT 20", (today_str(),)).fetchall()
            rem = [dict(r) for r in rows]
            devs = self._devices()
            sleep = _latest_metric(c, "sleep.total_minutes")
            screen, _ = _peak_metric(c, today_str(), "screen.active_minutes")
        return self._send(200, {
            "date": today_str(), "now": now_iso(), "terminal": term,
            "persona": CFG["persona"],
            "greeting": persona_line("info") + ("今天还没什么要注意的。" if not rem else "今天的提醒在下面。"),
            "reminders": rem,
            "latest": {"sleep_minutes": (sleep or {}).get("value"), "screen_minutes_today": screen},
            "digest": daily_digest(),
            "classes_today": courses_on(datetime.now(TZ).date()),
            "next_class": with_day_hint(next_course_from()),
            "calendar_today": calendar_today(),
            "care": care_now(),          # ★ 新增数据源（天气/在听/电量/闹钟/快递/温湿度/游戏）
            "devices": devs,
        })


    # ───────── 屏幕 / 音箱类设备的下发口（STM32、ESP32、树莓派小屏都通用）─────────
    # 设计取舍：单片机解析不了 JSON，也做不了 TLS → 这里只回**一行纯文本**，
    # 由局域网中继（mcu_relay.py）用 HTTPS 代它说话。设备用独立 mcu token，别给主 token。
    def _speakable(self, text: str) -> str:
        """把"给人看的话"变成"能念出来的话"：
        去掉（动作/情绪）标注、去掉 markdown 与 emoji、按句号截断到 ~120 字。
        —— 念出来的东西不该带动作标注，跟人设里"不许假装做物理动作"是同一条规矩。
        """
        import re as _re
        t = _re.sub(r"[（(][^）)]{1,12}[）)]", "", text or "")
        t = _re.sub(r"[*_`#>\[\]]", "", t)
        t = _re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", t)
        t = _re.sub(r"\s+", " ", t).strip()
        if len(t) > 120:
            cut = max(t.rfind("。", 0, 120), t.rfind("！", 0, 120), t.rfind("？", 0, 120))
            t = t[:cut + 1] if cut > 40 else t[:120]
        return t

    def _mcu_auth(self, q) -> bool:
        tok = (q.get("t") or [""])[0] or (self.headers.get("X-Token") or "")
        mcu = (CFG.get("mcu") or {}).get("token") or ""
        return bool(tok) and (self._same(tok, CFG.get("token")) or self._same(tok, mcu))

    def _mcu_inbox(self, q):
        """设备取一条要提醒的内容。回一行：ok|<id>|<文本> / none / err:token

        · peek=1  只看不消费（调试用）
        · enc=gb2312  给 SYN6288 这类中文 TTS 模块直接可用（默认 utf8）
        · 同时把设备心跳记进 terminals（这样 hubctl devices 能看到屏幕设备活着）
        """
        if not self._mcu_auth(q):
            return self._send_text(401, "err:token")
        dev = ((q.get("d") or ["mcu"])[0] or "mcu")[:24]
        peek = (q.get("peek") or ["0"])[0].lower() not in ("0", "", "false", "no")
        enc = (q.get("enc") or ["utf8"])[0].lower()
        self._touch("screen:" + dev)
        with db() as c:
            row = c.execute("SELECT * FROM reminders WHERE status='new' ORDER BY id ASC LIMIT 1").fetchone()
            if row is None:
                return self._send_text(200, "none", enc=enc)
            text = self._speakable(row["text"] or "")
            if not peek:
                c.execute("UPDATE reminders SET status='delivered', delivered_to=? WHERE id=?", (dev, row["id"]))
        return self._send_text(200, "ok|%s|%s" % (row["id"], text), enc=enc)

    def _mcu_ack(self, q):
        """设备念完了回执（可选）：ok|<id> → 标记 spoken，便于统计"真的念了几条"。"""
        if not self._mcu_auth(q):
            return self._send_text(401, "err:token")
        rid = (q.get("id") or [""])[0]
        if not rid.isdigit():
            return self._send_text(400, "err:id")
        with db() as c:
            c.execute("UPDATE reminders SET status='spoken' WHERE id=?", (int(rid),))
        return self._send_text(200, "ok")


    def _pending(self, q):
        term = (q.get("for") or q.get("terminal") or [""])[0]
        self._touch(term)
        try:
            limit = max(1, min(20, int((q.get("limit") or ["20"])[0])))
        except Exception:
            limit = 20
        peek = (q.get("peek") or ["0"])[0].lower() not in ("0", "", "false", "no")
        with db() as c:
            rows = c.execute("SELECT * FROM reminders WHERE status='new' ORDER BY id ASC LIMIT ?",
                             (limit,)).fetchall()
            if rows and term and not peek:
                ids = [r["id"] for r in rows]
                c.executemany("UPDATE reminders SET status='delivered', delivered_to=? WHERE id=?",
                              [(term, i) for i in ids])
        return self._send(200, {"terminal": term, "count": len(rows), "items": [dict(r) for r in rows]})

    def _devices(self):
        with db() as c:
            rows = c.execute(
                "SELECT device, COUNT(*) n, MAX(ts) last, GROUP_CONCAT(DISTINCT metric) metrics "
                "FROM metrics GROUP BY device ORDER BY last DESC").fetchall()
            terms = c.execute("SELECT name, last_seen FROM terminals ORDER BY last_seen DESC").fetchall()
        return {"data_sources": [dict(r) for r in rows], "terminals": [dict(r) for r in terms]}

    def _metrics(self, q):
        where, args = ["1=1"], []
        for key, col in (("device", "device"), ("metric", "metric"), ("day", "day")):
            v = (q.get(key) or [""])[0]
            if v:
                where.append(f"{col}=?")
                args.append(v)
        since = (q.get("since") or [""])[0]
        if since:
            where.append("ts>=?")
            args.append(since)
        limit = min(int((q.get("limit") or ["200"])[0]), 2000)
        with db() as c:
            rows = c.execute(f"SELECT * FROM metrics WHERE {' AND '.join(where)} ORDER BY ts DESC LIMIT ?",
                             (*args, limit)).fetchall()
        return self._send(200, {"count": len(rows), "items": [dict(r) for r in rows]})

    def _chat(self, body):
        """终端发来的对话。v0.1 走确定性回答；接上 LLM 后在此换成模型（人设从 /persona 取）。"""
        text = (body.get("text") or "").strip()
        term = body.get("terminal") or ""
        self._touch(term)
        if not text:
            return self._send(400, {"error": "text 为空"})
        with db() as c:
            c.execute("INSERT INTO chats(ts, terminal, role, text) VALUES (?,?,?,?)", (now_iso(), term, "user", text))
        p = CFG["persona"]
        ctx, _dropped = llm_context()          # ★ 给模型看的只有脱敏版
        items = analyze()
        lines = "；".join(i["text"] for i in items[:3]) or "没什么要提醒的"
        reply = f"{p['self_call']}在。{lines}。"
        if re.search(r"睡|作息", text):
            s = None
            with db() as c:
                s = _latest_metric(c, "sleep.total_minutes")
            reply = (f"你昨晚睡了 {int(s['value']) // 60} 小时 {int(s['value']) % 60} 分。"
                     if s and s.get("value") else "还没拿到你的睡眠数据呢。")
        elif re.search(r"今天|干啥|要做什么", text):
            reply = "今天要做：" + (lines if lines != "没什么要提醒的" else "暂时没有记录的待办。")
        with db() as c:
            c.execute("INSERT INTO chats(ts, terminal, role, text) VALUES (?,?,?,?)", (now_iso(), term, "persona", reply))
        return self._send(200, {"ok": True, "reply": reply, "persona": p})

    # ---------------------------------------------------------------- 一次性配对（MCU/新设备）
    def _pair(self, query):
        """设备友好的一次性配对：GET /api/pair?c=码&d=设备名 → **第一行就是 token**（或 err:xxx）。

        为什么回纯文本：单片机不用解析 JSON，一行 strtok 就够。
        码是**一次性**的 —— 换过即废，所以设备侧该存下来的是 token，不是码。
        """
        q = parse_qs(query)
        g = lambda k: (q.get(k) or [""])[0]
        code = g("c") or g("code")
        dev = g("d") or g("device") or "mcu"
        ok, res = pair_claim(code, dev, actor=self._client())
        if not ok:
            return self._send_text(400, "err:" + str(res.get("err")))
        print(f"[pair] {dev} 用一次性码换到 token", flush=True)
        return self._send_text(200, res["token"])

    # ---------------------------------------------------------------- 管理台（登录 / 会话）
    @staticmethod
    def _same(a, b) -> bool:
        """常数时间比较（外部评审 3.5-1）：用 == 比较 token 是理论上的时序侧信道。
        网络抖动远大于这点差异 → 低危；但改 compare_digest 零成本，没理由留着。"""
        import hmac as _hmac
        a, b = str(a or ""), str(b or "")
        return len(a) == len(b) and _hmac.compare_digest(a, b)

    def _secure_flag(self) -> str:
        """HTTPS 上必须带 Secure；HTTP 上不能带（否则本地调试登录不了）。
        判据取自连接本身，同进程同时听 11440/11443 也正确。"""
        try:
            import ssl as _ssl
            return "; Secure" if isinstance(self.connection, _ssl.SSLSocket) else ""
        except Exception:
            return ""

    def _logged_in(self, q):
        return (self._same(self.headers.get("X-Token"), CFG["token"])
                or (self._cookie_sess() != "" and self._same(self._cookie_sess(), admin_session())))

    def _login(self, q):
        if self._logged_in(q):
            return self._admin_page(q)
        return self._send(200, login_html(CFG["persona"]["name"]), "text/html; charset=utf-8")

    def _logout(self):
        audit("logout", actor=self._client(), note="退出管理台")
        self.send_response(303)
        self.send_header("Set-Cookie", "whale_admin=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
        self.send_header("Location", "/login")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _login_post(self):
        form = self._form()
        tok = str(form.get("token") or self.headers.get("X-Token") or "")
        who = self._client()
        # ★ 抗枚举：先看锁（5 次错 / 10 分钟 → 锁 15 分钟），成功一次清零
        _ok_t, _wait = auth_throttle_check(who)
        if not _ok_t:
            audit("login", actor=who, result="denied", note="触发限流，还剩 %d 秒" % _wait)
            return self._send(429, login_html(CFG["persona"]["name"],
                                              "试太多次了，请等 %d 秒后再试" % _wait),
                              "text/html; charset=utf-8")
        if not self._same(tok, CFG["token"]):
            _lock = auth_throttle_fail(who)
            audit("login", actor=who, result="denied",
                  note="口令不对" + ("（已锁定 %d 秒）" % _lock if _lock else ""))
            return self._send(401, login_html(CFG["persona"]["name"], "口令不对，再试一次"),
                              "text/html; charset=utf-8")
        auth_throttle_ok(who)
        audit("login", actor=who, note="登录管理台")
        self.send_response(303)
        self.send_header("Set-Cookie",
                         "whale_admin=%s; Path=/; HttpOnly; SameSite=Strict%s"
                         % (admin_session(), self._secure_flag()))
        self.send_header("Location", "/admin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _form(self):
        """解析表单体（浏览器 <form> 用 urlencoded；也容忍 JSON）。不引任何前端框架。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if not n:
            return {}
        raw = self.rfile.read(min(n, MAX_BODY)).decode("utf-8", "replace")
        if "json" in (self.headers.get("Content-Type") or "").lower():
            try:
                d = json.loads(raw)
                return d if isinstance(d, dict) else {}
            except Exception:
                return {}
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def _admin_page(self, q):
        """★ 这里原来是**免鉴权**的极简状态页（公网谁都能看到今天的提醒与设备名）。

        现在：没登录只给登录页；登录后是管理台（同一套 token，不额外造一套权限）。
        """
        if not self._logged_in(q):
            audit("auth_fail", target="/admin", actor=self._client(),
                  result="denied", note="未登录访问管理台")
            return self._send(401, login_html(CFG["persona"]["name"]), "text/html; charset=utf-8")
        return self._send(200, admin_html(), "text/html; charset=utf-8")

    def _admin_post(self, q):
        if not self._auth(q):
            return
        form = self._form()
        ok, msg = admin_apply(form, actor=self._client())
        audit("config_change", target=str(form.get("section") or "admin")[:40],
              actor=self._client(), result="ok" if ok else "denied", note=str(msg)[:100])
        return self._send(200 if ok else 400, admin_html(flash=msg, ok=ok),
                          "text/html; charset=utf-8")
