#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「料分读的字段」必须真的存在：跨模块字段一致性测试。

为什么必须有（真实踩过）：说话层的 material_score 读的是
`weather_today.rain_prob` / `classes_today` / `games_minutes_today`，
而中枢 llm_context 给的是 `weather_today.desc`（"雷阵雨"）/ `classes`（列表）/ 根本没有游戏时长。
**字段名对不上时不会报任何错，只是永远算 0 分** —— 于是"今天雷阵雨、电脑磁盘只剩 1.1%"
这种明摆着的料一个都不算，她也就一直沉默。这类 bug 靠看日志看不出来。
（和分桶名错位是同一类问题：**两边约定了一个字符串，却没人比对过**。）

做法：从中枢 llm_context 的源码里静态抽出它会给的所有键，
    再从说话层 material_score 的源码里抽出所有读的键，比对。
"""
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 允许缺席的键：依赖设备/数据源，数据没来就不该扣分（不是 bug）
OPTIONAL = {
    "sleep_minutes",   # 采集端还没接睡眠源
    "sleep",           # 同上（有数据时中枢才给）
    "bluetooth_batteries", "battery_charging", "battery_percent",
    "games_minutes_today",
}


def hub_context_keys():
    """静态抽取 hub.llm_context() 会给的键（dict 字面量的键 + ctx["k"] = ... 的键）。"""
    src_path = ROOT / "hub" / "hub.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "llm_context"), None)
    if fn is None:
        raise AssertionError("hub.py 里找不到 llm_context()")
    keys = set()
    for node in ast.walk(fn):
        # ctx = {"a": ..., "b": ...}
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "ctx" and isinstance(node.value, ast.Dict):
                    keys |= {k.value for k in node.value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        # ctx["k"] = ...
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "ctx":
            sl = node.slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                keys.add(sl.value)
    return keys


def speaker_read_keys():
    """抽取评分引擎里所有读的键（ctx.get("k") / ctx["k"]）。

    ★ 2026-09-29：料分的实现从 whale_speaker.material_score 搬到了
      speaker/whale_salience.py（加权五维）。这个测试必须跟着**实现**走，
      否则它会因为读的是那个"薄封装"而抽不到键 → 静默失效（守卫变摆设）。
      现在 cand_ctx 是唯一的读取点，material_score 只是它的薄封装。
    """
    keys = set()
    for rel, fn_name in (("speaker/whale_salience.py", "candidates"),):
        src = (ROOT / rel).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == fn_name), None)
        if fn is None:
            raise AssertionError("%s 里找不到 %s()" % (rel, fn_name))
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "ctx" and node.args:
                    a0 = node.args[0]
                    if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                        keys.add(a0.value)
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "ctx":
                sl = node.slice
                if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                    keys.add(sl.value)
    return keys


class TestMaterialSignals(unittest.TestCase):
    def test_抽键本身要有效(self):
        hk, sk = hub_context_keys(), speaker_read_keys()
        self.assertGreater(len(hk), 5, "没抽到中枢的键，抽取逻辑坏了：%s" % hk)
        self.assertGreater(len(sk), 5, "没抽到料分的键：%s" % sk)

    def test_料分读的键中枢都得给(self):
        hk, sk = hub_context_keys(), speaker_read_keys()
        missing = sorted(sk - hk - OPTIONAL)
        self.assertEqual(missing, [], "料分读了中枢不给的键（会静默算 0 分）：%s" % missing)

    def test_两份料分实现必须一致(self):
        """闸门用的 material_of() 与要展示的 material_score() 必须**恒等**。

        踩过的坑：这两个函数曾经是**两份独立实现**，字段名还各自跟中枢对不上；
        只修一份就会出现"以为修好了、日志里料分还是 2"。现在 material_of 是薄封装，
        这个断言让它们不可能再分家。
        """
        import importlib.util as iu
        import os as _os
        import tempfile
        _os.environ.setdefault("WHALE_HOME", tempfile.mkdtemp(prefix="whale-mat-"))
        sys_mod = __import__("sys")
        spk_dir = str(ROOT / "speaker")
        if spk_dir not in sys_mod.path:
            sys_mod.path.insert(0, spk_dir)
        spec = iu.spec_from_file_location("spk_mat_test", ROOT / "speaker" / "whale_speaker.py")
        m = iu.module_from_spec(spec)
        spec.loader.exec_module(m)
        # 三种典型上下文：空 / 一个真实形状的 / 极端的
        cases = [
            {},
            {"weather_today": {"desc": "雷阵雨", "tmax": 32, "tmin": 24},
             "weather_now": {"desc": "多云", "rain_24h": 0.0},
             "pc_health": {"disk_free_percent": 1.1},
             "classes": [{"periods": "第1-2节"}],
             "screen_total_minutes_today": 536,
             "screen_usage_minutes_by_category": {"社交": 296},
             "surprise": {"其他": "x"}, "most_notable": {"what": "其他"}},
            {"battery_percent": 8, "bluetooth_batteries": {"WF": {"percent": 5}},
             "screen_total_minutes_today": 999},
        ]
        for c in cases:
            self.assertEqual(m.material_of(c), m.material_score(c)[0],
                             "两份料分结果不一致（ctx=%s）" % list(c))

    def test_可选键要在注释里说明为什么可选(self):
        # 防呆：往 OPTIONAL 里塞东西必须是真的"数据依赖"，别拿它当橡皮擦
        hk = hub_context_keys()
        for k in OPTIONAL:
            self.assertTrue(
                k in hk or k in ("sleep_minutes", "games_minutes_today"),
                "%s 既不在中枢的键里、也不是已知的数据依赖项 —— 是不是拼错了？" % k)


if __name__ == "__main__":
    unittest.main()
