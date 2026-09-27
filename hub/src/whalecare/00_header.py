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
VERSION = "0.1.27"
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


