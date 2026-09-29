#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开口价值评估 v2 的回归测试（纯函数，零网络）。

为什么这些断言值得写下来：评分引擎决定"她多久开口一次、开口说什么"，
它坏了**不会报错**，只会表现为"她今天话很多/一句话都不说" —— 属于最难发现的那种故障。
所以每条不成熟处都钉一个断言：

  1. 时间敏感：下节课 20 分钟 vs 今天有课，价值必须差得开（旧版同分）
  2. 轻重分档：电量 5% 未充电 → 紧急通道；35% → 不紧急（旧版 ≤20% 一刀切）
  3. 时效窗：非出门窗的雨不该"紧急"；出门窗里同样一场雨价值更高
  4. 新鲜度：同一件事连着说第 3 次，价值归零（治"换着说法说 23 遍"）
  5. 不饱和：三件小料的价值 < 一件真紧急（或式合并的语义）
  6. 频率档：同一 ctx，低频更少说、高频更多说；档位只动阈值不动属性
  7. 后验不能压掉紧急通道，但可以收紧一般开口
  8. 老问题（数据缺口）不享受紧急待遇
  9. 档位读写：坏文件回落标准档，绝不因此不说
"""
import importlib.util
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "speaker"))

spec = importlib.util.spec_from_file_location("sal", ROOT / "speaker" / "whale_salience.py")
sal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sal)


class T(tuple):
    """time.localtime() 的替身：只要 tm_hour / tm_min / 日期。"""
    __slots__ = ()

    def __new__(cls, h, m=0, day=29, mon=9, year=2026):
        return super().__new__(cls, (year, mon, day, h, m, 0, 0, 0, 0, "", 0, -28800))

    tm_year, tm_mon, tm_mday = property(lambda s: s[0]), property(lambda s: s[1]), property(lambda s: s[2])
    tm_hour, tm_min = property(lambda s: s[3]), property(lambda s: s[4])


def v(ev, kind):
    for it in ev["items"]:
        if it["kind"] == kind:
            return it["value"]
    return 0.0


class TestSalience(unittest.TestCase):
    def test_时间敏感_下节课比今天有课值钱(self):
        now = T(7, 40)
        far = sal.decide({"classes": [{"start_hour": "14时"}]}, mode="normal", now=now)
        soon = sal.decide({"classes": [{"start_hour": "08时"}]}, mode="normal", now=now)
        self.assertGreater(v(soon, "class_soon"), v(far, "class_today"))
        self.assertTrue(soon["urgent"], "20 分钟后上课该走紧急通道")
        self.assertLess(soon["score"], 100)

    def test_电量分档与紧急(self):
        crit = sal.decide({"battery_percent": 5, "battery_charging": False},
                          mode="normal", now=T(14))
        mid = sal.decide({"battery_percent": 35, "battery_charging": False},
                         mode="normal", now=T(14))
        self.assertTrue(crit["urgent"], "5% 未充电 = 紧急")
        self.assertFalse(mid["urgent"], "35% 不该紧急")
        charging = sal.decide({"battery_percent": 5, "battery_charging": True},
                              mode="normal", now=T(14))
        self.assertEqual(charging["items"], [], "在充电就别提电量（角色卡硬规则）")

    def test_时效窗_雨只在出门窗紧急(self):
        rain = {"weather_today": {"desc": "雷阵雨", "tmax": 30, "tmin": 25},
                "weather_now": {"rain_1h": 1.0}}
        out = sal.decide(dict(rain), mode="normal", now=T(7, 30))       # 出门窗
        home = sal.decide(dict(rain), mode="normal", now=T(11, 30))     # 不在窗
        self.assertTrue(out["urgent"], "出门前的雨值得紧急说")
        self.assertFalse(home["urgent"], "不在出门窗，雨再大也不该'紧急'")
        self.assertGreater(v(out, "weather"), v(home, "weather"))

    def test_新鲜度_同一件事第三次就不说(self):
        ctx = {"pc_health": {"disk_free_percent": 3.0}}
        a = sal.decide(ctx, {"repeats": {}}, mode="normal", now=T(14))
        b = sal.decide(ctx, {"repeats": {a["top"]["bucket"]: 2}}, mode="normal", now=T(14))
        c = sal.decide(ctx, {"repeats": {a["top"]["bucket"]: 3}}, mode="normal", now=T(14))
        self.assertGreater(a["score"], b["score"], "第二次就该贬值")
        self.assertEqual(c["score"], 0, "第三次起不该再提（新鲜度 0）")
        self.assertFalse(c["say"])

    def test_不饱和_三件小料不如一件紧急(self):
        small = {"classes": [{"start_hour": "14时"}], "deliveries_7d": 2,
                 "screen_total_minutes_today": 320}
        big = {"pc_health": {"disk_free_percent": 1.0}}
        s1 = sal.decide(dict(small), mode="normal", now=T(9))
        s2 = sal.decide(dict(big), mode="normal", now=T(9))
        self.assertLess(s1["score"], s2["score"], "三件小事不该顶到和'磁盘要满'一样高")

    def test_概率或式_多条合并不超过100(self):
        ctx = {"weather_alert": "暴雨红色预警", "pc_health": {"disk_free_percent": 1.0},
               "battery_percent": 3, "classes": [{"start_hour": "09时"}],
               "screen_total_minutes_today": 800, "deliveries_7d": 3}
        ev = sal.decide(ctx, mode="high", now=T(8, 5))
        self.assertLessEqual(ev["score"], 100)
        self.assertTrue(ev["urgent"])

    def test_频率档_低频更少说高频更多说(self):
        ctx = {"classes": [{"start_hour": "14时"}], "deliveries_7d": 1,
               "screen_total_minutes_today": 330}
        low = sal.decide(dict(ctx), mode="low", now=T(9))
        high = sal.decide(dict(ctx), mode="high", now=T(9))
        self.assertEqual(low["score"], high["score"], "档位不该改动属性算法，只动阈值")
        self.assertFalse(low["say"], "低频：这点料不该开口")
        self.assertTrue(high["say"], "高频：同样的料可以说")
        self.assertLess(sal.MODES["low"]["cap"], sal.MODES["high"]["cap"])

    def test_后验收紧但不压紧急(self):
        urgent_ctx = {"pc_health": {"disk_free_percent": 1.0}}
        weak = {"deliveries_7d": 1, "screen_total_minutes_today": 330}
        u = sal.decide(dict(urgent_ctx), mode="normal", p_accept=0.1, now=T(9))
        w = sal.decide(dict(weak), mode="normal", p_accept=0.1, now=T(9))
        self.assertTrue(u["say"], "后验再低也不能挡住'磁盘只剩 1%'")
        self.assertFalse(w["say"], "料不足 + 后验低 → 收紧到不说话")

    def test_日限只影响非紧急(self):
        ctx = {"deliveries_7d": 1}
        capped = sal.decide(dict(ctx), mode="normal", said_today=99, now=T(9))
        self.assertFalse(capped["say"])
        em = sal.decide({"pc_health": {"disk_free_percent": 1.0}}, mode="normal",
                        said_today=99, now=T(9))
        self.assertTrue(em["say"], "紧急通道绕开日限")

    def test_数据缺口不算紧急且会贬值(self):
        ctx = {"data_health": {"电脑": {"verdict": "missing"},
                               "手机": {"verdict": "missing"}}}
        a = sal.decide(ctx, mode="high", now=T(14))
        self.assertFalse(a["urgent"], "设备没上报是运维噪音，不是紧急")
        self.assertLess(a["score"], 40, "第一天它也不该压过真正的料")

    def test_桶要粗_数字抖动算同一件事(self):
        a = sal.evaluate({"pc_health": {"disk_free_percent": 3.1}}, now=T(14))
        b = sal.evaluate({"pc_health": {"disk_free_percent": 3.4}}, now=T(14))
        self.assertEqual(a["top"]["bucket"], b["top"]["bucket"], "3.1% 与 3.4% 是同一件事")
        k = sal.evaluate({"battery_percent": 12, "battery_charging": False}, now=T(14))
        k2 = sal.evaluate({"battery_percent": 19, "battery_charging": False}, now=T(14))
        self.assertEqual(k["top"]["bucket"], k2["top"]["bucket"], "12% 与 19% 同桶")

    def test_空ctx不崩且不说(self):
        ev = sal.decide({}, mode="normal", now=T(14))
        self.assertEqual(ev["score"], 0)
        self.assertFalse(ev["say"])
        self.assertIsNone(ev["top"])

    def test_坏数据不崩(self):
        junk = {"battery_percent": "abc", "pc_health": {"disk_free_percent": None},
                "classes": "不是列表", "screen_usage_minutes_by_category": {"x": None},
                "bluetooth_batteries": {"耳机": None}}
        ev = sal.decide(junk, mode="normal", now=T(14))
        self.assertIsInstance(ev["score"], int)

    def test_档位读写往返(self):
        d = tempfile.mkdtemp(prefix="freq-")
        p = str(pathlib.Path(d) / "f.json")
        self.assertEqual(sal.load_mode(p), "normal", "文件不存在 → 标准档")
        self.assertTrue(sal.save_mode("high", p))
        self.assertEqual(sal.load_mode(p), "high")
        pathlib.Path(p).write_text("{坏 json", encoding="utf-8")
        self.assertEqual(sal.load_mode(p), "normal", "坏文件绝不导致不说")

    def test_三档参数单调(self):
        lo, no, hi = sal.MODES["low"], sal.MODES["normal"], sal.MODES["high"]
        self.assertGreater(lo["speak"], no["speak"])
        self.assertGreater(no["speak"], hi["speak"])
        self.assertLess(lo["cap"], no["cap"])
        self.assertLess(no["cap"], hi["cap"])
        self.assertGreater(lo["gap_mult"], hi["gap_mult"])

    def test_note_said_记账(self):
        ctx = {"pc_health": {"disk_free_percent": 3.0}}
        ev = sal.evaluate(ctx, now=T(14))
        st = sal.note_said({"repeats": {}}, ev)
        self.assertEqual(st["repeats"][ev["top"]["bucket"]], 1)
        st = sal.note_said(st, ev)
        self.assertEqual(st["repeats"][ev["top"]["bucket"]], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
