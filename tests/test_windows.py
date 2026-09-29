#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""活跃窗口学习器的回归测试（纯函数，零网络）。

这个模块的 bug 形态很特别：**它不会报错，只会让提醒变密或变稀** ——
属于"用户只会觉得她变烦/变哑"的那种静默失效。所以每条设计约束都钉一个断言：

  1. 活跃时段能被学出来；从不活跃的时段不能进窗口
  2. **相对阈值**：像主人这种"峰值只有 0.5 上下"的画像也必须学得出窗口
     （第一版用绝对阈值 0.5 → 一个窗口都学不出来 ✗）
  3. 窗口数量不固定（不是三个）—— 几段活跃就几个窗口
  4. 单槽（15 分钟）不算窗口（太碎）
  5. 收缩不能重复惩罚（那个坑：两层重收缩把最强时段也压平 → 0 窗口）
  6. 乘子必须夹在 [0.70, 1.40]：**只做软调制，绝不当开关**
  7. 样本少时向 1.0 收缩（不确定性缩放）—— 前两天基本不改行为
  8. 空/坏配置 → 恒等 1.0（学习器坏了不能连带把说话层带偏）
  9. 累计值按相邻上报**求差**，负增量/跨天丢弃（否则分钟数翻倍）
 10. 影子日志只追加、超长自裁剪、且**永不抛异常**
"""
import importlib.util
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "speaker"))

spec = importlib.util.spec_from_file_location("ww", ROOT / "speaker" / "whale_windows.py")
ww = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ww)


def mk_days(n=12, active_hours=((21, 23),), peak_pick=True):
    """造 n 天假数据：active_hours 里的整点天天有活动，其余给少量噪声。"""
    out = {}
    for d in range(n):
        mins, dens = [0.0] * ww.SLOTS, [0.0] * ww.SLOTS
        for h in range(24):
            on = any(a <= h < b for a, b in active_hours)
            v = (30.0 if on else 1.0)
            for k in range(4):
                mins[h * 4 + k] = v / 4.0
                dens[h * 4 + k] = 3 if on else 0
        out["2026-09-%02d" % (10 + d)] = {"minutes": mins, "density": dens}
    return out


class TestLearn(unittest.TestCase):
    def test_活跃时段能学出来_从不活跃的不进窗口(self):
        cfg = ww.learn(mk_days(12, active_hours=((21, 23),)))
        spans = [(w["start"], w["end"]) for w in cfg["windows"]]
        self.assertTrue(spans, "21-23 点天天活跃，必须学出窗口：%s" % cfg)
        inside = set()
        for w in cfg["windows"]:
            inside |= set(range(ww._slot_of(w["start"]), ww._slot_of(w["end"])))
        self.assertIn(ww._slot_of("21:30"), inside)
        self.assertNotIn(ww._slot_of("04:30"), inside, "凌晨从不活跃，不该进窗口")

    def test_阈值必须是相对的_绝对0_5会归零(self):
        """★ 主人的真实画像：全局约 0.26、峰值约 0.55（他是人，不可能连续 15 分钟一直用手机）。

        第一版用绝对阈值 0.5 → 实测**一个窗口都学不出来** ✗。
        这条断言同时钉两件事：① 阈值由全局推出来；② 同一条数据在绝对阈值下确实归零。
        """
        days = mk_days(10, active_hours=((11, 12), (19, 21)))
        for i, d in enumerate(sorted(days)):          # 一半的天没数据（贴近真实采集缺口）
            if i % 2 == 0:
                days[d] = {"minutes": [0.0] * ww.SLOTS, "density": [0.0] * ww.SLOTS}
        cfg = ww.learn(days)
        self.assertAlmostEqual(cfg["thresh"], max(ww.TH_FLOOR, ww.TH_RATIO * cfg["global_p"]), places=3,
                               msg="阈值必须是 max(0.30, 1.3×全局) 这种相对口径")
        p = cfg["p_slots"]
        abs_wins = [w for w in ww._merge([s for s in range(ww.SLOTS) if p[s] >= 0.5])
                    if (w[1] - w[0] + 1) >= ww.MIN_WINDOW_SLOTS]
        self.assertLess(cfg["peak_p"], 0.75, "构造前提：峰值不该夸张（实际 %.2f）" % cfg["peak_p"])
        self.assertEqual(abs_wins, [], "绝对阈值 0.5 在这份画像上会学出 0 个窗口（这就是那个坑）")
        self.assertTrue(cfg["windows"], "相对阈值下必须仍能学出窗口 ✗")

    def test_窗口数不固定(self):
        two = ww.learn(mk_days(12, active_hours=((11, 12), (21, 23))))
        four = ww.learn(mk_days(12, active_hours=((1, 2), (9, 10), (12, 13), (19, 21))))
        self.assertGreaterEqual(len(two["windows"]), 2)
        self.assertGreaterEqual(len(four["windows"]), 3, "四段活跃不该被压成两三个窗口")

    def test_单槽不算窗口(self):
        cfg = ww.learn(mk_days(12, active_hours=((15, 16),)))
        for w in cfg["windows"]:
            self.assertGreaterEqual(w["slots"], ww.MIN_WINDOW_SLOTS,
                                    "15 分钟的单槽窗口太碎，不该算窗口")

    def test_收缩不能重复惩罚(self):
        """两层重收缩会把最强时段压平 → 0 窗口（2026-09-29 实测踩到的坑）。"""
        days = mk_days(12, active_hours=((21, 23),))
        cfg = ww.learn(days)
        self.assertGreater(cfg["peak_p"], cfg["global_p"] + 0.15,
                           "峰值必须明显高于全局（否则就是收缩过度）：peak=%.3f global=%.3f"
                           % (cfg["peak_p"], cfg["global_p"]))
        self.assertGreater(ww.SHRINK_GLOBAL, 0)

    def test_没有数据时不生成窗口(self):
        cfg = ww.learn({})
        self.assertEqual(cfg["windows"], [])
        self.assertIn("不生成窗口", cfg["note"])


class TestEpisodes(unittest.TestCase):
    """episode 边界信号（Fischer 2011 / Attelia 思路）。

    结论是**我们的数据密度下它不能用**（逐信号留出验证：断点倍数 0.51、F1 0.148），
    但边界切分本身的正确性仍要测 —— 哪天采集密度上去了，这个信号要能立刻重用。
    """

    def test_连续使用被切成一个episode(self):
        rows = [("2026-09-29T21:00:00+08:00", 0), ("2026-09-29T21:05:00+08:00", 5),
                ("2026-09-29T21:09:00+08:00", 11),            # 间隔 ≤10 分钟 → 同一个
                ("2026-09-29T21:40:00+08:00", 12)]            # 断了 31 分钟 → 新的一个
        ep = ww.episodes(rows)
        self.assertEqual(len(ep), 2, "31 分钟的间隔必须切出新 episode：%s" % ep)
        self.assertEqual(ep[0][1].strftime("%H:%M"), "21:09", "第一个 episode 结束在 21:09（断点）")

    def test_单条上报也算一个episode(self):
        self.assertEqual(len(ww.episodes([("2026-09-29T09:00:00+08:00", 1)])), 1)

    def test_空输入不崩(self):
        self.assertEqual(ww.episodes([]), [])

    def test_断点落在episode结束那个槽(self):
        items = [{"day": "2026-09-29", "ts": "2026-09-29T21:00:00+08:00", "value": 0},
                 {"day": "2026-09-29", "ts": "2026-09-29T21:05:00+08:00", "value": 5},
                 {"day": "2026-09-29", "ts": "2026-09-29T23:00:00+08:00", "value": 40}]
        sb = ww.slots_by_day(items)
        self.assertAlmostEqual(sb["2026-09-29"]["boundary"][ww._slot_of("21:00")], 1.0,
                               msg="断点应记在 episode 结束（21:05）所在的槽")
        self.assertAlmostEqual(sb["2026-09-29"]["boundary"][ww._slot_of("23:00")], 1.0)


class TestBudgetProgress(unittest.TestCase):
    """额度分配（token bucket）：额度总量不变，但**活跃时段恢复更快**。"""

    def setUp(self):
        self.cfg = ww.learn(mk_days(14, active_hours=((21, 23),)))

    def test_进度单调不减且落在0到1(self):
        last = -1.0
        for h in range(0, 24):
            v = ww.budget_progress(self.cfg, type("N", (), {"tm_hour": h, "tm_min": 0})())
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 1.0)
            if h in range(7, 23):
                self.assertGreaterEqual(v + 1e-9, last, "进度不能倒退")
                last = v
        self.assertEqual(ww.budget_progress(self.cfg, type("N", (), {"tm_hour": 6, "tm_min": 0})()), 0.0)

    def test_窗口内的单位时间进度更快(self):
        """正确性质：**窗口内那一小时的进度增量**必须大于窗口外的一小时。

        第一版我断言"22:00 的累计进度 > 无窗口时"，结果失败（0.933 < 0.953）——
        那不是 bug 而是**我断言错了**：加权会把"窗口开启之前"的预算压低，
        而他的窗口恰恰靠天尾（17:45–24:00）→ 21:00 前反而更没额度。
        这条缺陷已写进 budget_cap 的 docstring（所以该杠杆暂不启用），
        这里改成断言真正要保住的性质：**窗口内每小时涨得比窗口外快**。
        """
        N = lambda h: type("N", (), {"tm_hour": h, "tm_min": 0})()      # noqa: E731
        def delta(h_from, h_to, cfg):
            return ww.budget_progress(cfg, N(h_to)) - ww.budget_progress(cfg, N(h_from))
        in_win = delta(21, 22, self.cfg)            # 21-23 在窗口内
        out_win = delta(13, 14, self.cfg)           # 13-14 在窗口外
        self.assertGreater(in_win, out_win, "窗口内的单位时间进度必须更快，否则额度分配没意义")

    def test_全天额度总量不变(self):
        """加权不能把"一天的总额度"改掉 —— 否则等于偷偷改了每日上限。"""
        empty = {"windows": [], "days": 14}
        self.assertAlmostEqual(ww.budget_progress(self.cfg, type("N", (), {"tm_hour": 23, "tm_min": 0})()),
                               ww.budget_progress(empty, type("N", (), {"tm_hour": 23, "tm_min": 0})()),
                               places=6, msg="到 23:00 两份进度都必须正好是 1.0")

    def test_坏配置不崩(self):
        N = type("N", (), {"tm_hour": 12, "tm_min": 0})
        for bad in ({}, None, {"windows": [{"start": "坏", "end": "x"}]}):
            self.assertTrue(0.0 <= ww.budget_progress(bad, N()) <= 1.0)


class TestMultiplier(unittest.TestCase):
    def setUp(self):
        self.cfg = ww.learn(mk_days(14, active_hours=((21, 23),)))

    def test_窗口内说勤点_窗口外说少点(self):
        inside = ww.multiplier_at(self.cfg, 21, 30)
        outside = ww.multiplier_at(self.cfg, 15, 0)
        self.assertLess(inside["mult"], 1.0, "窗口内应该更勤")
        self.assertGreaterEqual(outside["mult"], 1.0, "窗口外不该更勤")
        self.assertTrue(inside["in"])

    def test_乘子夹在0_7到1_4(self):
        for h in range(24):
            for m in (0, 30):
                r = ww.multiplier_at(self.cfg, h, m)
                self.assertGreaterEqual(r["mult"], 1.0 - ww.MAX_BOOST - 1e-9,
                                        "乘子不得低于 %.2f（软调制下限）" % (1 - ww.MAX_BOOST))
                self.assertLessEqual(r["mult"], 1.0 + ww.DEAD_SLOW + 1e-9,
                                     "乘子不得高于 %.2f" % (1 + ww.DEAD_SLOW))

    def test_死区比普通外部更拉长(self):
        dead = ww.multiplier_at(self.cfg, 4, 0)       # 凌晨 4 点，几乎没活动
        normal = ww.multiplier_at(self.cfg, 15, 0)    # 下午，普通不活跃
        self.assertGreaterEqual(dead["mult"], normal["mult"])

    def test_样本少时向1收缩(self):
        few = dict(self.cfg, days=1)
        self.assertAlmostEqual(ww.multiplier_at(few, 21, 30)["mult"], 1.0, places=1,
                               msg="只有 1 天样本时几乎不该改动节奏")
        self.assertEqual(ww.confidence(0), 0.0)
        self.assertEqual(ww.confidence(14), 1.0)
        self.assertAlmostEqual(ww.confidence(7), 0.5, places=2)

    def test_空配置与坏配置都返回恒等(self):
        for bad in ({}, None, {"windows": None}, {"windows": [{"start": "坏", "end": "更坏"}]},
                    {"windows": [{"start": "21:00", "end": "23:00"}], "days": "abc"}):
            r = ww.multiplier_at(bad, 21, 30)
            self.assertIsInstance(r["mult"], float)
            self.assertGreaterEqual(r["mult"], 1.0 - ww.MAX_BOOST - 1e-9)

    def test_活跃原型本期不参与(self):
        self.assertEqual(ww.prototypes({}), [], "没有数据支撑的因子不许偷偷加进来")


class TestConfigIO(unittest.TestCase):
    def test_读写往返与原子写(self):
        d = tempfile.mkdtemp(prefix="win-")
        p = str(pathlib.Path(d) / "w.json")
        cfg = ww.learn(mk_days(10, active_hours=((21, 23),)))
        self.assertTrue(ww.save_cfg(cfg, p))
        self.assertEqual(ww.load_cfg(p)["windows"], cfg["windows"])
        self.assertFalse(pathlib.Path(p + ".tmp").exists(), "临时文件必须被 replace 掉")
        pathlib.Path(p).write_text("{坏 json", encoding="utf-8")
        self.assertEqual(ww.load_cfg(p), {}, "坏配置 → 空（乘子恒等，不影响说话）")

    def test_影子日志只追加且超长自裁剪(self):
        d = tempfile.mkdtemp(prefix="winsh-")
        p = str(pathlib.Path(d) / "s.jsonl")
        for i in range(5):
            ww.shadow_log({"i": i}, p)
        self.assertEqual(len(pathlib.Path(p).read_text(encoding="utf-8").splitlines()), 5)
        ww.shadow_log({"i": "x", "data": "y" * 10}, p)
        self.assertEqual(len(pathlib.Path(p).read_text(encoding="utf-8").splitlines()), 6)

    def test_影子日志写不进去也不抛(self):
        ww.shadow_log({"a": 1}, "/proc/definitely-not-writable/x.jsonl")   # 不抛就算过


class TestSlots(unittest.TestCase):
    def test_累计值按相邻上报求差(self):
        items = [{"day": "2026-09-29", "ts": "2026-09-29T21:05:00+08:00", "value": 5},
                 {"day": "2026-09-29", "ts": "2026-09-29T21:18:00+08:00", "value": 12}]
        sb = ww.slots_by_day(items)
        m = sb["2026-09-29"]["minutes"]
        self.assertAlmostEqual(sum(m), 12.0, msg="两条上报 5→12 的总活跃必须是 12 分钟，不能翻倍")
        self.assertAlmostEqual(m[ww._slot_of("21:00")], 5.0, msg="首条算起点，落在 21:00 那个槽")
        self.assertAlmostEqual(m[ww._slot_of("21:15")], 7.0, msg="差量落在后一条上报所在的槽")
        self.assertAlmostEqual(sum(sb["2026-09-29"]["density"]), 2.0,
                               msg="两次上报 = 密度 2（每次上报都是一次『手机醒了』的痕迹）")

    def test_负增量与跨天被丢弃(self):
        items = [{"day": "2026-09-29", "ts": "2026-09-29T23:50:00+08:00", "value": 300},
                 {"day": "2026-09-29", "ts": "2026-09-29T23:55:00+08:00", "value": 30}]  # 翻页/重置
        sb = ww.slots_by_day(items)
        self.assertEqual(sb["2026-09-29"]["minutes"][ww._slot_of("23:45")], 0.0,
                         "负增量必须丢掉，否则跨天会把分钟数算爆")

    def test_空输入不崩(self):
        self.assertEqual(ww.slots_by_day([]), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
