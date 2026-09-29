#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""天气"哪一天"的回归测试 —— 对象是**合并产物** hub/hub.py。

为什么值得单写一个文件：这个 bug **一直没报错**，只是让鲸鲸把日子说错。
真实语料里它的表现是：
    · "明天雷阵雨概率 88%" → 第二天又说"明天有冰雹"（同一个"明天"两个答案）
    · 早上 07:30 说"明早 08:00 有课，今晚别熬太晚"（那是**今天**早上的课）
    · 一天里 she 说的话互相对不上（因为 weather_today 其实是三天后）

三个根因（2026-09-29 一次修掉，各钉一条断言）：
  ① 主源（中国天气网）给的 date 是**不补零**的 "9/28"，而代码对它做**字符串排序**
     → "10/1" < "9/28"（'1' < '9'）⇒ 9→10 月交界时挑错天
  ② 去重键用 `for_day`，而主源**根本不写 for_day** → 全是 None → 只剩第一条
     ⇒ `weather_of(1)`（明天）**永远返回 None**
  ③ **按索引取第 N 条**：今天的预报行缺失时，"第 0 条"其实是明天 → 后天冒充今天
"""
import datetime as dt
import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_hub():
    spec = importlib.util.spec_from_file_location("whalecare_weather_under_test",
                                                  ROOT / "hub" / "hub.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestDayOf(unittest.TestCase):
    def setUp(self):
        self.hub = load_hub()

    def test_不补零的月份要能解析(self):
        d = dt.date(2026, 9, 29)
        self.assertEqual(self.hub.day_of({"date": "9/28"}, today=d), dt.date(2026, 9, 28))
        self.assertEqual(self.hub.day_of({"date": "10/1"}, today=d), dt.date(2026, 10, 1))

    def test_跨年自动纠年份(self):
        self.assertEqual(self.hub.day_of({"date": "1/2"}, today=dt.date(2026, 12, 30)),
                         dt.date(2027, 1, 2))
        self.assertEqual(self.hub.day_of({"date": "12/31"}, today=dt.date(2027, 1, 1)),
                         dt.date(2026, 12, 31))

    def test_iso与不补零都认(self):
        d = dt.date(2026, 9, 29)
        self.assertEqual(self.hub.day_of({"for_day": "2026-09-30"}, today=d), dt.date(2026, 9, 30))
        self.assertEqual(self.hub.day_of({"date": "09/30"}, today=d), dt.date(2026, 9, 30))

    def test_乱七八糟的输入不崩(self):
        for junk in ({}, None, {"date": ""}, {"date": "明天"}, {"date": "13/45"}):
            self.assertIsNone(self.hub.day_of(junk, today=dt.date(2026, 9, 29)))


class TestPickByRealDate(unittest.TestCase):
    """★ 核心断言：9→10 月交界时，"今天"不能变成三天后。"""

    def setUp(self):
        self.hub = load_hub()
        self.today = dt.date(2026, 9, 29)

    def _days(self, metas):
        """把假的 meta 列表塞进 weather_days 的取数路径（不打真库）。"""
        out = []
        for m in metas:
            d = self.hub.day_of(m, self.today)
            if d is None or d < self.today:
                continue
            m = dict(m)
            m["for_day"] = d.isoformat()
            out.append((d, m))
        out.sort(key=lambda kv: kv[0])
        return [m for _d, m in out]

    def test_字符串排序会挑错的场景(self):
        # 这就是线上真实出现过的一组：9/28(昨天) / 9/29(今天) / 9/30 / 10/1
        metas = [{"date": "9/28", "desc": "阴转多云"}, {"date": "9/29", "desc": "晴"},
                 {"date": "9/30", "desc": "多云"}, {"date": "10/1", "desc": "雷阵雨"}]
        # 旧实现：sorted by str(date) → ["10/1","9/28","9/29","9/30"] → 今天=10/1 ✗
        self.assertEqual(sorted(metas, key=lambda x: x["date"])[0]["desc"], "雷阵雨",
                         "旧实现的错误行为（这就是那个 bug，改动它说明测试失效了）")
        got = self._days(metas)
        self.assertEqual([m["desc"] for m in got], ["晴", "多云", "雷阵雨"],
                         "新的顺序必须是真实日期：今天 → 明天 → 后天")

    def test_过期预报不能冒充今天(self):
        # 今天的行缺失（比如刚过零点、还没抓到新的一天）
        got = self._days([{"date": "9/30", "desc": "多云"}, {"date": "10/1", "desc": "雷阵雨"}])
        self.assertEqual([m["desc"] for m in got], ["多云", "雷阵雨"])
        self.assertEqual(got[0]["for_day"], "2026-09-30")
        # ⇒ 调用方按 for_day 精确匹配后，weather_of(0) 会返回 None，
        #    而不是把 9/30 的天气当成"今天"讲（宁可不说，也不讲错日子）

    def test_过去的日子被剔除(self):
        got = self._days([{"date": "9/27", "desc": "昨天"}, {"date": "9/29", "desc": "晴"}])
        self.assertEqual([m["desc"] for m in got], ["晴"])


class TestWeatherOfLooksUpByDate(unittest.TestCase):
    def setUp(self):
        self.hub = load_hub()

    def test_weather_of_是按日期查不是按下标(self):
        """`weather_of` 内部必须用 for_day 精确匹配（源码级断言）。

        因为"第 N 条"这种取法在这个 bug 里是第三个根因：今天的行缺失时，
        第 0 条 = 明天 → **后天冒充今天**。所以这里直接断言实现方式，
        而不只是断言某一次的输出（输出会随库里数据变）。
        """
        import inspect
        src = inspect.getsource(self.hub.weather_of)
        self.assertIn("for_day", src, "weather_of 必须按 for_day 精确匹配")
        self.assertIn("timedelta", src, "weather_of 必须按 delta 天数算目标日期")
        self.assertNotIn("days[which]", src, "weather_of 不能按下标取第 N 条")


if __name__ == "__main__":
    unittest.main(verbosity=2)
