#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""说话层的两条"事后守卫"（都是 2026-09-29 上线当天实测误伤后补的）。

为什么单独一个文件：这两条规则都不属于"评分"，但都属于**静默失效**型故障 ——
拦错了不会报错，只会表现为"她该说的没说"（用户唯一的感觉是"她怎么变蠢了"）。

  ① `_is_meter_report` 原来把"分钟/小时"当通用关键词 → 紧急提醒
     "11 点第 5-6 节上课，还剩 37 分钟" 被判成"数值播报"，
     再被事实去重判"数值没变" → **当场把该说的课静默拦掉** ✗
  ② `topic_of` 里"天气"排在前面 → 当天说过一句日常天气后，
     **暴雨预警**会被"这个话题今天说过了"挡掉 ✗（一周模拟抓到）
"""
import importlib.util
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "speaker"))
os.environ.setdefault("WHALE_HOME", tempfile.mkdtemp(prefix="whale-guard-"))

spec = importlib.util.spec_from_file_location("spk_guard", ROOT / "speaker" / "whale_speaker.py")
sp = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(sp)
except SystemExit:
    pass


class TestMeterGuard(unittest.TestCase):
    def test_倒计时与课表不算数值播报(self):
        for t in ("（翻了翻课表）主人，11 点第 5-6 节上课，还剩 37 分钟",
                  "下节课还有 60 分钟", "明早 08:00 有课",
                  "闹钟 30 分钟后响", "（记下来了）明天下午 2 点开会"):
            self.assertFalse(sp._is_meter_report(t), "不该被判成数值播报：%s" % t)

    def test_真状态播报仍然算(self):
        for t in ("屏幕今天 7 小时 14 分", "电脑磁盘只剩 1.2%", "手机电量 12% 没充电",
                  "今天社交 296 分钟", "连续坐了 11 小时"):
            self.assertTrue(sp._is_meter_report(t), "应该算数值播报：%s" % t)


class TestTopicGuard(unittest.TestCase):
    def test_预警不与天气同话题(self):
        alert = sp.topic_of("天气预警：暴雨黄色预警")
        wx = sp.topic_of("今天雷阵雨，出门带伞")
        self.assertEqual(alert[0], "天气预警")
        self.assertEqual(wx[0], "天气")
        self.assertNotEqual(alert[0], wx[0], "预警必须独立成话题，否则会被日常天气挡掉")

    def test_预警之后当天仍能说(self):
        sp.SAID_TODAY.clear()
        sp.topic_mark_said("（看了下天气）今天多云，25 到 33 度")
        self.assertFalse(sp.topic_already_said_today("（认真）气象台发了暴雨黄色预警，出门带伞"),
                         "预警不该被普通的天气话挡掉")


class TestPendingDoesNotSwallow(unittest.TestCase):
    """★ 所有 `/pending` 调用都必须带 peek=1（只有回执那一步才认领）。

    踩过的坑（2026-09-29 实测丢消息）：中枢的 `/pending` **不带 peek 就是"认领"** ——
    `UPDATE reminders SET status='delivered', delivered_to=?`。而自检原来写了裸的
    `/pending?for=…` → **每次重启都把待发提醒标记成已投递、却一条都没发出去**。
    这类 bug 不会报错、只会让用户"什么都没收到"，所以用静态守卫钉住它。
    """

    def test_每处pending都要peek(self):
        src = (ROOT / "speaker" / "whale_speaker.py").read_text(encoding="utf-8")
        calls = [ln.strip() for ln in src.splitlines()
                 if "/pending" in ln and "hub(" in ln]
        self.assertTrue(calls, "没找到 /pending 调用（抽取逻辑坏了）")
        for ln in calls:
            self.assertIn("peek=1", ln, "这处 /pending 没带 peek=1，会吃掉待发队列：%s" % ln)


if __name__ == "__main__":
    unittest.main(verbosity=2)
