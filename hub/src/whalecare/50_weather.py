# ---- 天气：主用中国天气网（中国气象局数据，与大厂手机天气同源）；Open-Meteo 兜底 ----
WX_CODE = {
    "00": "晴", "01": "多云", "02": "阴", "03": "阵雨", "04": "雷阵雨", "05": "雷阵雨伴冰雹",
    "06": "雨夹雪", "07": "小雨", "08": "中雨", "09": "大雨", "10": "暴雨", "11": "大暴雨",
    "12": "特大暴雨", "13": "阵雪", "14": "小雪", "15": "中雪", "16": "大雪", "17": "暴雪",
    "18": "雾", "19": "冻雨", "20": "沙尘暴", "21": "小到中雨", "22": "中到大雨", "23": "大到暴雨",
    "24": "暴雨到大暴雨", "25": "大暴雨到特大暴雨", "26": "小到中雪", "27": "中到大雪",
    "28": "大到暴雪", "29": "浮尘", "30": "扬沙", "31": "强沙尘暴", "53": "霾",
}
WX_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
         "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


def _json_in(text):
    """从 'var x ={...};var y=...' 里抠出**第一个完整** JSON 对象（官方接口后面带尾巴）。"""
    i = text.find("{")
    if i < 0:
        return None
    depth = 0
    for k in range(i, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[i:k + 1])
                except Exception:
                    return None
    return None


def wx_search_city(name):
    """把城市名换成中国天气网代码（任意城市；多地用户就靠这个）。"""
    import urllib.parse as _up
    import urllib.request as _rq
    req = _rq.Request("http://toy1.weather.com.cn/search?cityname=" + _up.quote(name) + "&_=1")
    req.add_header("User-Agent", WX_UA)
    req.add_header("Referer", "http://www.weather.com.cn/")
    with _rq.urlopen(req, timeout=15) as r:
        t = r.read().decode("utf-8", "replace")
    i, j = t.find("["), t.rfind("]")
    if i < 0 or j < 0:
        return []
    out = []
    for a in json.loads(t[i:j + 1]):
        ref = str(a.get("ref") or "")
        code = ref.split("~")[0]
        if code.isdigit():
            parts = ref.split("~")
            out.append({"code": code, "name": parts[2] if len(parts) > 2 else (a.get("name") or ""),
                        "province": parts[-1] if len(parts) > 3 else ""})
    return out


def wx_official(code):
    """中国天气网：一次请求拿到 实况 + 今日 + 未来几天 + 预警 + 生活指数。"""
    import urllib.request as _rq
    req = _rq.Request(f"http://d1.weather.com.cn/weather_index/{code}.html")
    req.add_header("User-Agent", WX_UA)
    req.add_header("Referer", "http://www.weather.com.cn/")
    with _rq.urlopen(req, timeout=15) as r:
        page = r.read().decode("utf-8", "replace")
    out = {}
    for name in ("dataSK", "cityDZ", "alarmDZ", "fc", "dataZS"):
        idx = page.find("var %s =" % name)
        out[name] = (_json_in(page[idx:]) if idx >= 0 else None) or {}
    return out


def weather_from_official(code):
    """整理成要存的几条指标（city_code 写进 meta，便于多地用户各自取自己的）。"""
    d = wx_official(code)
    sk = d.get("dataSK") or {}
    days = (d.get("fc") or {}).get("f") or []
    zs = (d.get("dataZS") or {}).get("zs") or {}
    alerts = (d.get("alarmDZ") or {}).get("w") or []

    def num(x):
        try:
            return float(str(x).replace("℃", "").replace("%", "").strip())
        except Exception:
            return None

    base = {"city_code": str(code), "city": sk.get("cityname") or "", "src": "中国天气网"}
    items = []
    if sk:
        m = dict(base)
        m.update({"desc": sk.get("weather") or "", "humidity": num(sk.get("SD")),
                  "wind": f"{sk.get('WD','')}{sk.get('WS','')}".strip(),
                  "rain_1h": num(sk.get("rain")), "rain_24h": num(sk.get("rain24h")),
                  "aqi": num(sk.get("aqi")), "vis_km": num(sk.get("njd")),
                  "observed_at": sk.get("time")})
        items.append({"metric": "weather.now", "value": num(sk.get("temp")), "unit": "C", "meta": m})
    for i, f in enumerate(days[:3]):
        a, b = f.get("fa") or "", f.get("fb") or ""
        desc = WX_CODE.get(a, "")
        if b and b != a:
            desc += "转" + WX_CODE.get(b, "")
        m = dict(base)
        m.update({"label": f.get("fj") or ("今天" if i == 0 else ""), "date": f.get("fi") or "",
                  "tmax": num(f.get("fc")), "tmin": num(f.get("fd")), "desc": desc,
                  "wind": f"{f.get('fe','')}{f.get('fg','')}".strip()})
        items.append({"metric": "weather.day", "value": float(i), "unit": "", "meta": m})
    for a in alerts[:2]:
        m = dict(base)
        m.update({"title": a.get("w1") or a.get("title") or "气象预警", "level": a.get("w2") or "",
                  "text": (a.get("w7") or a.get("content") or "")[:80]})
        items.append({"metric": "weather.alert", "value": 1.0, "unit": "", "meta": m})
    if zs:
        m = dict(base)
        m.update({"dress": f"{zs.get('ct_hint','')}｜{zs.get('ct_des_s','')}"[:60],
                  "traffic": f"{zs.get('lk_hint','')}｜{zs.get('lk_des_s','')}"[:60],
                  "sport": f"{zs.get('cl_hint','')}｜{zs.get('cl_des_s','')}"[:60]})
        items.append({"metric": "weather.life", "value": 1.0, "unit": "", "meta": m})
    return items


def weather_cities():
    """要抓哪些城市：默认城市 + 各设备单独配的城市（多地用户就配 devices.<设备>.city_code）。"""
    codes = {}
    pv = CFG.get("privacy") or {}
    codes[str(pv.get("weather_city_code") or "101280101")] = "server"
    for dev, dcfg in (CFG.get("devices") or {}).items():
        cc = (dcfg or {}).get("city_code")
        if cc:
            codes[str(cc)] = dev
    return codes


def fetch_weather(days=2):
    """抓天气（Open-Meteo，免 key）。城市级坐标写在 privacy.weather_lat/lon，不涉及定位。"""
    import urllib.request as _urlreq  # 显式导入：别依赖别处的作用域别名

    # ① 先试官方源（中国气象局数据）：一次拿到实况 + 多天 + 预警 + 生活指数
    saved = 0
    for code, dev in weather_cities().items():
        try:
            items = weather_from_official(code)
        except Exception as e:
            print(f"[weather] 官方源失败({code})：{type(e).__name__} {str(e)[:60]}", flush=True)
            continue
        if not items:
            continue
        with db() as c:
            for it in items:
                c.execute("INSERT INTO metrics(ts, day, device, metric, value, unit, source, confidence, meta) "
                          "VALUES (?,?,?,?,?,?,?,?,?)",
                          (now_iso(), today_str(), dev, it["metric"], it.get("value"), it.get("unit", ""),
                           "weather.com.cn", 1.0, json.dumps(it.get("meta") or {}, ensure_ascii=False)))
                saved += 1
        print(f"[weather] 官方源更新 {len(items)} 条 · {code} → {dev}", flush=True)
    if saved:
        return saved

    # ② 官方源全挂时才退回 Open-Meteo（国际模型，免 key）
    pv = CFG.get("privacy", {})
    lat = pv.get("weather_lat", 23.13)
    lon = pv.get("weather_lon", 113.12)
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
           "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weathercode"
           f"&timezone=Asia%2FShanghai&forecast_days={days}")
    try:
        req = _urlreq.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0 (whalecare)")
        with _urlreq.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode())
    except Exception as e:
        print(f"[weather] 抓取失败：{type(e).__name__} {str(e)[:80]}", flush=True)
        return 0
    dl = d.get("daily") or {}
    dates = dl.get("time") or []
    n = 0
    with db() as c:
        for i, day in enumerate(dates[:days]):
            meta = {
                "tmax": (dl.get("temperature_2m_max") or [None])[i],
                "tmin": (dl.get("temperature_2m_min") or [None])[i],
                "rain": (dl.get("precipitation_probability_max") or [None])[i],
                "code": (dl.get("weathercode") or [None])[i],
                "desc": WEATHER_CODE.get((dl.get("weathercode") or [0])[i], ""),
                "for_day": day,
            }
            c.execute("INSERT INTO metrics(ts, day, device, metric, value, unit, source, confidence, meta) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (now_iso(), today_str(), "server", "weather.day", i, "", "open-meteo", 1.0,
                       json.dumps(meta, ensure_ascii=False)))
            n += 1
    print(f"[weather] 已更新 {n} 天", flush=True)
    return n


_DATE_RE = None


def day_of(m, today=None):
    """一条 weather.day 的 meta 到底指**哪一天**。返回 date 或 None。

    两种来源形状都要认：
      · 中国天气网（主）：`date="9/28"`（**不补零**）、没有 for_day
      · Open-Meteo（兜底）：`for_day="2026-09-29"`（ISO）
    ★ 为什么要专门有此函数（2026-09-29 修的真 bug）：
      60_analysis 原来直接对 `date` 做**字符串排序** —— "10/1" < "9/28"（'1' < '9'）
      ⇒ 9→10 月交界时"今天"被挑成三天后、"明天"被挑成昨天 ✗
      （真实语料里"明天有冰雹""早上提醒明早的干嘛"就是这么来的）。
    """
    global _DATE_RE
    if _DATE_RE is None:
        import re as _re
        _DATE_RE = _re.compile(r"^(?:(\d{4})[-/])?(\d{1,2})[-/](\d{1,2})$")
    if not isinstance(m, dict):
        return None
    today = today or datetime.now(TZ).date()
    for raw in (m.get("for_day"), m.get("date")):
        mt = _DATE_RE.match(str(raw or "").strip())
        if not mt:
            continue
        y, mo, d = mt.group(1), int(mt.group(2)), int(mt.group(3))
        cands = []
        for cy in ([int(y)] if y else [today.year, today.year + 1, today.year - 1]):
            try:
                cands.append(datetime(cy, mo, d).date())
            except Exception:
                continue
        cands = [c for c in cands if abs((c - today).days) <= 200]      # 跨年/跨月自动纠
        if cands:
            return min(cands, key=lambda c: abs((c - today).days))
    return None


def weather_days(today=None, fresh_hours=12, city_code=None):
    """未来几天的预报，**按真实日期排序**（最早一条 = 今天），已过去的日子剔除。

    每天只留**最新一次抓取**的那条（否则同一个日期会有多份重复记录）。
    `city_code` 给了就只认这个城市（多地用户各看各的，与 llm_context 的口径一致）。
    """
    today = today or datetime.now(TZ).date()
    try:
        with db() as c:
            rows = c.execute("SELECT ts, meta FROM metrics WHERE metric='weather.day' "
                             "ORDER BY ts DESC LIMIT 12").fetchall()
    except Exception:
        return []
    best, order = {}, []
    for r in rows:
        try:
            m = json.loads(r["meta"] or "{}")
        except Exception:
            continue
        try:
            fresh = (datetime.now(TZ) - datetime.fromisoformat(r["ts"])).total_seconds() < fresh_hours * 3600
        except Exception:
            fresh = False
        if not fresh:
            continue
        if city_code and str(m.get("city_code") or city_code) != str(city_code):
            continue
        d = day_of(m, today)
        if d is None or d < today:          # ★ 过去的日子不能冒充"今天/明天"
            continue
        if d in best:                       # 同一天：第一次遇到的就是最新的（按 ts DESC ✓）
            continue
        best[d] = m
        order.append(d)
    order.sort()
    out = []
    for d in order:
        m = dict(best[d])
        delta = (d - today).days
        m["for_day"], m["date"] = d.isoformat(), d.isoformat()
        m["label"] = ("今天" if delta == 0 else "明天" if delta == 1
                      else "后天" if delta == 2 else "%d 天后" % delta)
        m.setdefault("city", m.get("city") or "")
        out.append(m)
    return out


def weather_of(which=0, today=None):
    """which=0 今天 / 1 明天 —— 按**真实日期**取，不是"取第 N 条"。拿不到就 None。

    踩过的三个坑（2026-09-29 一起修的）：
      ① 按字符串排日期 → "10/1" < "9/28" ⇒ 挑错天 ✗
      ② 去重键用 `for_day`，而主源（中国天气网）**根本不写 for_day**
         → 全部都是 None → `None in [None]` → 只剩第一条 ⇒ **which=1 永远 None** ✗
      ③ **按索引取第 N 条**本身就危险：今天的预报行一旦缺失，"第 0 条"就是明天、
         "第 1 条"就是后天 ⇒ **后天的天气被当成今天讲**（宁可不说，也别讲错日子）
    """
    t = today or datetime.now(TZ).date()
    want = (t + timedelta(days=int(which))).isoformat()
    for m in weather_days(today=t):
        if str(m.get("for_day")) == want:
            return {"desc": m.get("desc"), "tmin": m.get("tmin"), "tmax": m.get("tmax"),
                    # 主源不提供降水概率 → 老实给 None（**不编**）；有就给
                    "rain_prob": m.get("rain") if m.get("rain") is not None else m.get("rain_prob"),
                    "for_day": m.get("for_day"), "label": m.get("label"), "src": m.get("src") or ""}
    return None



