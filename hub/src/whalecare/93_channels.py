# ---------------------------------------------------------------- 出口（直发通道）
# 为什么要有它：主出口是"说话层 → Hermes 网关 webhook → 微信"，那条链依赖网关活着。
# 这里给中枢自己开**直发**通道，一台 4C4G 的机器 + 一个 webhook 就能把话说出去：
#   · 企业微信群机器人（官方接口、无限流）—— 填 channels.wecom_webhook 即可，不需要 corpid/secret
#   · 通用 webhook —— 任何接受 POST {"text": "..."} 的地址（自建小服务、Slack/Discord 中转等）
#   · ntfy —— 极简自托管推送：把文本 POST 到 https://ntfy.sh/<你的主题> 即达（可自建服务端）
#   · Bark —— iOS 极简推送：https://api.day.app/<你的key>/<内容>（可自建服务端）
#   · 钉钉 —— 自定义机器人 webhook（可选加签 secret，用官方 sign 算法）
#   · Discord —— 频道 webhook（POST {"content": ...}，单条上限 2000 字）
#   · QQ —— **官方机器人 API**（QQ 开放平台的 AppID + Secret，直接 HTTPS 调用，不装任何 SDK）；
#            群里发就把 qq_kind 设成 group。注意官方平台需要你先在 q.qq.com 建好机器人并通过审核
# 刻意**不**自动发：自动发会和说话层重复推送。它是个"通道"，由调用方（人 / cron / 扩展）决定何时用。
CHANNEL_KEYS = ("wecom_webhook", "generic_webhook", "wecom_corpid", "wecom_secret", "wecom_agentid",
                "wecom_touser", "ntfy_url", "ntfy_token", "bark_url", "bark_sound",
                "dingtalk_webhook", "dingtalk_secret", "discord_webhook",
                "qq_appid", "qq_secret", "qq_target", "qq_kind", "qq_api_base", "qq_token_url",
                # 第 9 个出口（数据出口）—— 见文件末尾注释
                "analysis_webhook", "analysis_file", "analysis_keep")


def channels_status(cfg=None):
    """各出口配没配（给人看的；**不打印 webhook 地址本身**，它带密钥）。"""
    ch = (cfg or CFG).get("channels") or {}
    return {
        "wecom_bot": "已配置" if ch.get("wecom_webhook") else "空",
        "generic_webhook": "已配置" if ch.get("generic_webhook") else "空",
        "wecom_app": "已配置" if all(ch.get(k) for k in ("wecom_corpid", "wecom_secret", "wecom_agentid"))
                     else "空（需要 corpid + secret + agentid）",
        "ntfy": "已配置" if ch.get("ntfy_url") else "空（填 https://ntfy.sh/你的主题）",
        "bark": "已配置" if ch.get("bark_url") else "空（填 https://api.day.app/你的key）",
        "dingtalk": "已配置" + ("（已加签）" if ch.get("dingtalk_secret") else "") if ch.get("dingtalk_webhook")
                    else "空（填钉钉自定义机器人 webhook）",
        "discord": "已配置" if ch.get("discord_webhook") else "空（填频道 webhook 地址）",
        "qq": ("已配置（%s）" % ("群" if (ch.get("qq_kind") or "user") == "group" else "私聊"))
              if all(ch.get(k) for k in ("qq_appid", "qq_secret", "qq_target"))
              else "空（需官方 AppID + Secret + 目标 openid）",
        # ★ 第 9 个出口是**数据出口**（发结构化数据，不发句子）—— 状态与上面 8 个分开报，
        #   免得有人以为"配了它她就会多说话"
        "analysis": ("已配置（%s）" % "、".join(
            [n for n, v in (("webhook", ch.get("analysis_webhook")),
                            ("file", ch.get("analysis_file"))) if v])
            if (ch.get("analysis_webhook") or ch.get("analysis_file"))
            else "空（数据出口；不填也能用 GET /analysis 拉最近一次）"),
        "note": "主出口仍是说话层→网关；这里是中枢**直发**通道，不自动使用"
                "（analysis 发**结构化数据**，其余 8 个发**文本**）",
    }


_QQ_TOKEN = {}      # QQ 官方 access_token 缓存（约 2 小时过期）


def _post_json(url, payload, timeout=10):
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.getcode(), r.read(200).decode("utf-8", "replace")


def channel_send(text, cfg=None):
    """把一条消息发到所有已配置的直发出口。返回 {出口: "ok"/错误}。

    企业微信群机器人要求的 payload 是 {"msgtype":"text","text":{"content": ...}}；
    通用出口只要求 {"text": ...}（够简单，自己搭个转发服务就能接）。
    """
    ch = (cfg or CFG).get("channels") or {}
    text = str(text or "").strip()
    out = {}
    if not text:
        return {"error": "内容为空"}
    url = (ch.get("wecom_webhook") or "").strip()
    if url:
        try:
            st, body = _post_json(url, {"msgtype": "text", "text": {"content": text[:1800]}})
            out["wecom_bot"] = "ok" if st == 200 else "HTTP %s" % st
            if st == 200 and '"errcode":0' not in body.replace(" ", ""):
                out["wecom_bot"] = "被拒：%s" % body[:80]
        except Exception as e:
            out["wecom_bot"] = "%s: %s" % (type(e).__name__, str(e)[:60])
    url2 = (ch.get("generic_webhook") or "").strip()
    if url2:
        try:
            st, _ = _post_json(url2, {"text": text[:1800]})
            out["generic"] = "ok" if st == 200 else "HTTP %s" % st
        except Exception as e:
            out["generic"] = "%s: %s" % (type(e).__name__, str(e)[:60])
    # ── ntfy：把文本**原样**POST 到主题地址（不是 JSON！这是它自己的协议）──
    ntfy = (ch.get("ntfy_url") or "").strip()
    if ntfy:
        try:
            import urllib.request
            hdr = {"Title": ("whalecare".encode()).decode(),
                   "Tags": "whale", "Content-Type": "text/plain; charset=utf-8"}
            tok = (ch.get("ntfy_token") or "").strip()
            if tok:
                hdr["Authorization"] = "Bearer " + tok
            req = urllib.request.Request(ntfy, data=text[:1800].encode("utf-8"), headers=hdr, method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                out["ntfy"] = "ok" if r.getcode() == 200 else "HTTP %s" % r.getcode()
        except Exception as e:
            out["ntfy"] = "%s: %s" % (type(e).__name__, str(e)[:60])

    # ── Bark：经典路径式（https://api.day.app/<key>/<标题>/<内容>?sound=xxx）──
    bark = (ch.get("bark_url") or "").strip()
    if bark:
        try:
            import urllib.parse
            import urllib.request
            base = bark.rstrip("/")
            title = "whalecare"
            seg = "%s/%s/%s" % (base, urllib.parse.quote(title), urllib.parse.quote(text[:900]))
            snd = (ch.get("bark_sound") or "").strip()
            if snd:
                seg += "?sound=" + urllib.parse.quote(snd)
            with urllib.request.urlopen(seg, timeout=10) as r:
                body = r.read(200).decode("utf-8", "replace")
                ok = r.getcode() == 200 and '"code":200' in body.replace(" ", "")
                out["bark"] = "ok" if ok else ("被拒：%s" % body[:60])
        except Exception as e:
            out["bark"] = "%s: %s" % (type(e).__name__, str(e)[:60])

    # ── 钉钉：自定义机器人（{"msgtype":"text","text":{"content":...}}，可选加签）──
    dt = (ch.get("dingtalk_webhook") or "").strip()
    if dt:
        try:
            import base64
            import hashlib
            import hmac
            import time
            import urllib.parse
            url = dt
            sec = (ch.get("dingtalk_secret") or "").strip()
            if sec:
                ts = str(int(time.time() * 1000))
                raw = "%s\n%s" % (ts, sec)
                sign = urllib.parse.quote_plus(base64.b64encode(
                    hmac.new(sec.encode(), raw.encode(), hashlib.sha256).digest()))
                url = "%s%s&timestamp=%s&sign=%s" % (dt, "&" if "?" in dt else "?", ts, sign)
            st, body = _post_json(url, {"msgtype": "text", "text": {"content": text[:1800]}})
            out["dingtalk"] = "ok" if (st == 200 and '"errcode":0' in body.replace(" ", "")) \
                else ("被拒：%s" % body[:80] if st == 200 else "HTTP %s" % st)
        except Exception as e:
            out["dingtalk"] = "%s: %s" % (type(e).__name__, str(e)[:60])

    # ── Discord：频道 webhook（{"content": ...}，上限 2000 字）──
    dc = (ch.get("discord_webhook") or "").strip()
    if dc:
        try:
            st, body = _post_json(dc, {"content": text[:1900]})
            out["discord"] = "ok" if st in (200, 204) else "HTTP %s %s" % (st, body[:60])
        except Exception as e:
            out["discord"] = "%s: %s" % (type(e).__name__, str(e)[:60])

    # ── QQ：**官方机器人 API**（QQ 开放平台 AppID + Secret；不依赖任何 SDK）──
    #   ① 取 access_token：POST https://bots.qq.com/app/getAppAccessToken
    #   ② 发消息：私聊 POST {api}/v2/users/{openid}/messages（群 /v2/groups/{group_openid}/messages）
    #      头 Authorization: QQBot <token>，体 {"content": "...", "msg_type": 0}
    appid = str(ch.get("qq_appid") or "").strip()
    qsec = str(ch.get("qq_secret") or "").strip()
    qtgt = str(ch.get("qq_target") or "").strip()
    if appid and qsec and qtgt:
        try:
            import time
            import urllib.request
            global _QQ_TOKEN
            tok, exp = _QQ_TOKEN.get("token", ""), float(_QQ_TOKEN.get("exp", 0) or 0)
            if not tok or time.time() > exp - 120:
                tok_url = (ch.get("qq_token_url") or "https://bots.qq.com/app/getAppAccessToken").strip()
                st, body = _post_json(tok_url,
                                      {"appId": appid, "clientSecret": qsec})
                j = json.loads(body or "{}")
                tok = j.get("access_token") or ""
                exp = time.time() + float(j.get("expires_in") or 7200)
                if not tok:
                    raise RuntimeError("取 token 失败：%s" % body[:80])
                _QQ_TOKEN = {"token": tok, "exp": exp}
            kind = (ch.get("qq_kind") or "user").strip().lower()
            api = (ch.get("qq_api_base") or "https://api.sgroup.qq.com").rstrip("/")
            path = ("/v2/groups/%s/messages" % qtgt) if kind == "group" else ("/v2/users/%s/messages" % qtgt)
            req = urllib.request.Request(api + path,
                                         data=json.dumps({"content": text[:1800], "msg_type": 0},
                                                         ensure_ascii=False).encode(),
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": "QQBot " + tok,
                                                  "X-Union-Appid": appid},
                                         method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                body = r.read(300).decode("utf-8", "replace")
            out["qq"] = "ok" if r.getcode() in (200, 201) else "HTTP %s %s" % (r.getcode(), body[:60])
        except Exception as e:
            out["qq"] = "%s: %s" % (type(e).__name__, str(e)[:60])

    if not out:
        out["error"] = ("没有配置任何直发出口（channels.wecom_webhook / generic_webhook / "
                        "ntfy_url / bark_url / dingtalk_webhook / discord_webhook / qq_appid）")
    try:
        audit("channel_send", target=",".join(sorted(out)), result="ok" if "ok" in str(list(out.values())) else "error",
              note="直发一条（%d 字）" % len(text))
    except Exception:
        pass
    return out


def channel_test(cfg=None):
    """发一条测试消息（配出口时用它验收，别等真有事才发现不通）。"""
    return channel_send("（测试）whalecare 直发通道已连通 —— 收到即说明配置生效。", cfg)


# ══════════════════════════════════════════════════════════════════════════════
#  第 9 个出口：**分析出口**（数据出口）
#
#  和上面 8 个的根本区别是**发的东西**，不是发的地方：
#      · 那 8 个发的是**句子**（她的话）→ 给人看 → 所以会拼问候、会截断到 1800 字
#      · 这个发的是**结构化数据**（AI 分析后的结果）→ 给**机器**看 →
#        所以它**一个字都不加工**：原样 JSON，不句化、不加称呼、不截断成散文
#
#  "利用 AI 分析处理数据"这一步**不在这里做**，原因是一条红线：
#  中枢跑在服务器上、**不持有任何模型 API key**（key 只在本机，见 docs/PRIVACY.md）。
#  所以分工是：
#      说话层 `whale_analyze.py`（本机，有 key）→ 脱敏上下文 → 让模型输出 JSON
#        → POST /analysis → **这里**：落库 + 分发给消费者（webhook / 文件）
#  这样"AI 分析"和"出口配置"各自待在正确的那一边。
#
#  消费者（都属于"机器"这一类）：
#      · `channels.analysis_webhook` —— POST 一份 JSON 给你自己的看板/服务
#      · `channels.analysis_file`    —— 原子落一个 JSON 文件给挂件/网页/设备读
#      · `GET /analysis`             —— 直接拉最近一次（不配任何出口也能用）
# ══════════════════════════════════════════════════════════════════════════════
ANALYSIS_MAX_BYTES = 64 * 1024      # 一份分析的上限：它是数据，不该长成散文
ANALYSIS_TOP_KEYS = ("v", "day", "values", "trends", "outliers", "pairs", "scores", "tags", "notes")

# ★ 格式规范 = **docs/analysis.schema.json**，这里嵌的是它的**原文**。
#   为什么单文件分发要自带规范：① 中枢必须能独立校验（不读磁盘、不装 jsonschema —— 本项目零依赖）
#   ② 要能把规范本身发给消费者（GET /analysis/schema）。
#   两份漂移由 tests/test_analysis_schema.py 兜住（嵌入原文 vs 仓库文档，按结构逐项比对）。
ANALYSIS_SCHEMA_JSON = r'''{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://github.com/DpVoliin/whalecare/blob/main/docs/analysis.schema.json",
  "title": "whalecare 分析出口 · 数据格式",
  "description": "分析出口（第 9 个出口）的 payload 里 analysis 字段的规范格式。中枢在 POST /analysis 时按本文件**硬校验**，不符合直接拒收。人读版说明见 docs/ANALYSIS-FORMAT.md。",
  "version": 1,
  "type": "object",
  "required": ["v"],
  "additionalProperties": false,
  "$defs": {
    "label": {
      "type": "string",
      "minLength": 1,
      "maxLength": 24,
      "pattern": "^[^。，！？；：、\\n\\r]*$",
      "description": "短标签：≤24 字，且不含句读 —— 数据出口不装句子"
    },
    "metric_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 48,
      "pattern": "^[^\\s。，！？；：、\"'（）()\\[\\]【】{}<>《》/\\\\|]+$",
      "description": "指标 id：必须与中枢 llm_context 里的键**逐字相同**（点号路径或叶子键都认）。允许中文（如游戏名/分类名），但不含空白、句读、引号与括号 —— 它是标识符，不是文本"
    },
    "conf": {
      "enum": ["low", "mid", "high"],
      "description": "置信度；样本天数 n < 5 时必须为 low"
    }
  },
  "properties": {
    "v": {
      "const": 1,
      "description": "格式版本。本文件即 v1；破坏性改动必须升版本，读方应拒收不认识的 v"
    },
    "day": {
      "type": "string",
      "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
      "description": "这份分析针对哪一天（本地时区）"
    },
    "values": {
      "type": "array",
      "maxItems": 40,
      "description": "指标现值（只放上下文中真实存在的指标）",
      "items": {
        "type": "object",
        "required": ["id", "v"],
        "additionalProperties": false,
        "properties": {
          "id": { "$ref": "#/$defs/metric_id" },
          "v": { "type": "number" },
          "unit": { "$ref": "#/$defs/label" },
          "unverified": {
            "type": "boolean",
            "description": "true = 该数值无法与上下文直接对上（派生值/换算值）。写端应尽力核对，读端应把它当弱证据"
          }
        }
      }
    },
    "trends": {
      "type": "array",
      "maxItems": 40,
      "description": "趋势（相对基线）。方向必须由数值算出，不是形容",
      "items": {
        "type": "object",
        "required": ["id", "dir"],
        "additionalProperties": false,
        "properties": {
          "id": { "$ref": "#/$defs/metric_id" },
          "dir": { "enum": ["up", "down", "flat"] },
          "delta_pct": { "type": "number", "description": "相对变化百分比（+12 = 涨 12%）" },
          "vs": { "$ref": "#/$defs/label", "description": "基线是什么（如 7日均值）" },
          "conf": { "$ref": "#/$defs/conf" }
        }
      }
    },
    "outliers": {
      "type": "array",
      "maxItems": 40,
      "description": "异常点。z 是标准分，不是主观判断",
      "items": {
        "type": "object",
        "required": ["id", "side"],
        "additionalProperties": false,
        "properties": {
          "id": { "$ref": "#/$defs/metric_id" },
          "side": { "enum": ["high", "low"] },
          "z": { "type": "number" },
          "conf": { "$ref": "#/$defs/conf" }
        }
      }
    },
    "pairs": {
      "type": "array",
      "maxItems": 20,
      "description": "两指标的共变关系。n 是样本天数，必须给 —— 没有 n 的相关系数是耍流氓",
      "items": {
        "type": "object",
        "required": ["a", "b"],
        "additionalProperties": false,
        "properties": {
          "a": { "$ref": "#/$defs/metric_id" },
          "b": { "$ref": "#/$defs/metric_id" },
          "rho": { "type": "number", "minimum": -1, "maximum": 1 },
          "n": { "type": "integer", "minimum": 1 },
          "conf": { "$ref": "#/$defs/conf" }
        }
      }
    },
    "scores": {
      "type": "array",
      "maxItems": 20,
      "description": "派生评分（0~of 的整数分制）。自定义名字用短标签，不得含句读",
      "items": {
        "type": "object",
        "required": ["id", "v"],
        "additionalProperties": false,
        "properties": {
          "id": { "$ref": "#/$defs/label" },
          "v": { "type": "number", "minimum": 0, "maximum": 100 },
          "of": { "type": "number", "minimum": 1, "description": "满分，默认 100" }
        }
      }
    },
    "tags": {
      "type": "array",
      "maxItems": 12,
      "description": "状态标签（短词，如 睡眠不足 / 久坐）。读端用来做筛选与着色",
      "items": { "$ref": "#/$defs/label" }
    },
    "notes": {
      "type": "array",
      "maxItems": 6,
      "description": "数据侧的说明（**不是**给她/给人的嘱咐）。缺数据、口径变化等",
      "items": { "$ref": "#/$defs/label" }
    }
  },
  "examples": [
    {
      "v": 1,
      "day": "2026-09-28",
      "values": [
        { "id": "screen_active_minutes", "v": 554, "unit": "min" },
        { "id": "sleep_minutes", "v": 420, "unit": "min" }
      ],
      "trends": [
        { "id": "screen_active_minutes", "dir": "up", "delta_pct": 14, "vs": "7日均值", "conf": "mid" }
      ],
      "outliers": [
        { "id": "screen_active_minutes", "side": "high", "z": 2.1, "conf": "mid" }
      ],
      "pairs": [
        { "a": "game_minutes", "b": "screen_active_minutes", "rho": 0.62, "n": 9, "conf": "mid" }
      ],
      "scores": [
        { "id": "作息规律", "v": 72, "of": 100 }
      ],
      "tags": ["睡眠不足", "久坐"],
      "notes": ["心率今日缺"]
    }
  ]
}'''

# ★ 信封里报的格式版本号：**从规范本身读**（不手工写第二遍，免得两处漂移）
ANALYSIS_SCHEMA_VERSION = json.loads(ANALYSIS_SCHEMA_JSON).get("version", 1)

# 零依赖校验器：规则表是把上面那份 schema 手工映射过来的（改 schema 必须同步改这里，测试会拦）
_LABEL_BAD = "。，！？；：、\n\r"
_LABEL_MAX = 24
# 标识符（指标 id）禁用字符：空白 + 句读 + 引号括号 —— 允许中文（游戏名/分类名也可能是键）
_ID_BAD = set(' \t\r\n。，！？；：、"\'（）()[]【】{}<>《》/\\|')


def _en(*vals):
    return ("enum",) + vals


def _num(lo=None, hi=None):
    return ("num", lo, hi)


def _int(lo=None, hi=None):
    return ("int", lo, hi)


_ANALYSIS_ITEM_RULES = {
    "values": {"req": ("id", "v"), "max": 40,
               "f": {"id": ("id",), "v": _num(), "unit": ("label",), "unverified": ("bool",)}},
    "trends": {"req": ("id", "dir"), "max": 40,
               "f": {"id": ("id",), "dir": _en("up", "down", "flat"),
                     "delta_pct": _num(), "vs": ("label",), "conf": _en("low", "mid", "high")}},
    "outliers": {"req": ("id", "side"), "max": 40,
                 "f": {"id": ("id",), "side": _en("high", "low"), "z": _num(),
                       "conf": _en("low", "mid", "high")}},
    "pairs": {"req": ("a", "b"), "max": 20,
              "f": {"a": ("id",), "b": ("id",), "rho": _num(-1, 1), "n": _int(1, None),
                    "conf": _en("low", "mid", "high")}},
    "scores": {"req": ("id", "v"), "max": 20,
               "f": {"id": ("label",), "v": _num(0, 100), "of": _num(1, None)}},
}
_ANALYSIS_LABEL_ARRAYS = {"tags": 12, "notes": 6}


def _chk(spec, val):
    """按一条字段规则检查单个值，返回错误说明（"" = 通过）。"""
    kind = spec[0]
    if kind == "label":
        if not isinstance(val, str) or not val or len(val) > _LABEL_MAX or any(c in val for c in _LABEL_BAD):
            return "要短标签（1~%d 字且不含句读）" % _LABEL_MAX
        return ""
    if kind == "id":
        if not isinstance(val, str) or not val or len(val) > 48 or any(c in _ID_BAD for c in val):
            return "要指标 id（1~48 字，且不含空白/句读/引号括号）"
        return ""
    if kind == "bool":
        return "" if isinstance(val, bool) else "要布尔值"
    if kind in ("num", "int"):
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return "要数字"
        if kind == "int" and not float(val).is_integer():
            return "要整数"
        lo, hi = spec[1], spec[2]
        if lo is not None and val < lo:
            return "不得小于 %s" % lo
        if hi is not None and val > hi:
            return "不得大于 %s" % hi
        return ""
    if kind == "enum":
        return "" if val in spec[1:] else "只能是 %s" % "/".join(str(x) for x in spec[1:])
    return "未知规则"


def analysis_validate(data):
    """按 docs/analysis.schema.json 校验一份分析结果。返回错误列表（空列表 = 合规）。

    硬校验（不是提醒）：`POST /analysis` 不合规直接拒收 —— 出口的契约就是"数据"，
    放一段话进来会污染所有下游（看板 / 脚本 / 设备），而且事后没法补救。
    """
    errs = []
    if not isinstance(data, dict):
        return ["顶层必须是一个 JSON 对象"]
    for k in data:
        if k not in ANALYSIS_TOP_KEYS:
            errs.append("顶层多了不允许的键：%s" % k)
    if data.get("v") != 1:
        errs.append("v 必须等于 1（v1 格式；不认识的版本读方应拒收）")
    d = data.get("day")
    if d is not None and not (isinstance(d, str) and len(d) == 10 and d[4] == "-" and d[7] == "-"):
        errs.append("day 要 YYYY-MM-DD（收到 %r）" % (d,))
    for key, rule in _ANALYSIS_ITEM_RULES.items():
        v = data.get(key)
        if v is None:
            continue
        if not isinstance(v, list):
            errs.append("%s 必须是数组" % key)
            continue
        if len(v) > rule["max"]:
            errs.append("%s 最多 %d 条（现在 %d）" % (key, rule["max"], len(v)))
        for n, it in enumerate(v[:rule["max"]]):
            if not isinstance(it, dict):
                errs.append("%s[%d] 必须是对象" % (key, n))
                continue
            for f in rule["req"]:
                if f not in it:
                    errs.append("%s[%d] 缺必填字段 %s" % (key, n, f))
            for f, fs in rule["f"].items():
                if f in it:
                    bad = _chk(fs, it[f])
                    if bad:
                        errs.append("%s[%d].%s %s（收到 %r）" % (key, n, f, bad, it[f]))
            for f in sorted(it):
                if f not in rule["f"]:
                    errs.append("%s[%d] 多了不允许的字段：%s" % (key, n, f))
    for key, mx in _ANALYSIS_LABEL_ARRAYS.items():
        v = data.get(key)
        if v is None:
            continue
        if not isinstance(v, list):
            errs.append("%s 必须是数组" % key)
            continue
        if len(v) > mx:
            errs.append("%s 最多 %d 条" % (key, mx))
        for n, x in enumerate(v[:mx]):
            bad = _chk(("label",), x)
            if bad:
                errs.append("%s[%d] %s（收到 %r）" % (key, n, bad, x))
    if len(errs) > 20:
        errs = errs[:20] + ["……还有 %d 条" % (len(errs) - 20)]
    return errs


def analysis_payload(data, engine="", day=""):
    """包一层**信封**（信封里只有元数据，没有句子）。"""
    return {"type": "analysis", "schema": ANALYSIS_SCHEMA_VERSION, "at": now_iso(),
            "day": str(day or today_str())[:10], "engine": str(engine or "")[:60],
            "analysis": data}


def _write_json_atomic(path, payload):
    """原子写 JSON：先写 `.tmp` 再 `os.replace`。

    为什么不用直接 `open(w).write()`：读方是**别的进程**（挂件/网页/设备），
    直接写会让它们读到**半个文件**（JSON 解析失败 → 看板上数字忽然全空）。
    `os.replace` 在同一文件系统上是原子的。
    """
    import os
    p = os.path.expanduser(str(path))
    d = os.path.dirname(os.path.abspath(p))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    return "ok"


def analysis_send(payload, cfg=None):
    """把已包好的分析信封发到**分析出口**。返回 {出口: "ok"/错误}（只走数据出口，不碰文本出口）。"""
    ch = (cfg or CFG).get("channels") or {}
    out = {}
    url = (ch.get("analysis_webhook") or "").strip()
    if url:
        try:
            st, body = _post_json(url, payload, timeout=15)
            out["analysis_webhook"] = "ok" if st in (200, 201, 202, 204) else "HTTP %s %s" % (st, body[:60])
        except Exception as e:
            out["analysis_webhook"] = "%s: %s" % (type(e).__name__, str(e)[:60])
    path = (ch.get("analysis_file") or "").strip()
    if path:
        try:
            out["analysis_file"] = _write_json_atomic(path, payload)
        except Exception as e:
            out["analysis_file"] = "%s: %s" % (type(e).__name__, str(e)[:60])
    if not out:
        out["note"] = ("没配 channels.analysis_webhook / analysis_file —— "
                       "分析已入库，仍可用 GET /analysis 拉最近一次")
    try:
        audit("analysis_send", target=",".join(sorted(k for k in out if k != "note")),
              result="ok" if "ok" in str(list(out.values())) else "error",
              note="分析出口一份（%d 字节）" % len(json.dumps(payload, ensure_ascii=False)))
    except Exception:
        pass
    return out


def analysis_store(engine, data, day=""):
    """落库（分析出口的发件箱）+ 按 `channels.analysis_keep` 裁剪。返回新记录 id。"""
    try:
        keep = int((CFG.get("channels") or {}).get("analysis_keep") or 50)
    except (TypeError, ValueError):
        keep = 50
    blob = json.dumps(data, ensure_ascii=False)
    with db() as c:
        cur = c.execute("INSERT INTO analyses(ts, day, engine, data) VALUES (?,?,?,?)",
                        (now_iso(), str(day or today_str())[:10], str(engine or "")[:60], blob))
        rid = cur.lastrowid
        if keep > 0:
            c.execute("DELETE FROM analyses WHERE id NOT IN "
                      "(SELECT id FROM analyses ORDER BY id DESC LIMIT ?)", (keep,))
    return rid


def analysis_recent(limit=1):
    """最近 N 份分析（新的在前）。`data` 解析失败时给 None，不炸。"""
    with db() as c:
        rows = c.execute("SELECT id, ts, day, engine, data FROM analyses "
                         "ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)).fetchall()
    out = []
    for r in rows:
        try:
            d = json.loads(r["data"])
        except Exception:
            d = None
        out.append({"id": r["id"], "ts": r["ts"], "day": r["day"],
                    "engine": r["engine"], "analysis": d})
    return out


def analysis_ingest(body, client="", validate_only=False):
    """`POST /analysis` 的处理：**按规范硬校验** → 落库 → 分发给分析出口。

    validate_only（`?validate=1`）：只校验不落库 —— 写端可以先干跑一遍再发。
    """
    b = body if isinstance(body, dict) else {}
    data = b.get("analysis") if isinstance(b.get("analysis"), dict) else b
    if not isinstance(data, dict) or not data:
        return {"ok": False, "error": "要传一个 JSON 对象（分析结果）：`{...}` 或 `{\"analysis\": {...}}`"}
    size = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    if size > ANALYSIS_MAX_BYTES:
        return {"ok": False, "error": "分析结果太大（%d 字节 > %d）—— 数据出口不该发这么长的东西"
                % (size, ANALYSIS_MAX_BYTES)}
    errs = analysis_validate(data)
    if validate_only:
        return {"ok": not errs, "errors": errs, "bytes": size, "schema": "/analysis/schema"}
    if errs:
        # ★ 拒收（不是警告）：格式是出口的契约；放进来会污染所有下游，事后没法补救
        try:
            audit("analysis_reject", target=",".join(sorted(data)[:6]), actor=client or "?",
                  result="denied", note="不符合 schema v1 的 %d 条" % len(errs))
        except Exception:
            pass
        return {"ok": False, "rejected": True, "bytes": size, "errors": errs, "schema": "/analysis/schema",
                "error": "不符合 docs/analysis.schema.json（v1）—— 已拒收，未落库未分发"}
    engine = str(b.get("engine") or b.get("model") or "")[:60]
    day = str(b.get("day") or "")[:10]
    rid = analysis_store(engine, data, day)
    fan = analysis_send(analysis_payload(data, engine=engine, day=day))
    res = {"ok": True, "id": rid, "bytes": size, "fanout": fan, "schema": "/analysis/schema",
           "note": "已入库；GET /analysis 可拉最近一次"}
    unver = sum(1 for it in (data.get("values") or []) if isinstance(it, dict) and it.get("unverified"))
    if unver:
        res["warn"] = "有 %d 个数值标了 unverified（对不上上下文，读端应视作弱证据）" % unver
    print("[analysis] #%s %s %d 字节 → %s" % (rid, engine or "-", size,
                                              ",".join("%s=%s" % kv for kv in fan.items())), flush=True)
    return res


def analysis_view(q=None):
    """`GET /analysis`：最近 N 次分析 + 出口状态（挂件/脚本/人 都能拉）。"""
    q = q or {}
    try:
        n = int((q.get("limit") or ["1"])[0])
    except (TypeError, ValueError):
        n = 1
    n = max(1, min(20, n))
    items = analysis_recent(n)
    try:
        st = channels_status().get("analysis")
    except Exception:
        st = ""
    return {"count": len(items), "items": items, "analysis_channel": st,
            "schema": "/analysis/schema",
            "schema_version": json.loads(ANALYSIS_SCHEMA_JSON).get("version"),
            "how_to_write": "POST /analysis {\"analysis\": {...}, \"engine\": \"模型名\"}（X-Token 头）；"
                            "可先干跑 POST /analysis?validate=1"}
