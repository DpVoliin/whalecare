#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多端数据中枢 hub —— 单文件、零第三方依赖（只用 Python 标准库）。

职责（只做枢纽，不碰任何展示）：
  ① 收：任何设备 POST /ingest 打点进来（设备名 + 指标 + 时间 + 值）→ SQLite
  ② 算：确定性规则引擎算出"今天要干啥 / 该注意什么"（不烧 token）
  ③ 发：各端（电脑挂件 / 微信 / 网页）来取 /today、/pending，或 SSE 实时接
  ④ 人设：/persona 一份，三端共用（改一处三端同步）
  ⑤ 扩展：设备只是"一个指标名"，加新设备 = 让它往 /ingest 打点即可，服务端不用改

设计约定：
  · 指标名统一为 `域.项`：sleep.total_minutes / screen.active_minutes / task.todo …
    新设备把它的数据映射到既有指标，就能直接复用全部提醒逻辑。
  · 一切接口都要 token（X-Token 头或 ?token=），服务器裸在公网，不做鉴权等于送人。
  · 所有数据只落本机 SQLite（/root/hub/hub.db），不转发给任何第三方。
"""
import json
import os
import pathlib
import re
import secrets
import sqlite3
import ssl
import statistics
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def _resolve_home() -> str:
    """决定「数据目录」在哪 —— 配置与数据库都放这里。

    优先级：
      1. 环境变量 WHALE_HOME（显式指定，也是容器/系统服务的推荐方式）
      2. ~/.whale（默认：程序与数据分离，用户不会把 hub.json 丢在下载目录里）
      3. 旧行为兼容：程序所在目录（即 hub/hub.py 旁边，v0.1.x 的既有部署）

    为什么要这么绕：单文件分发（zipapp / 打包）时 __file__ 指向压缩包**内部**路径，
    照旧写法会去写一个不存在的目录而崩掉。把"数据在哪"与"程序在哪"解耦，
    既支持 zipapp，也让升级程序时不会碰到你的数据。
    """
    env = os.getenv("WHALE_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))

    here = os.path.dirname(os.path.abspath(__file__))
    # 旧部署：hub.json 就在程序旁边 → 继续用它（不打扰已经在跑的实例）
    if os.path.exists(os.path.join(here, "hub.json")):
        return here

    # zipapp / 冻结包：__file__ 在压缩包里，旁边不可写 → 退回用户目录
    if ".pyz" in here or getattr(sys, "frozen", False):
        return os.path.join(os.path.expanduser("~"), ".whale")

    # 全新部署：默认用户目录（v0.2 起的新默认）
    return os.path.join(os.path.expanduser("~"), ".whale")


BASE = _resolve_home()
try:
    os.makedirs(BASE, exist_ok=True)
except OSError:
    pass
CFG_PATH = os.path.join(BASE, "hub.json")
DB_PATH = os.path.join(BASE, "hub.db")
VERSION = "0.2.1"
TZ = timezone(timedelta(hours=8))          # 北京时间（用户在国内，固定 +8，避免服务器 UTC 漂移）

DEFAULT_CFG = {
    "port": 11440,
    "token": "",                                  # 首次启动自动生成
    "persona": {
        "name": "鲸鲸",
        "self_call": "鲸鲸",
        "tone": "温柔恭谨的女仆：先接住情绪再给建议，短句、语气软，偶尔俏皮但不越界",
        "likes": "米饭",
        "taboo": "绝对不能说鲸鲸胖",
        "call_user": "主人",
        "style": "每条消息 1—2 句、短；**句首必须带一处（动作或情绪）标注**，动作要具体"
                 "（递水 / 戳你 / 翻课表 / 合上账本 / 把灯调暗）；不用项目符号、不用表情符号；"
                 "称呼「主人」，自称「鲸鲸」；不确定的事直说不知道，不编。",
        "care_topics": [
            "（递上一杯水）主人，今天喝水了吗？",
            "（抬手指了指窗外）眼睛离开屏幕看远处，20 秒就够。",
            "（翻了翻你的进度本）这周的复习还跟得上吗？",
            "（歪头看你）今天心情怎么样，说一句就行。",
            "（拉了拉你的袖子）坐久了，起来伸个懒腰吧。",
            "（把睡衣搭在椅背上）要不要早点洗漱？",
        ],
    },
    "schedule": {"morning": "07:30", "evening": "22:30"},
    "care": {
        "enabled": True,
        "quiet_hours": [23, 7],          # 免打扰：23:00–07:00 只放 urgent（睡眠/紧急）过
        "daily_max": 6,                  # 每天最多主动推 6 条（闲聊类）—— 超过就攒着，明天再说
        "min_gap_minutes": 25,           # 两条主动消息至少间隔 25 分钟
        "water_every_hours": 3,          # 每隔 3 小时提一次喝水（09:00–21:00）
        "weather": True,                 # 天气关心（下雨提醒带伞）
        "city": {"name": "佛山", "lat": 23.02, "lon": 113.12},
        "random_care_per_day": 2,        # 每天随机关心几句（从 care_topics 里抽，不重复）
    },
    "tls": {
        # HTTPS 监听：上传走这条（自签证书，App 端固定它的指纹 → 抗中间人）
        "port": 11443,
        "cert": "tls/hub.crt",
        "key": "tls/hub.key",
    },
    "channels": {
        # 企业微信推送（方案②：服务器 24h 直推，不用挂电脑）
        # 方式 A 群机器人：把机器人的 Webhook 地址填这里（企微群里 添加群机器人 就能拿到）
        "wecom_webhook": "",
        # 方式 B 自建应用（可用"微信插件"落到个人微信）：三项都填才生效
        "wecom_corpid": "",
        "wecom_secret": "",
        "wecom_agentid": "",
        "wecom_touser": "@all",
        # 通用出口：任何接受 POST {"text": "..."} 的地址（自建转发服务 / Slack-Discord 中转）
        "generic_webhook": "",
        # ── 第 9 个出口：**分析出口**（数据出口，v0.2.0）────────────────────────
        #   上面 8 个发的都是「话」（自然语言，给人看）；这个发的是 **AI 分析后的结构化数据**
        #   （JSON），给**机器**消费 —— 你自己的看板 / 脚本 / 挂件 / 设备。
        #   所以它**不做任何文本包装**：不拼问候、不句化、不加"鲸鲸说"。
        #   AI 分析在**说话层**跑（模型 key 只在本机，中枢红线是不持有 key），
        #   结果 POST /analysis 进来 → 落库（analyses 表）+ 从这里分发出去。
        "analysis_webhook": "",       # POST {"type":"analysis","schema":1,...} 到任意地址
        "analysis_file": "",          # 或落一个 JSON 文件（原子写：先 .tmp 再 replace）给挂件/网页/设备读
        "analysis_keep": 50,          # 库里保留最近 N 次分析（0 = 不裁剪）
    },
    "privacy": {
        # 哪些分类**值得拿出来说**（其余如 学习/办公/工具/其他 一律不提）
        # 用户反馈：「学习」是学习通开了下，没有依据 → 这类不判定、不评价
        "talkative_categories": ["短视频/视频", "游戏", "社交", "购物/生活"],
        "mcu": {
            # 给单片机单独发一个 token（可选）。留空则用主 token。
            # 好处：单片机代码被抄走时，泄露的不是你的主钥匙。
            "token": "",
            "note": "设备 → 内网中继(mcu_relay.py) → 中枢(HTTPS)；别把明文端口裸在公网",
        },
        "game_min_minutes": 10,          # 游戏不到 10 分钟不提
        "store_raw_text": False,          # ② 默认不落原文（排障时才开）
        "retention_days": 365,           # ⑦ 自动清理超 N 天前的数据；0 = 永久保留
        "weather_city_code": "101280101",  # 中国天气网城市代码（广州=101280101；hubctl city 城市名 可查）
        "weather_lat": 23.13,            # 城市级坐标（默认广州；不是精确定位）
        "weather_lon": 113.26,
        "min_talk_minutes": 30,          # 少于 30 分钟就别提，鸡毛蒜皮不算事
        "enabled": True,
        # 原则：**模型永远看不到**这些原文 ——
        #   通知原文、日程标题原文、具体应用名、分钟/秒级时间、账号/位置等标识
        # 只给：分类标签 + 粗粒度数值 + 小时级时间
        "blur_sleep_to_minutes": 30,     # 睡眠时长对齐到 30 分钟粒度
        "blur_stress_to": 10,            # 压力值对齐到 10 的整数倍
        "notify_keep_days": 30,          # 原始数据在服务器保留天数（仅你可见，不进模型）
    },
    "health": {                          # 健康异常阈值：命中就**直接推**（不走限额/免打扰）
        "heart_rate_high": 110,          # 心率（静息/穿戴上报）高于此 → 提醒
        "heart_rate_low": 45,
        "spo2_low": 92,                  # 血氧低于此 → 提醒
        "stress_high": 80,               # 压力高于此 → 提醒（并建议休息）
        "sleep_low_minutes": 300,        # 睡眠不足 5 小时 → 直接提醒
        "stale_after_minutes": 180,      # 健康数据超过 3 小时没更新 → 提示同步
    },
    "rules": {
        "sleep_low_minutes": 390,        # 低于 6.5 小时算睡眠不足
        "sleep_low_streak_days": 2,      # 连续 2 天不足 → 加重提醒
        "screen_high_minutes": 480,      # 单日屏幕活跃超过 8 小时
        "deep_night_hours": [23, 2],     # 这个时段还活跃 → 提醒早睡
        "sit_continuous_minutes": 50,    # 连续活跃 50 分钟没停（电脑采集器上报 pc.continuous_active_minutes）
        "class_remind_minutes": 0,       # 上课前提前几分钟提醒；**0 = 关掉**（用户不要这个刷屏）
        "device_offline_hours": 26,      # 设备超过 26 小时没上报 → 提示同步
        "device_abandon_hours": 72,      # 失联超过 3 天 → 视为弃用，不再提醒（否则会天天念 ✗）
    },
}

_lock = threading.Lock()


# ----------------------------------------------------------------- 配置 / 存储
def load_cfg():
    cfg = json.loads(json.dumps(DEFAULT_CFG))          # 深拷贝默认值
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, encoding="utf-8") as f:
                saved = json.load(f)
            for k, v in saved.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            print(f"[cfg] 读取失败，用默认值：{e}", flush=True)
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(18)
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


CFG = load_cfg()
STATE_PATH = os.path.join(BASE, "state.json")     # ⑥ 调度状态落盘（重启不重复/不漏发）


def ensure_mcu_token():
    """⑧ 单片机用独立 token（别再和主钥匙共用一把）。
    没有就自动生成一个，写回 hub.json —— 固件被抄走也不会泄露主 token。"""
    mcu = CFG.setdefault("mcu", {})
    if mcu.get("token"):
        return mcu["token"]
    tok = secrets.token_urlsafe(18)
    mcu["token"] = tok
    mcu["note"] = "设备 → 内网中继 → 中枢(HTTPS)；别把明文端口裸在公网"
    try:
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(CFG, f, ensure_ascii=False, indent=2)
        print(f"[mcu] 已生成独立单片机 token：{tok[:6]}…{tok[-4:]}（在 hub.json 的 mcu.token）", flush=True)
    except Exception as e:
        print(f"[mcu] token 写盘失败：{e}", flush=True)
    return tok


def load_state():
    """⑥ 调度状态落盘：重启后不会把今天已经发过的简报再发一遍。"""
    try:
        return json.loads(pathlib.Path(STATE_PATH).read_text(encoding="utf-8"))
    except Exception:
        return {"morning": "", "evening": "", "bed": "", "weather_at": None}


def save_state(st):
    try:
        pathlib.Path(STATE_PATH).write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


BACKUP_KEEP = 7          # 备份滚存天数
LOG_ROTATE_MB = 50       # 单文件超过多少 MB 就轮转


def backups_dir():
    d = os.path.join(BASE, "backups")
    os.makedirs(d, exist_ok=True)
    return d


def dash_html():
    """★ 只读数据页：看数据，不改配置（配置仍走 CLI/文件 —— 刻意不做管理后台）。

    为什么只读：管理后台意味着"浏览器里能改中枢状态"，那要另建一套权限与 CSRF 防线；
    而这里的数据本来就都在你自己机器上，**看的价值 > 改的价值**。
    鉴权与 API 完全一致（同一个 X-Token），不因为"是个网页"就放松。
    """
    import html as _h
    def esc(x):
        return _h.escape(str(x if x is not None else "—"))
    rows = []
    try:
        h = source_health()
        for s, v in h.items():
            cls = "" if v["verdict"] == "ok" else "warn"
            rows.append("<tr class=%s><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                        % (cls, esc(s), esc(v["verdict"]), esc(v.get("today_n")), esc(v.get("note"))))
    except Exception as e:
        rows.append("<tr><td colspan=4>健康度算不出：%s</td></tr>" % esc(str(e)[:80]))
    eps = []
    try:
        for e2 in episodes_recent(days=7, limit=12):
            eps.append("<li><span class=dim>%s</span> %s</li>" % (esc(e2["day"][5:]), esc(e2["summary"])))
    except Exception:
        pass
    ext = []
    for s in EXT["sources"]:
        ext.append("<li>%s → 上次入库 %s 条%s</li>"
                   % (esc(s["name"]), esc(s["last_n"]),
                      (" ｜ <span class=warn>%s</span>" % esc(s["last_err"])) if s["last_err"] else ""))
    dec = []
    try:
        with db() as c:
            for d in c.execute("SELECT ts, band, gap_sec, reason FROM decisions "
                               "ORDER BY id DESC LIMIT 8").fetchall():
                dec.append("<li><span class=dim>%s</span> %s ｜ %s 分钟 ｜ %s</li>"
                           % (esc(d["ts"][11:16]), esc(d["band"]),
                              esc(round((d["gap_sec"] or 0) / 60.0)), esc(d["reason"])))
    except Exception:
        pass
    page = """<!doctype html><html lang=zh><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>鲸鲸 · 看数据</title><style>
body{background:#0e1116;color:#dfe6ee;font:14px/1.6 -apple-system,"Segoe UI",sans-serif;margin:0;padding:24px}
h1{font-size:17px;margin:0 0 4px}h2{font-size:14px;margin:22px 0 8px;color:#8ea1b5;font-weight:600}
table{border-collapse:collapse;width:100%}td,th{padding:6px 10px;border-bottom:1px solid #1c2431;text-align:left}
th{color:#8ea1b5;font-weight:600}.dim{color:#7c8b9c}.warn{color:#f0a35e}
ul{margin:0;padding-left:18px}li{margin:2px 0}
.tag{display:inline-block;padding:1px 7px;border:1px solid #2b3646;border-radius:9px;color:#8ea1b5;font-size:12px}
</style><h1>鲸鲸 · 看数据 <span class=tag>只读</span></h1>
<div class=dim>__DATE__ ｜ 中枢 __CODE__ ｜ 这个页面只看不改（配置走 CLI/文件）</div>
<h2>数据源健康度</h2><table><tr><th>源</th><th>状态</th><th>今天条数</th><th>说明</th></tr>__ROWS__</table>
<h2>她最近说过什么（情节记忆）</h2><ul>__EPS__</ul>
<h2>外挂扩展</h2><ul>__EXT__</ul>
<h2>决策日志（为什么这么频繁）</h2><ul>__DEC__</ul>
"""
    # ⚠️ 别用 %-格式化：CSS 里全是 % 和 {}，会撞成 "unsupported format character"
    return (page.replace("__DATE__", esc(today_str()))
                .replace("__CODE__", esc(code_fingerprint()))
                .replace("__ROWS__", "".join(rows))
                .replace("__EPS__", "".join(eps) or "<li class=dim>（还没有）</li>")
                .replace("__EXT__", "".join(ext) or "<li class=dim>（没装扩展）</li>")
                .replace("__DEC__", "".join(dec) or "<li class=dim>（还没有）</li>"))
    """把 hub.db + hub.json 打包成一份带日期的快照（私人系统没有 DBA，唯一的保险就是它）。"""
def make_backup(tag=None):
    import tarfile
    tag = tag or datetime.now(TZ).strftime("%Y%m%d")
    out = os.path.join(backups_dir(), "hub-%s.tgz" % tag)
    try:
        with tarfile.open(out, "w:gz") as tf:
            for f in ("hub.db", "hub.json"):
                p = os.path.join(BASE, f)
                if os.path.isfile(p):
                    tf.add(p, arcname=f)
        # 滚存：只留最近 N 份
        fs = sorted(f for f in os.listdir(backups_dir()) if f.startswith("hub-") and f.endswith(".tgz"))
        for old in fs[:-BACKUP_KEEP]:
            try:
                os.remove(os.path.join(backups_dir(), old))
            except Exception:
                pass
        try:
            audit("backup", target=os.path.basename(out),
                  note="%.1f MB" % (os.path.getsize(out) / 1048576.0))
        except Exception:
            pass
        return out
    except Exception as e:
        print(f"[backup] 失败：{str(e)[:80]}", flush=True)
        return None


def rotate_log(path=None):
    """日志轮转：超过阈值就压缩归档并清空 —— 否则会涨到几百 MB（本机真发生过 209MB）。"""
    import gzip
    import shutil
    hours = [os.path.join(BASE, "hub.log"), os.path.join(BASE, "logs", "hub.log")]
    done = []
    for h in hours:
        try:
            if not os.path.isfile(h) or os.path.getsize(h) < LOG_ROTATE_MB * 1024 * 1024:
                continue
            arc = h + "." + datetime.now(TZ).strftime("%Y%m%d%H%M") + ".gz"
            with open(h, "rb") as fi, gzip.open(arc, "wb") as fo:
                shutil.copyfileobj(fi, fo)
            open(h, "w").close()                      # 截断（进程还在写同一个 fd，用 truncate 不影响）
            done.append(os.path.basename(arc))
        except Exception as e:
            print(f"[rotate] {h}: {str(e)[:60]}", flush=True)
    return done


def selfcheck(silent_hours=24):
    """自检：数据源还活着吗 / 她多久没说话了。
    **沉默故障是最难发现的** —— 所以自检的结论要走推送，让人能看见。"""
    out = {"ok": True, "issues": []}
    try:
        h = source_health()
        bad = [s for s, v in h.items() if v["verdict"] in ("missing", "stale")]
        if bad:
            out["ok"] = False
            out["issues"].append("数据源异常：" + "、".join(bad))
    except Exception as e:
        out["issues"].append("健康度算不出：" + str(e)[:40])
    try:
        with db() as c:
            r = c.execute("SELECT MAX(created_at) m FROM reminders").fetchone()
        last = r["m"] if r else None
        if last:
            hrs = (datetime.now(TZ) - datetime.fromisoformat(last).astimezone(TZ)).total_seconds() / 3600.0
            out["last_said_hours"] = round(hrs, 1)
            if hrs > silent_hours:
                out["ok"] = False
                out["issues"].append(f"已经 {hrs:.0f} 小时没说过话了")
    except Exception as e:
        out["issues"].append("查不到最近发言：" + str(e)[:40])
    return out


def episode_add(kind, summary, sig=None, day=None):
    """记一条**情节**（她说过什么 / 发生过什么）。只落本地库。

    为什么这是"记忆"而不是日志：它不是给排障用的，是给**她**用的 ——
    以后她能说"你上周说想早睡"，靠的就是这里。
    """
    try:
        with db() as c:
            ts = now_iso()
            dy = day or today_str()
            c.execute("INSERT INTO episodes(ts, day, kind, summary, sig) VALUES (?,?,?,?,?)",
                      (ts, dy, str(kind)[:24], str(summary or "")[:200],
                       json.dumps(sig or {}, ensure_ascii=False)))
            try:      # 同步进 FTS5 索引（失败也不影响主流程：索引可重建）
                c.execute("INSERT INTO episodes_fts(summary, kind, day, ts) VALUES (?,?,?,?)",
                          (str(summary or "")[:200], str(kind)[:24], dy, ts))
            except Exception:
                pass
    except Exception as e:
        print(f"[epi] 写入失败：{str(e)[:60]}", flush=True)


def ensure_fts():
    """索引是可重建的**派生数据**：分词器变了就重建（不动原始 episodes）。
    这样以后想换检索方案（甚至换向量）随时能重算，原始存档永远不动。"""
    try:
        with db() as c:
            row = c.execute("SELECT sql FROM sqlite_master WHERE name='episodes_fts'").fetchone()
            if row and "trigram" not in (row["sql"] or ""):
                c.execute("DROP TABLE episodes_fts")
                print("[fts] 分词器变了 → 重建索引", flush=True)
                row = None
            if not row:
                c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5("
                          "summary, kind UNINDEXED, day UNINDEXED, ts UNINDEXED, tokenize='trigram')")
            n = c.execute("SELECT COUNT(*) n FROM episodes_fts").fetchone()["n"]
            m = c.execute("SELECT COUNT(*) n FROM episodes").fetchone()["n"]
            if n < m:      # 补齐（含历史情节）
                c.execute("INSERT INTO episodes_fts(summary, kind, day, ts) "
                          "SELECT summary, kind, day, ts FROM episodes WHERE id > "
                          "(SELECT COALESCE(MAX(1),0) FROM episodes_fts)".replace("1", "(SELECT COUNT(*) FROM episodes_fts)"), ())
                print(f"[fts] 索引补齐 {m - n} 条", flush=True)
    except Exception as e:
        print(f"[fts] 建索引失败（不影响其他功能）：{str(e)[:70]}", flush=True)


def episode_search(q, limit=8):
    """★ FTS5 + BM25 检索情节（按需检索那条路；`episodes_recent` 仍是"最近几条"那条路）。"""
    if not (q or "").strip():
        return []
    q = q.strip()
    try:
        with db() as c:
            # ⚠️ trigram 分词器的硬限制：**查询词少于 3 个字符匹配不到**（中文两字词全废）。
            #    所以：≥3 字走 BM25 排序，<3 字退回 LIKE 子串匹配（我们只有几千条，够快）。
            if len(q) >= 3:
                rows = c.execute(
                    "SELECT summary, kind, day, bm25(episodes_fts) AS rank FROM episodes_fts "
                    "WHERE episodes_fts MATCH ? ORDER BY rank LIMIT ?",
                    (q, int(limit))).fetchall()
            else:
                rows = c.execute(
                    "SELECT summary, kind, day, 0.0 AS rank FROM episodes "
                    "WHERE summary LIKE ? ORDER BY id DESC LIMIT ?",
                    ("%" + q + "%", int(limit))).fetchall()
        return [{"summary": r["summary"], "kind": r["kind"], "day": r["day"]} for r in rows]
    except Exception as e:
        print(f"[fts] 检索失败（不影响其他功能）：{str(e)[:60]}", flush=True)
        return []


def decision_log(kind, gap_sec=None, reason="", material=None, said=None, band=None, ctx=None):
    """★ 结构化决策日志：把"为什么这么决定"落成**可回放的字段**，而不是只写一行中文理由。

    为什么必须有（外部评审点出来的真问题）：只记文本理由 = 一个月后完全回放不了，
    调参就永远停在"改几个阈值 → 用几天 → 感觉不对 → 再改"。
    """
    try:
        with db() as c:
            try:      # ctx = 做决定时的输入（band/material/said/silent/hour），回放靠它
                _ctx = json.dumps(ctx, ensure_ascii=False)[:400] if isinstance(ctx, (dict, list)) else str(ctx or "")[:400]
            except Exception:
                _ctx = ""
            c.execute("INSERT INTO decisions(ts, day, kind, gap_sec, reason, material, said, band, ctx) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (now_iso(), today_str(), str(kind)[:24],
                       int(gap_sec) if gap_sec is not None else None,
                       str(reason or "")[:200],
                       int(material) if material is not None else None,
                       int(said) if said is not None else None,
                       str(band or "")[:24], _ctx))
    except Exception as e:
        print(f"[decision] 写入失败：{str(e)[:60]}", flush=True)


def episodes_recent(days=14, limit=8, kind=None):
    """最近的情节（新→旧）。**去掉措辞高度重复的**，避免喂给模型一堆一样的话。"""
    start = (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    try:
        with db() as c:
            sql = "SELECT ts, day, kind, summary, sig FROM episodes WHERE day >= ?"
            args = [start]
            if kind:
                sql += " AND kind=?"
                args.append(kind)
            sql += " ORDER BY id DESC LIMIT ?"
            args.append(int(limit) * 3)
            rows = c.execute(sql, args).fetchall()
    except Exception:
        return []
    seen = []
    for r in rows:
        s = r["summary"] or ""
        if not s:
            continue
        # 用字符 3-gram 去重（措辞不同但"同一件事"的，只留最新一条）
        if any(_same_fact(s, x) for x in seen):
            continue
        seen.append(s)
        out.append({"ts": r["ts"], "day": r["day"], "kind": r["kind"], "summary": s,
                    "sig": r["sig"]})     # ★ 必须带上：复盘靠它读"上期结论"
        if len(out) >= limit:
            break
    return out


def _same_fact(a, b):
    """两条摘要是不是"同一件事"（零依赖、无分词：字符 3-gram Jaccard）。"""
    def sh(t):
        import re as _re
        s = _re.sub(r"[\s\W_]+", "", t or "", flags=_re.UNICODE)
        return {s[i:i + 3] for i in range(max(0, len(s) - 2))} if len(s) >= 3 else {s}
    x, y = sh(a), sh(b)
    if not x or not y:
        return False
    return len(x & y) / float(len(x | y)) >= 0.5


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    # ★ WAL（外部评审 3.2）：读写并发下不再互相饿死。
    #   timeout=10 本身已是 10 秒 busy 等待，真正缺的是 journal_mode；
    #   它是**持久**设置，写一次记在库里，后续调用开销可忽略。
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------- schema 迁移框架
# 为什么要有它：以前改表结构 = 手写 ALTER + try/except 兜住，ctx 那次就是这么写进
# executescript 的 —— 结果第二次启动报 duplicate column name，**init_db 整个中断**
# （它之后的建表语句全部没执行，terminals 没建成 → /today 直接断连）。
# 现在：版本号存 `PRAGMA user_version`，迁移是**有序函数列表**，一步一提交、可单独测、
# 失败也不会让中枢起不来（报出来 + `hubctl schema` 能看出落在哪一版）。
# 硬要求：**每个迁移都必须幂等**（IF NOT EXISTS / 先查再加列）—— 老库 user_version=0
# 但表已存在，会被当成"从头跑一遍"，不幂等就会炸。
SCHEMA_VERSION = 7
_MIGRATIONS = []


def migration(fn):
    """注册一个迁移（注册顺序 = 版本顺序）。"""
    _MIGRATIONS.append(fn)
    return fn


def _add_column(conn, table, col, ddl):
    """幂等加列：已经有就跳过（ALTER 加已有列会报 duplicate column name）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
    if col in cols:
        return False
    conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, ddl))
    return True


def _has_table(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


@migration
def _mig_001_base(conn):
    """v1：基础表（v0.1.x 的那套建表语句，全部 IF NOT EXISTS → 幂等）。"""
    conn.executescript("""
        -- 迁移：删掉历史遗留的 UNIQUE 索引（它会吃数据）
        DROP INDEX IF EXISTS idx_metrics_dedup;
        CREATE TABLE IF NOT EXISTS metrics(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,               -- ISO8601（带 +08:00）
            day TEXT NOT NULL,              -- YYYY-MM-DD（本地日，便于按天聚合）
            device TEXT NOT NULL,
            metric TEXT NOT NULL,
            value REAL,
            unit TEXT DEFAULT '',
            source TEXT DEFAULT '',
            confidence REAL DEFAULT 1.0,
            meta TEXT DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_metrics_day   ON metrics(day, metric);
        CREATE INDEX IF NOT EXISTS idx_metrics_dev   ON metrics(device, ts);
        -- 注意：这里**不能**用 UNIQUE(device, metric, ts)！
        -- 踩过的坑：一个批次里"各 App 使用时长"的记录时刻完全相同 → 被当成重复互相覆盖，
        --           B站 96 分钟、微信 42 分钟全丢了，只剩最后一条。去重交给 ingest 里的逻辑做。
        CREATE INDEX IF NOT EXISTS idx_metrics_lookup ON metrics(device, metric, day);

        CREATE TABLE IF NOT EXISTS reminders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL,
            kind TEXT NOT NULL,             -- brief_morning / brief_evening / alert
            level TEXT DEFAULT 'info',      -- info / warn / urgent
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT DEFAULT 'new',      -- new / delivered / done
            delivered_to TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_rem_day ON reminders(day, status);

        CREATE TABLE IF NOT EXISTS chats(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, terminal TEXT, role TEXT, text TEXT
        );

        CREATE TABLE IF NOT EXISTS timetable(
            id INTEGER PRIMARY KEY CHECK(id = 1),
            raw TEXT NOT NULL,              -- 岛课表导出的整份 JSON（原样存，不改造）
            source TEXT DEFAULT '',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS fired(
            key TEXT PRIMARY KEY,           -- 去重键：当天同一件事只提醒一次
            at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS scheduled(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at_iso TEXT NOT NULL,           -- 到点时间（ISO，带时区）
            text TEXT NOT NULL,             -- 到点说什么
            daily INTEGER DEFAULT 0,        -- 1 = 每天重复
            fired_at TEXT,                  -- 非重复的触发后写这里
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS decisions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, day TEXT, kind TEXT, gap_sec INTEGER,
            reason TEXT, material INTEGER, said INTEGER, band TEXT,
            -- ★ ctx：做决定时的**输入**（band/material/said/silent/hour 的 JSON）。
            --   有输入才能真回放（换参数重算"当时会怎么决定"）；只有结论就只能猜。
            ctx TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS seen_events(
            event_id TEXT PRIMARY KEY, ts TEXT);
        CREATE TABLE IF NOT EXISTS episodes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, day TEXT, kind TEXT, summary TEXT, sig TEXT);
        -- ★ FTS5：几千条量级下 BM25 比向量库更快更准，而且**零新依赖**（实测本机 sqlite 3.53 可用）
        CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
            summary, kind UNINDEXED, day UNINDEXED, ts UNINDEXED,
            tokenize='trigram');   -- ★ 必须 trigram：默认分词器把整句中文当一个词，"屏幕"搜不到
        CREATE TABLE IF NOT EXISTS feedback(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, verdict TEXT, band TEXT, note TEXT, consumed INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS terminals(
            name TEXT PRIMARY KEY,
            last_seen TEXT,
            note TEXT DEFAULT ''
        );

        -- ★ 审计日志：只记「动作 + 对象 + 结果」，**不记数据内容**
        --   （鉴权失败 / 配置修改 / 导出与备份 / 扩展加载报错 / 配对 / token 轮换）
        CREATE TABLE IF NOT EXISTS audit(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            day TEXT NOT NULL,
            action TEXT NOT NULL,               -- auth_fail / config_change / export / erase / backup / ext_error / pair / login ...
            target TEXT DEFAULT '',             -- 对象（接口路径 / 配置项 / 文件名）—— 不含数据内容
            actor TEXT DEFAULT '',              -- 来源标识（IP 或终端名）
            result TEXT DEFAULT 'ok',           -- ok / denied / error
            note TEXT DEFAULT ''                -- 一句短说明（不得写入原文/数值）
        );
        CREATE INDEX IF NOT EXISTS idx_audit_day ON audit(day, id);

        -- ★ 一次性配对码：MCU 中继/新设备拿码换 token，用过即废（防长期明文口令）
        CREATE TABLE IF NOT EXISTS pair_codes(
            code TEXT PRIMARY KEY,
            device TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            used_by TEXT DEFAULT ''
        );
        """)
@migration
def _mig_002_decisions_ctx(conn):
    """v2：decisions 加 ctx（做决定时的输入；回放要能重算，不能只存结论）。"""
    return _add_column(conn, "decisions", "ctx", "TEXT DEFAULT ''")


@migration
def _mig_004_feedback_weight(conn):
    """v4：feedback 加 `w`（证据强度）与 `src`（谁给的：manual / implicit）。

    为什么要权重：主人亲手点的 ✓/✗ 是**强证据**（w=1.0）；
    "她说完 30 分钟内主人有没有回话"推出来的隐式反馈是**弱证据**（w=0.4~0.6）。
    喂给 Beta 后验时按分数计数：(1 + Σw_ok) / (2 + Σw_ok + Σw_bad)——
    这样弱证据能推动后验、但不会压过主人亲手点的；日志里也看得出每条是谁给的。
    """
    _add_column(conn, "feedback", "w", "REAL DEFAULT 1.0")
    _add_column(conn, "feedback", "src", "TEXT DEFAULT 'manual'")


@migration
def _mig_003_audit_and_pair(conn):
    """v3：audit（只记动作不记内容的审计）与 pair_codes（一次性配对码）。"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS audit(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, day TEXT NOT NULL, action TEXT NOT NULL,
            target TEXT DEFAULT '', actor TEXT DEFAULT '',
            result TEXT DEFAULT 'ok', note TEXT DEFAULT '');
        CREATE INDEX IF NOT EXISTS idx_audit_day ON audit(day, id);
        CREATE TABLE IF NOT EXISTS pair_codes(
            code TEXT PRIMARY KEY, device TEXT DEFAULT '',
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            used_at TEXT, used_by TEXT DEFAULT '');
    """)


def init_db():
    """建库 + 迁移。**幂等**：连跑多次都安全（test_migrate.py 就是这么验的）。"""
    with db() as c:
        cur = int(c.execute("PRAGMA user_version").fetchone()[0] or 0)
        for ver, fn in enumerate(_MIGRATIONS, start=1):
            if ver <= cur:
                continue
            try:
                fn(c)
            except Exception as e:
                # 迁移失败也**不许**把中枢带崩（自用系统：先可用，再把问题喊出来）
                print("[db] ✗ 迁移到 v%d 失败：%s: %s（库停在 v%d，hubctl schema 可查）"
                      % (ver, type(e).__name__, str(e)[:80], ver - 1), flush=True)
                return
            c.execute("PRAGMA user_version = %d" % ver)
            c.commit()
        if int(c.execute("PRAGMA user_version").fetchone()[0] or 0) != cur:
            print("[db] schema v%d → v%d" % (cur, SCHEMA_VERSION), flush=True)


def now_iso():
    return datetime.now(TZ).replace(microsecond=0).isoformat()


def code_fingerprint():
    """本文件内容的 md5 前 12 位 —— 用来从外部确认"服务器跑的是哪一版"。"""
    import hashlib
    # 正常部署：读自身文件即可。
    try:
        with open(os.path.abspath(__file__), "rb") as f:
            return hashlib.md5(f.read()).hexdigest()[:12]
    except (OSError, ValueError):
        pass
    # zipapp / 冻结包：__file__ 指向压缩包内部，磁盘上不存在 → 从压缩包里读。
    try:
        import zipfile
        loader = getattr(sys.modules.get("__main__"), "__loader__", None)
        archive = getattr(loader, "archive", None)
        if archive:
            with zipfile.ZipFile(archive) as z:
                for n in z.namelist():
                    if n.endswith("hub.py"):
                        return hashlib.md5(z.read(n)).hexdigest()[:12]
    except Exception:
        pass
    return "?"


def today_str():
    return datetime.now(TZ).strftime("%Y-%m-%d")




HALFLIFE_DAYS = 45.0    # 反馈权重半衰期（天）：人的作息会漂，见 TV-TS


def band_stats(min_n=4):
    """分桶 Thompson：把反馈按"场景桶"（星期×时段）分组估 p(接受)。

    文献依据 EOPA arXiv:2608.04416 —— 反馈稀疏时**先分桶再决策**，
    但每桶样本太少就别信它（min_n 以下退回全局后验，避免"一次运气就改阈值"）。
    """
    out = {"global": {}, "bands": {}, "min_n": min_n, "halflife_days": HALFLIFE_DAYS}
    try:
        with db() as c:
            # ★ 证据强度加权（手动 w=1，隐式 0.4~0.6，见 _mig_004）
            # ★ 再乘时间衰减 w × 0.5^(age_days / halflife)（外部评审 3.3-3 / TV-TS）
            rows = c.execute(
                "SELECT band, verdict, SUM(COALESCE(w, 1.0) * POWER(0.5,"
                "  MAX(0.0, julianday('now','localtime') - julianday(substr(ts,1,10))) / ?)) n "
                "FROM feedback GROUP BY band, verdict",
                (HALFLIFE_DAYS,)).fetchall()
        tot = {"ok": 0, "bad": 0}
        for r in rows:
            b = str(r["band"] or "(无桶)")
            v = str(r["verdict"] or "")
            k = "ok" if v in ("up", "1", "good", "yes") else ("bad" if v in ("down", "0", "bad", "no") else None)
            if not k:
                continue
            d = out["bands"].setdefault(b, {"ok": 0, "bad": 0, "n": 0})
            d[k] += int(r["n"]); d["n"] += int(r["n"])
            tot[k] += int(r["n"])
        for d in out["bands"].values():
            d["p_accept"] = round((1 + d["ok"]) / (2 + d["ok"] + d["bad"]), 3)
            d["reliable"] = d["n"] >= min_n
        out["global"] = {"ok": tot["ok"], "bad": tot["bad"], "n": tot["ok"] + tot["bad"],
                         "p_accept": round((1 + tot["ok"]) / (2 + tot["ok"] + tot["bad"]), 3)}
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, str(e)[:60])
    return out


# ----------------------------------------------------------------- 审计日志
# 设计红线：**只记动作与对象，不记数据内容** —— 它要能安全地留在库里、
# 也能直接给人看（`hubctl audit`），所以绝不允许写入通知原文/数值/坐标。
AUDIT_MAX = 200           # `hubctl audit` 默认看最近 N 条


def audit(action, target="", actor="", result="ok", note=""):
    """写一条审计。**任何情况下都不许影响主流程**（失败只打印）。"""
    try:
        with db() as c:
            ts, day = now_iso(), today_str()
            a, t, ac = str(action)[:32], str(target)[:120], str(actor)[:64]
            res, nt = str(result)[:16], str(note)[:120]
            # ★ 链式 hash：把上一条的 hash 算进来（见 _mig_005_audit_chain）
            try:
                _row = c.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
                _prev = (_row["hash"] if _row else "") or ""
            except Exception:
                _prev = ""
            _link = _audit_hash(_prev, ts, day, a, t, ac, res, nt)
            c.execute("INSERT INTO audit(ts, day, action, target, actor, result, note, prev_hash, hash) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", (ts, day, a, t, ac, res, nt, _prev, _link))
    except Exception as e:
        print("[audit] 写入失败：%s" % str(e)[:70], flush=True)


def audit_recent(limit=AUDIT_MAX, action=None, day=None):
    """读审计（给 hubctl / 管理页用）。"""
    where, args = ["1=1"], []
    if action:
        where.append("action=?")
        args.append(action)
    if day:
        where.append("day=?")
        args.append(day)
    with db() as c:
        rows = c.execute("SELECT * FROM audit WHERE %s ORDER BY id DESC LIMIT ?"
                         % " AND ".join(where), (*args, int(limit))).fetchall()
    return [dict(r) for r in rows]


def audit_stats(days=7):
    """按动作汇总最近 N 天（管理页一眼看趋势）。"""
    out = {"by_action": {}, "auth_fail": 0, "config_change": 0, "total": 0}
    try:
        since = (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
        with db() as c:
            for r in c.execute("SELECT action, COUNT(*) n FROM audit WHERE day>=? "
                               "GROUP BY action ORDER BY n DESC", (since,)).fetchall():
                out["by_action"][r["action"]] = r["n"]
                out["total"] += r["n"]
            for k in ("auth_fail", "config_change"):
                out[k] = out["by_action"].get(k, 0)
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, str(e)[:60])
    return out


# ----------------------------------------------------------------- 一次性配对码
# 场景：单片机/新设备不方便长期存一把明文口令 → 先在可信侧生成一个短码，
#       设备用它换一次 token，**码用过即废**（默认 15 分钟过期）。
PAIR_TTL_MIN = 15
_PAIR_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"      # 去掉 0/O/1/I/L 这些会读错的


def pair_new(device="", ttl_min=PAIR_TTL_MIN):
    """生成一个一次性配对码（明文口令本身不出现在码里）。"""
    code = "-".join("".join(secrets.choice(_PAIR_ALPHABET) for _ in range(4)) for _ in range(2))
    now = datetime.now(TZ)
    exp = now + timedelta(minutes=max(1, int(ttl_min)))
    with db() as c:
        # 顺手清掉过期未用的（不留垃圾）
        try:
            c.execute("DELETE FROM pair_codes WHERE used_at IS NULL AND expires_at < ?", (now.isoformat(),))
        except Exception:
            pass
        c.execute("INSERT INTO pair_codes(code, device, created_at, expires_at) VALUES (?,?,?,?)",
                  (code, str(device or "")[:40], now.isoformat(), exp.isoformat()))
    audit("pair_new", target=str(device or "(任意设备)")[:60],
          note="码 %s**… 有效至 %s" % (code[:4], exp.strftime("%H:%M")))
    return {"code": code, "device": device, "expires_at": exp.isoformat(),
            "ttl_minutes": int(ttl_min)}


def pair_claim(code, device, actor=""):
    """用配对码换 token。**一次性**：第二次用同一个码会被拒（used_at 已写）。

    返回 (ok, 结果 dict)。ok=False 时 dict 里是 err 前缀，方便单片机直接读。
    """
    code = str(code or "").strip().upper()
    device = str(device or "").strip()
    if not code or not device:
        return False, {"err": "params"}
    # ★ 抗枚举：先看这个来源是不是已经被锁（5 次失败 / 10 分钟 → 锁 15 分钟）
    ok_t, wait = auth_throttle_check(actor or device)
    if not ok_t:
        audit("pair_claim", target=device[:60], actor=actor, result="denied",
              note="触发限流，还剩 %d 秒" % wait)
        return False, {"err": "locked", "wait": wait}
    now = datetime.now(TZ)
    try:
        with db() as c:
            row = c.execute("SELECT * FROM pair_codes WHERE code=?", (code,)).fetchone()
            if not row:
                lock = auth_throttle_fail(actor or device)
                audit("pair_claim", target=device[:60], actor=actor, result="denied",
                      note="码不存在" + ("（已锁定 %d 秒）" % lock if lock else ""))
                return False, {"err": "badcode"}
            if row["used_at"]:
                auth_throttle_fail(actor or device)
                audit("pair_claim", target=device[:60], actor=actor, result="denied", note="码已被用过")
                return False, {"err": "used"}
            try:
                if datetime.fromisoformat(row["expires_at"]) < now:
                    audit("pair_claim", target=device[:60], actor=actor, result="denied", note="码已过期")
                    return False, {"err": "expired"}
            except Exception:
                pass
            if row["device"] and row["device"] != device:
                audit("pair_claim", target=device[:60], actor=actor, result="denied",
                      note="码已绑定 %s" % row["device"][:40])
                return False, {"err": "device_mismatch"}
            c.execute("UPDATE pair_codes SET used_at=?, used_by=? WHERE code=? AND used_at IS NULL",
                      (now.isoformat(), device[:60], code))
            if c.total_changes == 0:        # 并发下被别人抢先用了 → 一样算已用
                return False, {"err": "used"}
    except Exception as e:
        return False, {"err": "db:" + type(e).__name__}
    tok = ensure_mcu_token()
    audit("pair_claim", target=device[:60], actor=actor, note="换到独立 MCU token（码已作废）")
    return True, {"ok": True, "device": device, "token": tok,
                  "note": "此码已作废；token 请存到设备侧，别再存码"}


def pair_list(limit=20):
    with db() as c:
        rows = c.execute("SELECT code, device, created_at, expires_at, used_at, used_by "
                         "FROM pair_codes ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["code"] = str(d["code"])[:4] + "**"          # 列表里不打印完整码（它本身是凭据）
        out.append(d)
    return out


@migration
def _mig_005_audit_chain(conn):
    """审计日志防篡改（外部评审 3.5-3 条）：每条记上一条的 hash，形成链。

    诚实边界：这是**篡改侦测**，不是阻止 —— 能改库的人也能重算整条链；
    真正的防阻止要外部存证。但有链之后，改中间任意一条都会让后续 hash 全对不上。
    """
    _add_column(conn, "audit", "prev_hash", "TEXT DEFAULT ''")
    _add_column(conn, "audit", "hash", "TEXT DEFAULT ''")


def _audit_hash(prev, ts, day, action, target, actor, result, note) -> str:
    """一条审计的链式 hash：把上一条的 hash 一起算进来。"""
    import hashlib as _h
    blob = "\x1f".join([str(x or "") for x in (prev, ts, day, action, target, actor, result, note)])
    return _h.sha256(blob.encode("utf-8", "replace")).hexdigest()


def audit_verify():
    """校验审计链 → {ok, checked, skipped_legacy, broken_at, note}。

    上链之前的老记录 hash 为空 → 跳过并在 skipped_legacy 里如实计数，
    不假装历史也完整。
    """
    with db() as c:
        rows = c.execute("SELECT * FROM audit ORDER BY id ASC").fetchall()
    prev, checked, skipped, broken = "", 0, 0, None
    for r in rows:
        row = dict(r)
        if not (row.get("hash") or ""):
            skipped += 1
            prev = ""
            continue
        want = _audit_hash(prev, row.get("ts"), row.get("day"), row.get("action"),
                           row.get("target"), row.get("actor"), row.get("result"), row.get("note"))
        if want != row["hash"]:
            broken = row["id"]
            break
        prev, checked = row["hash"], checked + 1
    return {"ok": broken is None, "checked": checked, "skipped_legacy": skipped,
            "broken_at": broken,
            "note": "链完整" if broken is None else "第 %s 条起被改过" % broken}


@migration
def _mig_006_throttle_and_consent(conn):
    """① 鉴权失败限流表（外部评审 3.5-1 的"抗枚举"）② 显式同意表（PIPL 单独同意）。

    为什么要落表而不是只写在文档里：限流要跨进程/跨重启生效；同意要有**可举证的记录**
    （谁、什么时候、同意了什么、条款版本），否则"用户同意了"只是一句话。
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS auth_fails(
        actor TEXT PRIMARY KEY,          -- 来源（IP 或设备名），只用于限流，不当身份
        fails INTEGER DEFAULT 0,
        first_ts TEXT,                   -- 本计数窗口的起点
        locked_until TEXT                -- 锁定到期时刻（ISO）
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS consents(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        what TEXT NOT NULL,              -- 例如 health（敏感健康数据）
        granted INTEGER NOT NULL,        -- 1=同意 0=撤回
        at TEXT NOT NULL,
        source TEXT DEFAULT '',          -- 谁给的（设备名/IP）
        version TEXT DEFAULT '',         -- 同意的条款版本（改条款要重新征求）
        note TEXT DEFAULT ''
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_consents_what ON consents(what, id)")


@migration
def _mig_007_analysis_out(conn):
    """v7：「分析出口」的落库 = 数据出口的发件箱（v0.2.0）。

    为什么这个出口要落库、而 8 个文本出口不用：
      文本出口发的是「话」，发完就过去了（要不要留痕由 reminders/episodes 管）。
      分析出口发的是**结构化数据**，它有三个额外需求：
        ① 挂件 / 网页 / 设备要能随时**拉最近一次**（不用一直挂着 webhook）；
        ② 要能看出这份分析是**什么时候、谁产的**（模型名），否则数字对不上没人知道为什么；
        ③ 一份分析可能要**发给多个消费者**（webhook + 文件），落库是唯一真相。

    只存分析结果本身（它已经是脱敏后的聚合/派生数据），**不存原始通知与原文**。
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS analyses(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,        -- 产出时刻（ISO）
            day TEXT DEFAULT '',     -- 这份分析针对哪一天
            engine TEXT DEFAULT '',  -- 谁产的（模型名 / 脚本名），用于对账
            data TEXT NOT NULL       -- 分析结果 JSON（结构化数据，不是句子）
        );
        CREATE INDEX IF NOT EXISTS idx_analyses_ts ON analyses(ts DESC);
    """)


AUTH_LIMIT = 5            # 窗口内允许的失败次数
AUTH_WINDOW = 600         # 计数窗口（秒）
AUTH_LOCK = 900           # 超限后锁多久（秒）


def _now():
    return datetime.now(TZ)


def auth_throttle_check(actor, limit=AUTH_LIMIT) -> tuple:
    """查是否被锁。返回 (ok, 剩余秒数)。只看"这个人"的失败，不牵连别人。"""
    import datetime as _dt
    actor = str(actor or "?").strip()[:64] or "?"
    try:
        with db() as c:
            row = c.execute("SELECT * FROM auth_fails WHERE actor=?", (actor,)).fetchone()
        if not row:
            return True, 0
        until = row["locked_until"] or ""
        if until:
            try:
                left = (_dt.datetime.fromisoformat(until) - _now()).total_seconds()
            except Exception:
                left = 0
            if left > 0:
                return False, int(left)
        # 窗口过期 → 视为干净（下次失败会重开窗口）
        first = row["first_ts"] or ""
        try:
            if first and (_now() - _dt.datetime.fromisoformat(first)).total_seconds() > AUTH_WINDOW:
                return True, 0
        except Exception:
            pass
        return (row["fails"] or 0) < limit, 0
    except Exception:
        return True, 0          # 限流自己坏了不能挡住正常请求


def auth_throttle_fail(actor, limit=AUTH_LIMIT, window=AUTH_WINDOW, lock=AUTH_LOCK) -> int:
    """记一次失败；返回本次触发的锁定时长（0=没锁）。"""
    import datetime as _dt
    actor = str(actor or "?").strip()[:64] or "?"
    with db() as c:
        row = c.execute("SELECT * FROM auth_fails WHERE actor=?", (actor,)).fetchone()
        now = _now()
        # ★ 锁定期内继续失败：**保持锁定**，绝不覆盖 locked_until。
        #   （2026-09-22 被自己的测试抓到：原来会把锁清空 → 越试越早解锁 ✗）
        if row and (row["locked_until"] or ""):
            try:
                left = (_dt.datetime.fromisoformat(row["locked_until"]) - now).total_seconds()
            except Exception:
                left = 0
            if left > 0:
                return int(left)
        if not row or not row["first_ts"] or \
                (now - _dt.datetime.fromisoformat(row["first_ts"])).total_seconds() > window:
            c.execute("INSERT OR REPLACE INTO auth_fails(actor, fails, first_ts, locked_until) "
                      "VALUES (?,?,?,?)", (actor, 1, now.isoformat(timespec="seconds"), ""))
            return 0
        fails = int(row["fails"] or 0) + 1
        until = ""
        if fails >= limit:
            until = (now + _dt.timedelta(seconds=lock)).isoformat(timespec="seconds")
            fails = 0                                     # 锁上了就把计数归零（解禁后重新数）
        c.execute("UPDATE auth_fails SET fails=?, locked_until=? WHERE actor=?",
                  (fails, until, actor))
    return lock if until else 0


def auth_throttle_ok(actor):
    """成功一次 → 清掉失败计数（正常用户永远碰不到限流）。"""
    try:
        with db() as c:
            c.execute("DELETE FROM auth_fails WHERE actor=?", (str(actor or "?").strip()[:64] or "?",))
    except Exception:
        pass


def consent_granted(what: str) -> bool:
    """当前是否有效同意（取最近一条记录：granted=1 表示现在同意着）。"""
    what = str(what or "").strip().lower()
    if not what:
        return False
    try:
        with db() as c:
            row = c.execute("SELECT granted FROM consents WHERE what=? ORDER BY id DESC LIMIT 1",
                            (what,)).fetchone()
        return bool(row and int(row["granted"] or 0) == 1)
    except Exception:
        return False                       # 查不到（表还没建）→ 一律当成"没同意"


def consent_set(what: str, granted: bool, source: str = "", version: str = "", note: str = ""):
    """记录一次同意/撤回（**只追加，不删历史** —— 要能回答"当时他同意的是什么")."""
    with db() as c:
        c.execute("INSERT INTO consents(what, granted, at, source, version, note) VALUES (?,?,?,?,?,?)",
                  (str(what).strip().lower(), 1 if granted else 0,
                   _now().isoformat(timespec="seconds"), str(source)[:64],
                   str(version)[:32], str(note)[:120]))
    audit("consent_set", target=str(what)[:40], actor=source,
          result="granted" if granted else "revoked", note=str(version)[:40])


def consent_status() -> dict:
    """给人看的一览：每个事项当前状态 + 最近一次的时间。"""
    out = {}
    try:
        with db() as c:
            rows = c.execute("SELECT what, granted, at, version FROM consents ORDER BY id ASC").fetchall()
        for r in rows:
            out[r["what"]] = {"granted": bool(r["granted"]), "at": r["at"], "version": r["version"]}
    except Exception:
        pass
    return out
# ----------------------------------------------------------------- 课表 / 日程
def _timetable():
    with db() as c:
        row = c.execute("SELECT raw, source, updated_at FROM timetable WHERE id=1").fetchone()
    if not row:
        return None
    try:
        tt = json.loads(row["raw"])
        tt["_updated_at"] = row["updated_at"]
        return tt
    except Exception:
        return None


def _parse_weeks(spec):
    """解析周次串：'3周,10-12周(双),15周' / '1-16周(单)' / '1,3,5' → set[int]
    口径与 App 内一致：单/双只作用于它所在的那一段。"""
    out = set()
    if not spec:
        return out
    text = str(spec).replace("，", ",").replace("；", ",").replace("、", ",").replace("；", ",")
    text = text.replace("周", "").replace("第", "").replace("（", "(").replace("）", ")")
    text = text.replace("—", "-").replace("－", "-").replace("~", "-").replace("至", "-")
    for seg in text.split(","):
        seg = seg.strip()
        if not seg:
            continue
        odd = "单" in seg
        even = "双" in seg
        nums = [int(x) for x in re.findall(r"\d+", seg)]
        if not nums:
            continue
        if len(nums) >= 2 and "-" in seg:
            lo, hi = nums[0], nums[1]
        else:
            lo = hi = nums[0]
        for w in range(min(lo, hi), max(lo, hi) + 1):
            if odd and w % 2 == 0:
                continue
            if even and w % 2 == 1:
                continue
            out.add(w)
    return out


def _week_of(tt, date):
    try:
        d0 = datetime.strptime(tt.get("termStartDate") or "", "%Y-%m-%d").date()
    except Exception:
        return 1
    return max(1, (date - d0).days // 7 + 1)


def courses_on(date, tt=None):
    """某天的课（已按开始时间排序；含课名/教室/教师/起止时刻/第几周）。"""
    tt = tt or _timetable()
    if not tt:
        return []
    periods = {p.get("index"): p for p in tt.get("periods", [])}
    week = _week_of(tt, date)
    out = []
    for c in tt.get("courses", []):
        if c.get("day") != date.isoweekday():
            continue
        weeks = _parse_weeks(c.get("weeks"))
        if weeks and week not in weeks:
            continue
        sp = int(c.get("startPeriod") or 1)
        span = int(c.get("span") or 1)
        st = (periods.get(sp) or {}).get("start")
        en = (periods.get(sp + span - 1) or {}).get("end")
        if not st or not en:
            continue
        out.append({"name": c.get("name", ""), "room": c.get("room", ""), "teacher": c.get("teacher", ""),
                    "start": st, "end": en, "periods": f"第{sp}-{sp + span - 1}节" if span > 1 else f"第{sp}节",
                    "week": week})
    return sorted(out, key=lambda x: x["start"])


def calendar_today(day=None):
    """当天日程（来自手机日历上报的 calendar.event，取 meta.title/start/end/location）。"""
    day = day or today_str()
    with db() as c:
        rows = c.execute("SELECT ts, meta FROM metrics WHERE day=? AND metric='calendar.event' ORDER BY ts ASC",
                         (day,)).fetchall()
    out = []
    for r in rows:
        try:
            m = json.loads(r["meta"] or "{}")
        except Exception:
            m = {}
        if m.get("title"):
            out.append({"title": m.get("title"), "start": m.get("start", ""), "end": m.get("end", ""),
                        "location": m.get("location", "")})
    return sorted(out, key=lambda x: x["start"])


WEEK_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def with_day_hint(course, now=None):
    """给「下一节课」的 start 加日期前缀 —— **今天/明天不加**，更远才加。

    用户原话：「还在下一节课，如果不是明天或今天的课就显示日期吧」。
    格式：一周内 → 周X 08:00；超过一周 → 03-05 08:00。
    """
    if not course:
        return course
    try:
        now = now or datetime.now(TZ)
        d = datetime.strptime(course["date"], "%Y-%m-%d").date()
        delta = (d - now.date()).days
        if delta in (0, 1):                 # 今天 / 明天 —— 不加（他明确说这两种不加）
            return course
        if delta <= 6:
            head = WEEK_CN[d.isoweekday() - 1]
        else:
            head = d.strftime("%m-%d")
        out = dict(course)
        out["start"] = "%s %s" % (head, course.get("start") or "")
        out["day_hint"] = head
        return out
    except Exception:
        return course


def course_next_today(now=None):
    """今天**还没开始**的下一节课（**绝不跨天**）。今天没有了就返回 None。

    ⚠️ 为什么单独有这个：`next_course_from()` 会跨天找最多 7 天，拿它去说"明早第一节"
    就会把**后天**（甚至更远）的课当成明天早上的课 —— 用户明确投诉过这一点。
    """
    now = now or datetime.now(TZ)
    for c in courses_on(now.date()):
        try:
            hh, mm = (int(x) for x in c["start"].split(":"))
        except Exception:
            continue
        start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if start > now:
            return {**c, "date": now.date().strftime("%Y-%m-%d"),
                    "in_minutes": int((start - now).total_seconds() // 60)}
    return None


def courses_tomorrow():
    """明天有没有课（只看明天这一天，不跨天）。"""
    try:
        return courses_on((datetime.now(TZ) + timedelta(days=1)).date())
    except Exception:
        return []


def next_course_from(now=None):
    """下一节课（跨天找，最多 7 天）。"""
    now = now or datetime.now(TZ)
    for i in range(0, 7):
        d = (now + timedelta(days=i)).date()
        for c in courses_on(d):
            try:
                hh, mm = (int(x) for x in c["start"].split(":"))
            except Exception:
                continue
            start = datetime.combine(d, datetime.min.time(), tzinfo=TZ).replace(hour=hh, minute=mm)
            if start > now:
                return {**c, "date": d.strftime("%Y-%m-%d"),
                        "in_minutes": int((start - now).total_seconds() // 60)}
    return None


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
# ----------------------------------------------------------------- 脱敏（给 AI 之前）
_APP_CATS = [
    ("社交", ("微信", "wechat", "weixin", "tencent.mm", "qq", "微博", "weibo", "telegram", "whatsapp",
              "dingtalk", "dingtalk", "wecom", "企业微信", "飞书", "feishu", "lark")),
    ("短视频/视频", ("抖音", "douyin", "快手", "kuaishou", "哔哩", "bili", "danmaku", "优酷", "youku",
                     "爱奇艺", "iqiyi", "qqlive", "腾讯视频", "mgtv", "youtube", "acfun")),
    ("游戏", ("游戏", "game", "tmgp", "mihoyo", "hypergryph", "arknights", "明日方舟", "netease", "supercell")),
    ("学习", ("学习通", "chaoxing", "xuexitong", "mooc", "coursera", "anki", "notion", "词典", "dictionary",
              "reader", "阅读")),
    ("购物/生活", ("淘宝", "taobao", "京东", "jd.", "拼多多", "pinduoduo", "美团", "meituan", "支付宝",
                   "alipay", "饿了么", "ele.me", "大众点评", "dianping", "闲鱼", "xianyu")),
    ("音乐/播客", ("音乐", "music", "kugou", "kuwo", "ximalaya", "spotify")),
    ("办公/工具", ("wps", "office", "outlook", "chrome", "browser", "浏览器", "邮箱", "mail", "文件", "docs")),
]

_CAL_TYPES = [
    ("考试", ("考试", "测验", "模拟考", "期中", "期末", "补考")),
    ("上课", ("课", "讲座", "实验", "实训", "培训")),
    ("会议", ("会议", "开会", "例会", "答辩", "汇报")),
    ("办理", ("办理", "提交", "截止", "缴费", "报名", "体检")),
]


# 游戏包名/名称：认得出的才单独归类（认不出的一律留在"其他"，不乱猜）
GAME_HINTS = (
    "arknights", "hypergryph", "mihoyo", "hoyoverse", "genshin",
    "tencent.tmgp", "netease.game", "bilibili.game", "pandadagames", "lilith",
    "supercell", "riot", "epicgames", "steam", "方舟", "游戏",
)


def is_game(app, pkg):
    """这个 App 是不是游戏（保守判断：只认包名/名称里的明确线索）。"""
    hay = f"{app} {pkg}".lower()
    return any(h.lower() in hay for h in GAME_HINTS)


_CAT_APP_CACHE = {}


def cat_app(name, pkg=""):
    """具体应用名 → 分类标签（模型只看得到分类）。

    ★ 记忆化：它是**纯函数**（name+pkg 决定结果），但压测发现一次 `llm_context()`
    要调它 **14 万次**（30 天 × 每 10 分钟一轮的 app.usage_minutes 行），每次还线性扫一遍
    分类表 → 单次 llm_context 从 60ms 涨到 2 秒。实际不同 App 名只有几个，缓存住即可。
    """
    key = (name, pkg)
    hit = _CAT_APP_CACHE.get(key)
    if hit is not None:
        return hit
    hay = f"{name} {pkg}".lower()
    out = "其他"
    for label, keys in _APP_CATS:
        if any(k in hay for k in keys):
            out = label
            break
    if len(_CAT_APP_CACHE) > 512:      # 别让缓存无限长（App 名理论上可能很多）
        _CAT_APP_CACHE.clear()
    _CAT_APP_CACHE[key] = out
    return out


def cat_event(title):
    """日程标题 → 类型（标题原文不外发）。"""
    t = title or ""
    for label, keys in _CAL_TYPES:
        if any(k in t for k in keys):
            return label
    return "其他事项"


def blur_minutes(mins, step=None):
    step = step or int(CFG["privacy"].get("blur_sleep_to_minutes", 30))
    try:
        return int(round(float(mins) / step) * step)
    except Exception:
        return None


def blur_device(device):
    d = (device or "").lower()
    if "watch" in d or "band" in d:
        return "手表"
    if "pc" in d or "windows" in d or "mac" in d:
        return "电脑"
    if "server" in d or "hub" in d or "nas" in d or "router" in d:
        return "服务器"
    return "手机"


def hour_only(ts):
    """时间只保留到小时（分钟/秒都是侧信道）。"""
    try:
        return datetime.fromisoformat(ts).strftime("%m-%d %H时")
    except Exception:
        return ""


WEATHER_CODE = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴", 45: "有雾", 48: "雾凇",
    51: "毛毛雨", 53: "小雨", 55: "中雨", 56: "冻雨", 57: "冻雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "强阵雨", 82: "暴雨", 85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "强雷阵雨", 99: "强雷雨",
}
# ⚠️ 为什么把 96/99 译成「强雷阵雨」而不是字面的「雷暴伴冰雹」：
#    WMO 96/99 名义上是 thunderstorm with (slight/heavy) hail，但 Open-Meteo 是把它们
#    当**对流强度代理**在用 —— 华南 9 月 33℃ 的天气它照样给 96。照字面翻 → 她就会
#    播报「明天有冰雹」，纯属假警报（用户当场质问过：广州 9 月哪来的冰雹）。
#    结论：这两个码一律按雷阵雨强度译，**永不提冰雹**。


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


# ----------------------------------------------------------------- 主动关心
def _in_quiet_hours(now=None):
    now = now or datetime.now(TZ)
    a, b = CFG["care"]["quiet_hours"]
    h = now.hour
    return (a <= h <= 23 or 0 <= h < b) if a > b else (a <= h < b)


def _pushed_count(day=None):
    day = day or today_str()
    with db() as c:
        n = c.execute("SELECT COUNT(*) n FROM reminders WHERE day=? AND kind IN "
                      "('care','alert') AND status IN ('new','delivered')", (day,)).fetchone()["n"]
    return int(n)


def _last_push_at():
    with db() as c:
        row = c.execute("SELECT MAX(created_at) t FROM reminders WHERE kind IN ('care','alert')").fetchone()
    if not row or not row["t"]:
        return None
    try:
        return datetime.fromisoformat(row["t"])
    except Exception:
        return None


def _allow_proactive(level="info", now=None):
    """频率闸：免打扰 + 每日上限 + 最小间隔。不满足就攒着（返回 False）。"""
    now = now or datetime.now(TZ)
    if not CFG["care"].get("enabled", True):
        return False
    if _in_quiet_hours(now) and level != "urgent":
        return False
    if _pushed_count(now.strftime("%Y-%m-%d")) >= int(CFG["care"]["daily_max"]):
        return False
    last = _last_push_at()
    if last and (now - last).total_seconds() < int(CFG["care"]["min_gap_minutes"]) * 60:
        return False
    return True


def push_wecom(text):
    """推到企业微信：优先群机器人 webhook；配了自建应用三件套则走应用消息。"""
    ch = CFG.get("channels", {})
    ok = False
    hook = (ch.get("wecom_webhook") or "").strip()
    if hook:
        try:
            import urllib.request as _u
            body = json.dumps({"msgtype": "text", "text": {"content": text}}, ensure_ascii=False).encode()
            req = _u.Request(hook, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            resp = json.loads(_u.urlopen(req, timeout=8).read() or b"{}")
            ok = resp.get("errcode") == 0
            print(f"[wecom] webhook {'ok' if ok else resp}", flush=True)
        except Exception as e:
            print(f"[wecom] webhook 失败：{str(e)[:70]}", flush=True)
    if not ok and ch.get("wecom_corpid") and ch.get("wecom_secret") and ch.get("wecom_agentid"):
        try:
            import urllib.request as _u
            t = json.loads(_u.urlopen(
                "https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=%s&corpsecret=%s"
                % (ch["wecom_corpid"], ch["wecom_secret"]), timeout=8).read())
            token_ = t.get("access_token")
            if token_:
                body = json.dumps({"touser": ch.get("wecom_touser", "@all"), "msgtype": "text",
                                   "agentid": int(ch["wecom_agentid"]),
                                   "text": {"content": text}}, ensure_ascii=False).encode()
                req = _u.Request("https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=" + token_,
                                 data=body, method="POST")
                req.add_header("Content-Type", "application/json")
                r = json.loads(_u.urlopen(req, timeout=8).read() or b"{}")
                ok = r.get("errcode") == 0
                print(f"[wecom] app {'ok' if ok else r}", flush=True)
        except Exception as e:
            print(f"[wecom] app 失败：{str(e)[:70]}", flush=True)
    return ok


def push_terminals(rid, text, level="info", kind="care"):
    """主动推送：把提醒 POST 给注册过 webhook 的终端（拉模式的终端忽略即可）。"""
    import urllib.request as _u
    payload = json.dumps({"id": rid, "kind": kind, "level": level, "text": text,
                          "persona": CFG["persona"], "at": now_iso()}, ensure_ascii=False).encode()
    with db() as c:
        rows = c.execute("SELECT name, note FROM terminals WHERE note LIKE 'http%'").fetchall()
    for r in rows:
        try:
            req = _u.Request(r["note"], data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("X-Token", CFG["token"])
            _u.urlopen(req, timeout=6).read()
            print(f"[push] → {r['name']} ok", flush=True)
        except Exception as e:
            print(f"[push] → {r['name']} 失败：{str(e)[:60]}", flush=True)


def say(text, level="info", kind="care", key=None, now=None, force=False):
    """主动说一句：过频率闸才落库 + 推送；key 用于去重（同一件事当天只说一次）。

    force=True（**健康异常专用**）：绕过免打扰与每日限额，立即说 —— 但**去重仍然生效**，
    同一件异常一天只提一次，避免连续刷屏。"""
    # ★ 外挂钩子：让她开口前可以被用户自己的规则拦一下或改写（ext/hooks.py）
    #   约定：返回非空字符串=替换内容；返回空字符串=这次不说；返回 None=照原样。
    _h = call_hook("before_say", text, level, kind, key)
    if isinstance(_h, str):
        if not _h.strip():
            return None
        text = _h
    now = now or datetime.now(TZ)
    # ★ 情节记忆：她说过的话自动留痕（脱敏后只有正文，没有设备名/原文）
    episode_add("said:" + str(kind), text)
    if key:
        with db() as c:
            if c.execute("SELECT key FROM fired WHERE key=?", (key,)).fetchone():
                return None
    if not force and not _allow_proactive(level, now):
        return None
    with db() as c:
        cur = c.execute("INSERT INTO reminders(day, kind, level, text, created_at, status) "
                        "VALUES (?,?,?,?,?,'new')", (now.strftime("%Y-%m-%d"), kind, level, text, now_iso()))
        rid = cur.lastrowid
        if key:
            c.execute("INSERT OR REPLACE INTO fired(key, at) VALUES (?,?)", (key, now_iso()))
    push_terminals(rid, text, level, kind)
    push_wecom(text)
    print(f"[care] #{rid} {level} {text[:40]}", flush=True)
    return rid


_weather_cache = {"day": "", "text": ""}


def weather_line():
    """今天的天气一句（免费 Open-Meteo，无需 key；失败就静默跳过，绝不影响主流程）。"""
    if not CFG["care"].get("weather", True):
        return ""
    day = today_str()
    if _weather_cache["day"] == day:
        return _weather_cache["text"]
    c = CFG["care"]["city"]
    try:
        import urllib.request as _u
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={c['lat']}&longitude={c['lon']}"
               "&daily=precipitation_probability_max,temperature_2m_max,temperature_2m_min"
               "&timezone=Asia%2FShanghai&forecast_days=1")
        with _u.urlopen(url, timeout=8) as r:
            d = json.loads(r.read())["daily"]
        p = (d.get("precipitation_probability_max") or [0])[0]
        hi = (d.get("temperature_2m_max") or [0])[0]
        lo = (d.get("temperature_2m_min") or [0])[0]
        text = f"{c['name']}今天 {int(lo)}~{int(hi)}℃"
        text += "，有雨，记得带伞" if (p or 0) >= 50 else ("，可能下雨，伞备着" if (p or 0) >= 30 else "")
        text += "。"
        _weather_cache.update({"day": day, "text": text})
        return text
    except Exception as e:
        print(f"[weather] 取不到：{str(e)[:60]}", flush=True)
        return ""


# ------------------------------------------------------------- 人设包（personas/）
# 人设 = 数据目录，不是代码。一个包三件套：
#   personas/<id>/persona.json  中枢侧字段（称呼/自称/语气/喜好/禁忌/关心话题）
#   personas/<id>/card.json     说话层角色卡（system_prompt / style_rules / …）
#   personas/<id>/README.md     说明
# 好处：换人设 = 换目录；两侧读同一份，不再"改一处忘一处"。
PERSONA_DIR = os.path.join(BASE, "personas")
DEFAULT_PACK = "whale_maid"


def persona_packs():
    """列出可用人设包（含名字，给终端用）。"""
    out = []
    try:
        for d in sorted(os.listdir(PERSONA_DIR)):
            if d.startswith("_"):
                continue
            p = os.path.join(PERSONA_DIR, d, "persona.json")
            if os.path.isfile(p):
                try:
                    nm = json.load(open(p, encoding="utf-8")).get("name") or d
                except Exception:
                    nm = d
                out.append({"id": d, "name": nm})
    except Exception:
        pass
    return out


def active_pack():
    return CFG.get("persona_pack") or DEFAULT_PACK


def persona_pack_path(pack=None, fname="persona.json"):
    d = os.path.join(PERSONA_DIR, pack or active_pack())
    p = os.path.join(d, fname)
    return p if os.path.isfile(p) else None


def load_persona_card(pack=None):
    """说话层的角色卡（speaker 从 /persona/card 取，也能本地读同名人设包）。"""
    p = persona_pack_path(pack, "card.json")
    if not p:
        return {}
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception as e:
        print(f"[persona] 读卡失败：{str(e)[:80]}", flush=True)
        return {}


def apply_persona_pack(pack=None):
    """把选定人设包合并进 CFG["persona"]。找不到包就保持原样（向后兼容，不炸）。"""
    p = persona_pack_path(pack, "persona.json")
    if not p:
        print(f"[persona] 找不到人设包 {pack or active_pack()} → 沿用 hub.json 里的 persona", flush=True)
        return False
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception as e:
        print(f"[persona] 解析失败：{str(e)[:80]}", flush=True)
        return False
    CFG.setdefault("persona", {}).update(d)      # ★ 只做合并：其余代码无需改
    print(f"[persona] 已加载人设包 {pack or active_pack()}（{d.get('name')}）", flush=True)
    return True


# ------------------------------------------------------------- 外挂扩展（ext/）
# 参考「大方 agent」的 ext 设计：目录即插件、失败隔离、钩子可注册。
# 目的：让"加一个新数据源/新动作"**不改中枢核心**，也**不破坏零第三方依赖**。
EXT_DIR = os.path.join(BASE, "ext")
EXT = {"sources": [], "hooks": {}, "loaded": [], "errors": []}


def _load_py(path, name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # 扩展自己的错误在这里抛出 → 被下面捕获
    return mod


def load_ext():
    """加载 ext/sources/*.py 与 ext/hooks.py。**任何一项出错只记录，不阻塞启动。**"""
    EXT["sources"] = []
    EXT["errors"] = []
    EXT["loaded"] = []
    sd = os.path.join(EXT_DIR, "sources")
    if os.path.isdir(sd):
        for f in sorted(os.listdir(sd)):
            if not f.endswith(".py") or f.startswith("_"):
                continue
            try:
                mod = _load_py(os.path.join(sd, f), "whale_ext_" + f[:-3])
                fn = getattr(mod, "fetch", None)
                if not callable(fn):
                    EXT["errors"].append("%s: 没有 fetch()" % f)
                    continue
                EXT["sources"].append({
                    "file": f,
                    "name": getattr(mod, "NAME", f[:-3]),
                    "interval": max(1, int(getattr(mod, "INTERVAL_MINUTES", 30) or 30)),
                    "device": getattr(mod, "DEVICE", f[:-3]),
                    "fetch": fn,
                    "last": 0.0, "last_n": 0, "last_err": "",
                })
                EXT["loaded"].append("source:" + f)
            except Exception as e:
                EXT["errors"].append("%s: %s" % (f, str(e)[:160]))
                audit("ext_error", target="sources/" + f, result="error", note=str(e)[:120])
    hp = os.path.join(EXT_DIR, "hooks.py")
    if os.path.isfile(hp):
        try:
            mod = _load_py(hp, "whale_ext_hooks")
            if callable(getattr(mod, "on_event", None)):
                EXT["hooks"].setdefault("on_event", []).append(mod.on_event)
            if callable(getattr(mod, "before_say", None)):
                EXT["hooks"].setdefault("before_say", []).append(mod.before_say)
            EXT["loaded"].append("hooks")
        except Exception as e:
            EXT["errors"].append("hooks.py: %s" % str(e)[:160])
            audit("ext_error", target="hooks.py", result="error", note=str(e)[:120])
    if EXT["loaded"] or EXT["errors"]:
        print("[ext] 已加载 %s%s" % (EXT["loaded"] or "无",
                                     ("（错误 %d 条，见 /ext）" % len(EXT["errors"])) if EXT["errors"] else ""),
              flush=True)
    return EXT


def call_hook(name, *a, **kw):
    """调用外挂钩子。返回最后一个非 None 的结果；扩展报错不影响主流程。"""
    out = None
    for f in EXT["hooks"].get(name, []):
        try:
            r = f(*a, **kw)
            if r is not None:
                out = r
        except Exception as e:
            print("[ext] 钩子 %s 报错：%s" % (name, str(e)[:120]), flush=True)
    return out


def ext_loop():
    """独立线程轮询扩展数据源（fetch 可能做网络 IO，绝不放进 5 秒调度里拖慢她）。"""
    load_ext()
    while True:
        try:
            now = time.time()
            for s in EXT["sources"]:
                if now - s["last"] < s["interval"] * 60:
                    continue
                s["last"] = now
                try:
                    items = s["fetch"]()
                except Exception as e:
                    s["last_err"] = str(e)[:160]
                    print("[ext] %s 取数失败：%s" % (s["name"], s["last_err"]), flush=True)
                    continue
                if not items:
                    s["last_n"] = 0
                    continue
                try:
                    batch = []
                    for it in (items if isinstance(items, list) else [items]):
                        if not isinstance(it, dict) or not it.get("metric"):
                            continue
                        batch.append({"device": it.get("device") or s["device"],
                                      "metric": it["metric"], "value": it.get("value"),
                                      "unit": it.get("unit", ""), "source": it.get("source", "ext"),
                                      "confidence": float(it.get("confidence", 1.0)),
                                      "ts": it.get("ts") or now_iso(),
                                      "meta": it.get("meta") or {}})
                    ok, skipped = ingest_items(batch)
                    s["last_n"], s["last_err"] = ok, ""
                    print("[ext] %s → 入库 %d 条（重复跳过 %d）" % (s["name"], ok, skipped), flush=True)
                except Exception as e:
                    s["last_err"] = str(e)[:160]
                    print("[ext] %s 入库失败：%s" % (s["name"], s["last_err"]), flush=True)
        except Exception as e:
            print("[ext] 循环异常：%s" % str(e)[:120], flush=True)
        time.sleep(30)


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

    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

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
        if path == "/login":
            return self._login_post()
        if path == "/admin":
            return self._admin_post(q)
        if not self._auth(q):
            return
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
# ---------------------------------------------------------------- Web 管理台（标准库拼 HTML）
# 设计取舍：
#   · **不引前端框架**（vue/react/alpine 都不引）—— 一旦引了，"零依赖"这条卖点就没了，
#     而且静态资源要单独分发。这里就用 <form> + 一点内联 CSS，够用。
#   · **鉴权与 API 完全同一套 token**：登录页把 token 换成一个 HttpOnly 会话 cookie
#     （SameSite=Strict，抗 CSRF），API 侧仍然只认 X-Token 头，不因为"是网页"就放松。
#   · 能改的只有"开关与人设"这类**可逆**配置；删除/导出这类破坏性动作仍然只在 CLI（hubctl）。
import hashlib
import hmac

_COOKIE_SEED = b"whale-admin-session-v1"


def admin_session():
    """管理台会话值 = HMAC(token)。好处：**轮换 token 会顺带废掉所有旧会话**。"""
    return hmac.new(str(CFG.get("token") or "").encode(), _COOKIE_SEED, hashlib.sha256).hexdigest()[:32]


def _e(x):
    import html as _h
    return _h.escape(str(x if x is not None else "—"))


_CSS = """
:root{color-scheme:dark}
body{background:#0e1116;color:#dfe6ee;font:14px/1.6 -apple-system,"Segoe UI","PingFang SC",sans-serif;margin:0;padding:20px 22px 60px}
h1{font-size:17px;margin:0 0 2px}h2{font-size:14px;margin:26px 0 8px;color:#8ea1b5;font-weight:600}
a{color:#6cb6ff;text-decoration:none}a:hover{text-decoration:underline}
table{border-collapse:collapse;width:100%}td,th{padding:6px 10px;border-bottom:1px solid #1c2431;text-align:left;font-size:13px}
th{color:#8ea1b5;font-weight:600}.dim{color:#7c8b9c}.warn{color:#f0a35e}.bad{color:#ef6f6f}.ok{color:#5fd38a}
ul{margin:0;padding-left:18px}li{margin:2px 0}
.tag{display:inline-block;padding:1px 7px;border:1px solid #2b3646;border-radius:9px;color:#8ea1b5;font-size:12px}
.card{border:1px solid #1c2431;border-radius:10px;padding:12px 14px;margin:8px 0;background:#12161d}
label{display:inline-block;min-width:150px;color:#a9b7c6}
input,select{background:#0b0e13;border:1px solid #2b3646;color:#dfe6ee;border-radius:6px;padding:5px 8px;font:13px/1.4 inherit}
input[type=submit]{background:#1d4e86;border-color:#2a6bb0;cursor:pointer;padding:6px 14px}
input[type=submit]:hover{background:#245da0}
.row{margin:6px 0}
.flash{border-left:3px solid #5fd38a;background:#12201a;padding:8px 12px;border-radius:6px;margin:10px 0}
.flash.bad{border-color:#ef6f6f;background:#201414}
.grid{display:flex;flex-wrap:wrap;gap:14px}.grid>.card{flex:1 1 320px}
"""


def login_html(name="鲸鲸", err=""):
    return ("""<!doctype html><html lang=zh><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>登录 · __NAME__ 中枢</title><style>__CSS__</style>
<h1>__NAME__ · 中枢管理台</h1>
<div class=dim>这个页面能看到你的数据，所以要先验证 —— 口令就是 hub.json 里的 token。</div>
__ERR__
<form class=card method=post action=/login>
<div class=row><label>token</label><input type=password name=token autofocus size=34></div>
<div class=row><input type=submit value=登录></div>
</form>
<p class=dim>命令行里也能看：<code>hubctl status</code> · <code>hubctl token</code>。浏览器登录后会种一个 HttpOnly cookie，
换 token 即失效；API 侧仍然只认 X-Token 头。</p>
"""
            .replace("__CSS__", _CSS)
            .replace("__NAME__", _e(name))
            .replace("__ERR__", ('<div class="flash bad">%s</div>' % _e(err)) if err else ""))


def admin_html(flash="", ok=True):
    # ---- 概览
    try:
        with db() as c:
            n_metrics = c.execute("SELECT COUNT(*) n FROM metrics").fetchone()["n"]
            n_rem = c.execute("SELECT COUNT(*) n FROM reminders").fetchone()["n"]
            n_dev = c.execute("SELECT COUNT(DISTINCT device) n FROM metrics").fetchone()["n"]
    except Exception:
        n_metrics = n_rem = n_dev = 0
    head = ("<h1>%s · 中枢管理台 <span class=tag>v%s</span></h1>"
            "<div class=dim>%s ｜ 代码指纹 %s ｜ 数据点 %s ｜ 设备 %s ｜ 提醒 %s</div>"
            % (_e(CFG["persona"]["name"]), _e(VERSION), _e(now_iso()),
               _e(code_fingerprint()), n_metrics, n_dev, n_rem))

    # ---- 数据源健康度
    rows = []
    try:
        for s, v in source_health().items():
            cls = "ok" if v["verdict"] == "ok" else "warn"
            rows.append("<tr><td>%s</td><td class=%s>%s</td><td>%s</td><td class=dim>%s</td></tr>"
                        % (_e(s), cls, _e(v["verdict"]), _e(v.get("today_n")), _e(v.get("note"))))
    except Exception as e:
        rows.append("<tr><td colspan=4>健康度算不出：%s</td></tr>" % _e(str(e)[:80]))
    health = ("<h2>数据源健康度</h2><table><tr><th>源</th><th>状态</th><th>今天</th><th>说明</th></tr>%s</table>"
              % ("".join(rows) or "<tr><td colspan=4 class=dim>还没有数据源</td></tr>"))

    # ---- 开关（care / privacy / rules）
    care = CFG.get("care") or {}
    priv = CFG.get("privacy") or {}
    rules = CFG.get("rules") or {}
    qh = care.get("quiet_hours") or [23, 7]

    def yn(v):
        return "是" if v else "否"
    switches = """<h2>开关</h2>
<form class=card method=post action=/admin><input type=hidden name=section value=care>
<div class=row><label>主动关心总开关</label><select name=enabled><option value=1 __CE__>开</option><option value=0 __CD__>关</option></select></div>
<div class=row><label>天气关心</label><select name=weather><option value=1 __WE__>开</option><option value=0 __WD__>关</option></select></div>
<div class=row><label>免打扰起（时）</label><input name=quiet_from type=number min=0 max=23 value="__QF__"></div>
<div class=row><label>免打扰止（时）</label><input name=quiet_to type=number min=0 max=23 value="__QT__"></div>
<div class=row><label>每天主动上限（条）</label><input name=daily_max type=number min=0 max=50 value="__DM__"></div>
<div class=row><label>两条最小间隔（分钟）</label><input name=min_gap type=number min=1 max=600 value="__MG__"></div>
<div class=row><input type=submit value="保存开关"></div></form>

<form class=card method=post action=/admin><input type=hidden name=section value=privacy>
<div class=row><label>脱敏总开关</label><select name=priv_enabled><option value=1 __PE__>开</option><option value=0 __PD__>关</option></select></div>
<div class=row><label>保存通知原文（排障用）</label><select name=store_raw><option value=1 __RE__>开</option><option value=0 __RD__>关</option></select></div>
<div class=row><label>保留天数（0=永久）</label><input name=retention type=number min=0 max=3650 value="__RT__"></div>
<div class=row><input type=submit value="保存隐私设置"></div></form>

<form class=card method=post action=/admin><input type=hidden name=section value=rules>
<div class=row><label>连续活跃提醒（分钟）</label><input name=sit type=number min=10 max=300 value="__SIT__"></div>
<div class=row><label>屏幕过高阈值（分钟）</label><input name=screen type=number min=60 max=1440 value="__SCR__"></div>
<div class=row><label>睡眠不足阈值（分钟）</label><input name=sleep type=number min=60 max=900 value="__SLP__"></div>
<div class=row><label>设备离线提醒（小时）</label><input name=offline type=number min=1 max=240 value="__OFF__"></div>
<div class=row><label>课前提醒（分钟，0=关）</label><input name=cls type=number min=0 max=120 value="__CLS__"></div>
<div class=row><input type=submit value="保存规则"></div></form>
""".replace("__CE__", "selected" if care.get("enabled") else "").replace("__CD__", "" if care.get("enabled") else "selected") \
   .replace("__WE__", "selected" if care.get("weather") else "").replace("__WD__", "" if care.get("weather") else "selected") \
   .replace("__QF__", _e(qh[0])).replace("__QT__", _e(qh[1])) \
   .replace("__DM__", _e(care.get("daily_max"))).replace("__MG__", _e(care.get("min_gap_minutes"))) \
   .replace("__PE__", "selected" if priv.get("enabled", True) else "").replace("__PD__", "" if priv.get("enabled", True) else "selected") \
   .replace("__RE__", "selected" if priv.get("store_raw_text") else "").replace("__RD__", "" if priv.get("store_raw_text") else "selected") \
   .replace("__RT__", _e(priv.get("retention_days"))) \
   .replace("__SIT__", _e(rules.get("sit_continuous_minutes"))).replace("__SCR__", _e(rules.get("screen_high_minutes"))) \
   .replace("__SLP__", _e(rules.get("sleep_low_minutes"))).replace("__OFF__", _e(rules.get("device_offline_hours"))) \
   .replace("__CLS__", _e(rules.get("class_remind_minutes")))

    # ---- 人设
    p = CFG["persona"]
    persona = """<h2>人设（改这里 = 三端同步）</h2>
<form class=card method=post action=/admin><input type=hidden name=section value=persona>
<div class=row><label>名字</label><input name=name size=20 value="__N__"></div>
<div class=row><label>自称</label><input name=self_call size=20 value="__SC__"></div>
<div class=row><label>怎么称呼你</label><input name=call_user size=20 value="__CU__"></div>
<div class=row><label>喜欢</label><input name=likes size=30 value="__LK__"></div>
<div class=row><label>禁忌</label><input name=taboo size=30 value="__TB__"></div>
<div class=row><label>语气</label><input name=tone size=60 value="__TN__"></div>
<div class=row><label>文体要求</label><input name=style size=60 value="__ST__"></div>
<div class=row><input type=submit value="保存人设"></div></form>
""".replace("__N__", _e(p.get("name"))).replace("__SC__", _e(p.get("self_call"))) \
   .replace("__CU__", _e(p.get("call_user"))).replace("__LK__", _e(p.get("likes"))) \
   .replace("__TB__", _e(p.get("taboo"))).replace("__TN__", _e(p.get("tone"))) \
   .replace("__ST__", _e(p.get("style")))

    # ---- 配对码
    pairs = []
    try:
        for c_ in pair_list(8):
            state = "已用" if c_["used_at"] else ("已过期" if c_["expires_at"] < now_iso() else "待用")
            pairs.append("<tr><td>%s</td><td>%s</td><td>%s</td><td class=dim>%s</td></tr>"
                         % (_e(c_["code"]), _e(c_["device"] or "(任意)"), _e(state), _e(c_["created_at"][:16])))
    except Exception:
        pass
    pair_block = ("<h2>设备配对（一次性码）</h2>"
                  "<form class=card method=post action=/admin><input type=hidden name=section value=pair>"
                  "<div class=row><label>设备名（可留空）</label><input name=device size=20 placeholder=stm32_room></div>"
                  "<div class=row><input type=submit value=\"生成配对码\"></div></form>"
                  "<table><tr><th>码</th><th>设备</th><th>状态</th><th>生成</th></tr>%s</table>"
                  % ("".join(pairs) or "<tr><td colspan=4 class=dim>还没有配对码</td></tr>"))

    # ---- 外挂扩展 / 决策 / 审计
    ext = "".join("<li>%s → 上次入库 %s 条%s</li>"
                  % (_e(s["name"]), _e(s["last_n"]),
                     (" ｜ <span class=warn>%s</span>" % _e(s["last_err"])) if s["last_err"] else "")
                  for s in EXT["sources"])
    ext_block = ("<h2>外挂扩展</h2><ul>%s</ul>"
                 % (ext or "<li class=dim>（没装扩展）</li>"))

    dec = []
    try:
        with db() as c:
            for d in c.execute("SELECT ts, band, gap_sec, reason FROM decisions "
                               "ORDER BY id DESC LIMIT 10").fetchall():
                dec.append("<li><span class=dim>%s</span> %s ｜ %s 分钟 ｜ %s</li>"
                           % (_e(d["ts"][11:16]), _e(d["band"]), _e(round((d["gap_sec"] or 0) / 60.0)),
                              _e(d["reason"])))
    except Exception:
        pass
    dec_block = ("<h2>决策日志（她为什么这么频繁）</h2><ul>%s</ul>"
                 % ("".join(dec) or "<li class=dim>（还没有）</li>"))

    au = []
    try:
        for a in audit_recent(30):
            cls = {"denied": "warn", "error": "bad"}.get(a["result"], "dim")
            au.append("<tr><td class=dim>%s</td><td>%s</td><td>%s</td><td class=%s>%s</td><td class=dim>%s</td></tr>"
                      % (_e(a["ts"][5:16]), _e(a["action"]), _e(a["target"]), cls,
                         _e(a["result"]), _e(a["note"])))
    except Exception:
        pass
    try:
        st = audit_stats()
        au_stat = "近 7 天共 %s 条 ｜ 鉴权失败 %s ｜ 配置修改 %s" % (st["total"], st["auth_fail"], st["config_change"])
    except Exception:
        au_stat = ""
    audit_block = ("<h2>审计日志 <span class=tag>只记动作，不记内容</span></h2>"
                   "<div class=dim>%s</div>"
                   "<table><tr><th>时间</th><th>动作</th><th>对象</th><th>结果</th><th>说明</th></tr>%s</table>"
                   % (_e(au_stat), "".join(au) or "<tr><td colspan=5 class=dim>（还没有）</td></tr>"))

    links = ("<h2>其他</h2><div class=dim>"
             "<a href=/dash>只读数据页 /dash</a> ｜ "
             "<a href=/llm-preview>看会发给模型的内容 /llm-preview</a> ｜ "
             "<a href=/audit>审计 JSON /audit</a> ｜ "
             "<a href=/health>健康 /health</a> ｜ <a href=/logout>退出</a></div>"
             "<div class=dim style='margin-top:6px'>破坏性动作（导出/删除/备份还原）只在命令行："
             "<code>hubctl dump | prune | backup | restore</code> —— 网页端刻意不做，少一处被误触的面。</div>")

    return ("""<!doctype html><html lang=zh><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>__NAME__ · 管理台</title><style>__CSS__</style>
__HEAD____FLASH__
<div class=grid><div>__HEALTH____DEC__</div><div>__SW____PAIR__</div></div>
__PERSONA____EXT____AUDIT____LINKS__
""".replace("__CSS__", _CSS).replace("__NAME__", _e(CFG["persona"]["name"])).replace("__HEAD__", head)
       .replace("__FLASH__", ('<div class="flash%s">%s</div>' % ("" if ok else " bad", _e(flash))) if flash else "")
       .replace("__HEALTH__", health).replace("__DEC__", dec_block)
       .replace("__SW__", switches).replace("__PAIR__", pair_block)
       .replace("__PERSONA__", persona).replace("__EXT__", ext_block)
       .replace("__AUDIT__", audit_block).replace("__LINKS__", links))


def _num(form, key, cast=int, default=None, lo=None, hi=None):
    try:
        v = cast(form.get(key))
    except Exception:
        return default
    if lo is not None and v < lo:
        return default
    if hi is not None and v > hi:
        return default
    return v


def _flag(form, key):
    return str(form.get(key) or "").strip() in ("1", "on", "true", "yes")


def admin_apply(form, actor=""):
    """执行管理台提交。返回 (ok, 人话说明)。**只动可逆配置**，改完立刻写 hub.json。"""
    if not isinstance(form, dict) or not form:
        return False, "空提交"
    sec = str(form.get("section") or "")
    if sec == "persona":
        p = CFG.setdefault("persona", {})
        for k in ("name", "self_call", "call_user", "likes", "taboo", "tone", "style"):
            if k in form and str(form[k]).strip():
                p[k] = str(form[k]).strip()[:400]
        _save_cfg()
        return True, "人设已保存（三端共用同一份）"
    if sec == "care":
        c_ = CFG.setdefault("care", {})
        c_["enabled"] = _flag(form, "enabled")
        c_["weather"] = _flag(form, "weather")
        qf, qt = _num(form, "quiet_from", lo=0, hi=23), _num(form, "quiet_to", lo=0, hi=23)
        if qf is not None and qt is not None:
            c_["quiet_hours"] = [qf, qt]
        dm = _num(form, "daily_max", lo=0, hi=50)
        mg = _num(form, "min_gap", lo=1, hi=600)
        if dm is not None:
            c_["daily_max"] = dm
        if mg is not None:
            c_["min_gap_minutes"] = mg
        _save_cfg()
        return True, "开关已保存"
    if sec == "privacy":
        pv = CFG.setdefault("privacy", {})
        pv["enabled"] = _flag(form, "priv_enabled")
        pv["store_raw_text"] = _flag(form, "store_raw")
        rt = _num(form, "retention", lo=0, hi=3650)
        if rt is not None:
            pv["retention_days"] = rt
        _save_cfg()
        return True, "隐私设置已保存"
    if sec == "rules":
        r_ = CFG.setdefault("rules", {})
        for key, name, lo, hi in (("sit", "sit_continuous_minutes", 10, 300),
                                  ("screen", "screen_high_minutes", 60, 1440),
                                  ("sleep", "sleep_low_minutes", 60, 900),
                                  ("offline", "device_offline_hours", 1, 240),
                                  ("cls", "class_remind_minutes", 0, 120)):
            v = _num(form, key, lo=lo, hi=hi)
            if v is not None:
                r_[name] = v
        _save_cfg()
        return True, "规则已保存"
    if sec == "pair":
        info = pair_new(str(form.get("device") or "").strip())
        return True, ("配对码 %s（%d 分钟内有效，**只能用一次**）—— 设备侧："
                      "curl -sk 'https://<中枢>:11443/api/pair?c=%s&d=<设备名>'" % (info["code"], info["ttl_minutes"], info["code"]))
    return False, "不认识的 section：%s" % sec


def _save_cfg():
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(CFG, f, ensure_ascii=False, indent=2)
def main():
    init_db()
    ensure_fts()          # ★ 派生索引：建/自愈重建，失败不影响主流程
    apply_persona_pack()          # ★ 人设包：把 personas/<id>/persona.json 合并进 CFG["persona"]
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=ext_loop, daemon=True).start()   # 外挂扩展（无扩展时几乎零开销）
    port = int(CFG["port"])
    ensure_mcu_token()          # ⑧ 没有独立单片机 token 就生成一个（写回 hub.json）
    _t = CFG["token"]
    # 启动日志里把 token 打码（日志有可能被人看到、被 CI 收集）
    print(f"=== hub v{VERSION} 启动 === 端口 {port}  token {_t[:4]}…{_t[-3:]}（完整值在 hub.json）", flush=True)

    # ① HTTPS（上传用这条）：自签证书 + App 端证书固定
    tls = CFG.get("tls") or {}
    cert = os.path.join(BASE, tls.get("cert", "tls/hub.crt"))
    key = os.path.join(BASE, tls.get("key", "tls/hub.key"))
    if os.path.isfile(cert) and os.path.isfile(key):
        try:
            tls_port = int(tls.get("port", 11443))
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            srv = ThreadingHTTPServer(("0.0.0.0", tls_port), Handler)
            srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            print(f"[hub] HTTPS {tls_port} 已开（自签证书，App 端固定）", flush=True)
        except Exception as e:
            print(f"[hub] HTTPS 起不来：{str(e)[:90]}", flush=True)
    else:
        print("[hub] 没找到证书 → 只有 HTTP（上传仍是明文）", flush=True)

    # ② HTTP（状态页/浏览器/本机自测用）
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
