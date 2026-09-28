#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分析出口（数据出口）：格式规范 + 通道行为 的回归测试。

这个出口和另外 8 个的根本差别是**产物是数据、不是句子**，所以测试的重点也不同：

  ① **格式规范必须真被强制**：不合规 → 拒收（不落库、不分发）。
     如果只在提示词里写"请守规范"，那就等于没有约束 —— 这里逐条打各种脏输入。
  ② **规范只有一份**：`docs/analysis.schema.json`（人读版 ANALYSIS-FORMAT.md）。
     中枢单文件里嵌了一份副本 → 两份漂移必须能立刻发现。
  ③ **文档里的示例必须真的合规** —— 否则规范文档本身在教错东西。
  ④ 路由层（`/analysis`、`/analysis/schema`）**真起 HTTP 打一遍**：
     函数级测试抓不到"路由挂错分支"（`/consent` 那次就是这么翻车的）。
"""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
HUB_SRC = ROOT / "hub" / "hub.py"
FRAG = ROOT / "hub" / "src" / "whalecare" / "93_channels.py"
SCHEMA_FILE = ROOT / "docs" / "analysis.schema.json"

GOT = []


class Stub(BaseHTTPRequestHandler):
    """接住分析出口发出去的请求，原样记账。"""

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        GOT.append({"path": self.path, "body": self.rfile.read(n).decode("utf-8", "replace")})
        out = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def load_hub(home):
    os.environ["WHALE_HOME"] = str(home)
    os.environ.setdefault("WHALE_QUIET", "1")
    spec = importlib.util.spec_from_file_location("hub_analysis_test", HUB_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.init_db()
    return mod


def load_writer():
    """写端（speaker/whale_analyze.py）—— 只测它的纯函数（不碰模型/网络）。"""
    spec = importlib.util.spec_from_file_location("whale_analyze_under_test",
                                                 ROOT / "speaker" / "whale_analyze.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GOOD = {
    "v": 1, "day": "2026-09-28",
    "values": [{"id": "screen_active_minutes", "v": 554, "unit": "min"}],
    "trends": [{"id": "screen_active_minutes", "dir": "up", "delta_pct": 14,
                "vs": "7日均值", "conf": "mid"}],
    "outliers": [{"id": "screen_active_minutes", "side": "high", "z": 2.1, "conf": "mid"}],
    "pairs": [{"a": "game_minutes", "b": "screen_active_minutes", "rho": 0.62, "n": 9, "conf": "mid"}],
    "scores": [{"id": "作息规律", "v": 72, "of": 100}],
    "tags": ["睡眠不足", "久坐"], "notes": ["心率今日缺"],
}


class TestSchemaSingleSource(unittest.TestCase):
    """规范只有一份：仓库文档 vs 中枢单文件里的嵌入副本。"""

    def test_嵌入副本与仓库文档逐项一致(self):
        doc = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
        frag = FRAG.read_text(encoding="utf-8")
        emb = frag.split("ANALYSIS_SCHEMA_JSON = r'''", 1)[1].split("'''", 1)[0]
        self.assertEqual(json.loads(emb), doc,
                         "★ 中枢里嵌的规范与 docs/analysis.schema.json 漂移了 —— "
                         "跑 python3 /tmp/sync_analysis_schema.py（或手工同步）")

    def test_产物里也带着规范(self):
        """分发给用户的 hub.py 必须自带规范（否则 GET /analysis/schema 是空的）。"""
        self.assertIn("ANALYSIS_SCHEMA_JSON", HUB_SRC.read_text(encoding="utf-8"))

    def test_文档规范声明的版本是1(self):
        self.assertEqual(json.loads(SCHEMA_FILE.read_text(encoding="utf-8")).get("version"), 1)


class AnalysisBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        cls.hook = "http://127.0.0.1:%d/analysis" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        GOT.clear()
        self.home = pathlib.Path(tempfile.mkdtemp(prefix="whale-an-"))
        self.h = load_hub(self.home)
        self.h.CFG["channels"] = {"analysis_webhook": self.hook}
        self.h.CFG["channels"]["analysis_keep"] = 3


class TestValidator(AnalysisBase):
    """规范里的每一条约束都要真的能拦住（逐条打脏输入）。"""

    def test_文档里的示例必须合规(self):
        """规范文档自己的 example 是教具 —— 它错了比没写还糟。"""
        ex = json.loads(SCHEMA_FILE.read_text(encoding="utf-8")).get("examples") or []
        self.assertTrue(ex, "规范里应当带示例")
        for e in ex:
            self.assertEqual([], self.h.analysis_validate(e), f"示例不合规：{e}")

    def test_合规样本零错误(self):
        self.assertEqual([], self.h.analysis_validate(GOOD))

    def test_缺v或版本不对被拒(self):
        self.assertTrue(self.h.analysis_validate({"values": [{"id": "a", "v": 1}]}),
                        "★ 缺 v：读方无法判断版本，必须拒")
        bad = dict(GOOD, v=2)
        self.assertTrue(self.h.analysis_validate(bad), "不认识的版本必须拒（v1 是唯一合法值）")

    def test_顶层多一个键就被拒(self):
        bad = dict(GOOD, sentences=["主人记得喝水"])
        errs = self.h.analysis_validate(bad)
        self.assertTrue(errs)
        self.assertIn("sentences", " ".join(errs))

    def test_句子标签被拒(self):
        bad = dict(GOOD, tags=["主人该休息了，别太累"])
        errs = self.h.analysis_validate(bad)
        self.assertTrue(errs, "★ 标签里出现句读 —— 这正是本出口要防的\"退化成句子\"")
        self.assertIn("短标签", " ".join(errs))

    def test_超长标签被拒(self):
        bad = dict(GOOD, tags=["x" * 25])
        self.assertTrue(self.h.analysis_validate(bad))

    def test_枚举越界被拒(self):
        bad = dict(GOOD, trends=[{"id": "a", "dir": "upward"}])
        errs = self.h.analysis_validate(bad)
        self.assertTrue(errs)
        self.assertIn("up/down/flat", " ".join(errs))

    def test_指标id带空格被拒但中文允许(self):
        self.assertTrue(self.h.analysis_validate(dict(GOOD, values=[{"id": "screen minutes", "v": 1}])),
                        "id 里不该有空白")
        ok = dict(GOOD, values=[{"id": "明日方舟", "v": 72}])
        self.assertEqual([], self.h.analysis_validate(ok),
                         "★ 游戏名/分类名是合法指标键（中文必须放行），否则溯源就断了")

    def test_数值字段不接受字符串(self):
        bad = dict(GOOD, values=[{"id": "a", "v": "554min"}])
        self.assertTrue(self.h.analysis_validate(bad), "数值必须是数字，不能带单位后缀")

    def test_pairs_rrho越界与缺n(self):
        self.assertTrue(self.h.analysis_validate(dict(GOOD, pairs=[{"a": "x", "b": "y", "rho": 1.4}])))
        self.assertTrue(self.h.analysis_validate(dict(GOOD, pairs=[{"a": "x", "b": "y", "rho": 0.5, "n": 0}])))

    def test_scores范围与必填(self):
        self.assertTrue(self.h.analysis_validate(dict(GOOD, scores=[{"id": "负荷", "v": 120}])))
        self.assertTrue(self.h.analysis_validate(dict(GOOD, scores=[{"id": "负荷"}])))

    def test_条数上限(self):
        self.assertTrue(self.h.analysis_validate(dict(GOOD, tags=["t%d" % i for i in range(13)])))
        self.assertTrue(self.h.analysis_validate(
            dict(GOOD, values=[{"id": "a", "v": 1}] * 41)))

    def test_数组里多字段也被拒(self):
        bad = dict(GOOD, values=[{"id": "a", "v": 1, "advice": "多喝水"}])
        self.assertTrue(self.h.analysis_validate(bad))


class TestIngestGate(AnalysisBase):
    """不合规 → 拒收（不落库、不分发、留审计）。"""

    def test_合规时落库并分发(self):
        r = self.h.analysis_ingest({"analysis": GOOD, "engine": "unit-model"})
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(len(GOT), 1, "应当分发给分析出口")
        env = json.loads(GOT[0]["body"])
        self.assertEqual(env["type"], "analysis")
        self.assertEqual(env["schema"], 1)
        self.assertEqual(env["engine"], "unit-model")
        self.assertEqual(env["analysis"]["values"][0]["v"], 554)
        self.assertIsNotNone(r.get("id"))

    def test_不合规时拒收且不落库不分发(self):
        bad = dict(GOOD, tags=["主人该休息了，别太累"])
        r = self.h.analysis_ingest({"analysis": bad, "engine": "unit-model"})
        self.assertFalse(r.get("ok"))
        self.assertTrue(r.get("rejected"))
        self.assertTrue(r.get("errors"))
        self.assertEqual(GOT, [], "★ 拒收的东西一个字都不许发出去")
        self.assertEqual(self.h.analysis_recent(5), [], "★ 拒收的东西不许落库")

    def test_拒收会写审计(self):
        self.h.analysis_ingest({"analysis": {"values": "not-a-list", "v": 1}})
        acts = [r["action"] for r in self.h.audit_recent(10)]
        self.assertIn("analysis_reject", acts)

    def test_干跑不落库不分发(self):
        r = self.h.analysis_ingest({"analysis": GOOD}, validate_only=True)
        self.assertTrue(r.get("ok"))
        self.assertEqual(GOT, [])
        self.assertEqual(self.h.analysis_recent(5), [])

    def test_干跑能把错误报出来(self):
        r = self.h.analysis_ingest({"analysis": {"v": 1, "tags": ["带。句读"]}}, validate_only=True)
        self.assertFalse(r.get("ok"))
        self.assertTrue(r.get("errors"))

    def test_空对象被拒(self):
        self.assertFalse(self.h.analysis_ingest({}).get("ok"))
        self.assertFalse(self.h.analysis_ingest({"analysis": {}}).get("ok"))

    def test_超大体积被拒(self):
        """体积上限：它是数据，不该长成散文（也防"一个包把内存吃光"）。"""
        r = self.h.analysis_ingest({"analysis": {"v": 1, "notes": ["n"] * 6, "x": "y" * 70000}})
        self.assertFalse(r.get("ok"))
        self.assertIn("太大", str(r.get("error") or ""))

    def test_unverified会回报为warn(self):
        """unverified 由**写端**打（它才知道上下文）；中枢的职责是把它回显提醒读端。"""
        an = json.loads(json.dumps(GOOD))
        an["values"] = [{"id": "screen_active_minutes", "v": 12345, "unverified": True}]
        r = self.h.analysis_ingest({"analysis": an})
        self.assertTrue(r.get("ok"), r)
        self.assertIn("unverified", str(r.get("warn") or ""),
                      "★ 有对不上的数值就该提醒读端当弱证据")
        self.assertTrue(json.loads(GOT[0]["body"])["analysis"]["values"][0]["unverified"])

    def test_没配出口也能入库(self):
        self.h.CFG["channels"] = {}
        r = self.h.analysis_ingest({"analysis": GOOD})
        self.assertTrue(r.get("ok"))
        self.assertIn("note", r.get("fanout") or {})
        self.assertEqual(len(self.h.analysis_recent(1)), 1)


class TestStoreAndFile(AnalysisBase):
    def test_只保留最近keep份(self):
        for i in range(5):
            an = json.loads(json.dumps(GOOD))
            an["tags"] = ["第%d次" % i]
            self.h.analysis_ingest({"analysis": an})
        self.assertEqual(len(self.h.analysis_recent(10)), 3, "analysis_keep=3 应只留 3 份")
        self.assertEqual(self.h.analysis_recent(1)[0]["analysis"]["tags"], ["第4次"],
                         "留下的是最新的")

    def test_按时间倒序(self):
        for t in ("A", "B"):
            an = json.loads(json.dumps(GOOD))
            an["tags"] = [t]
            self.h.analysis_ingest({"analysis": an})
        tags = [x["analysis"]["tags"][0] for x in self.h.analysis_recent(5)]
        self.assertEqual(tags, ["B", "A"])

    def test_落文件是原子写且不留tmp(self):
        f = self.home / "out" / "analysis.json"
        self.h.CFG["channels"] = {"analysis_file": str(f)}
        self.h.analysis_ingest({"analysis": GOOD, "engine": "m"})
        self.assertTrue(f.is_file(), "应当落出文件（目录不存在时要自己建）")
        self.assertFalse(pathlib.Path(str(f) + ".tmp").exists(), "不该留下 .tmp")
        d = json.loads(f.read_text(encoding="utf-8"))
        self.assertEqual(d["type"], "analysis")
        self.assertEqual(d["analysis"]["tags"], ["睡眠不足", "久坐"])

    def test_view里报出规范入口(self):
        v = self.h.analysis_view({"limit": ["2"]})
        self.assertEqual(v["schema"], "/analysis/schema")
        self.assertEqual(v["schema_version"], 1)
        self.assertIn("count", v)


class TestRoutes(AnalysisBase):
    """真起 HTTP —— 路由挂错分支这类错只有这一层能抓到。"""

    def _srv(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), self.h.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    def test_schema_路由能取到规范(self):
        srv = self._srv()
        try:
            r = urllib.request.Request("http://127.0.0.1:%d/analysis/schema" % srv.server_address[1],
                                       headers={"X-Token": self.h.CFG["token"]})
            with urllib.request.urlopen(r, timeout=15) as x:
                d = json.loads(x.read().decode())
            self.assertEqual(d.get("version"), 1)
            self.assertIn("properties", d)
        finally:
            srv.shutdown()

    def test_post_analysis_合规走通_不合规被拒(self):
        srv = self._srv()
        base = "http://127.0.0.1:%d" % srv.server_address[1]

        def post(payload_segment):
            r = urllib.request.Request(base + payload_segment,
                                       data=json.dumps({"analysis": GOOD, "engine": "route"}).encode(),
                                       headers={"Content-Type": "application/json",
                                                "X-Token": self.h.CFG["token"]}, method="POST")
            with urllib.request.urlopen(r, timeout=15) as x:
                return json.loads(x.read().decode())

        try:
            ok = post("/analysis")
            self.assertTrue(ok.get("ok"), ok)
            dry = post("/analysis?validate=1")
            self.assertTrue(dry.get("ok"))
            self.assertEqual(len(self.h.analysis_recent(10)), 1, "干跑不该再落一份")
        finally:
            srv.shutdown()

    def test_health_接口列表里有分析出口(self):
        txt = (ROOT / "hub" / "src" / "whalecare" / "97_http.py").read_text(encoding="utf-8")
        m = txt.split('"endpoints":', 1)[1].split("]", 1)[0]
        self.assertIn("/analysis", m)
        self.assertIn("/analysis/schema", m)


class TestWriterConforms(AnalysisBase):
    """写端（AI 产物 → 规范）的收敛逻辑：脏输出必须被收进规范，且**必过**中枢校验。"""

    CTX = {
        "date": "2026-09-28",
        "screen_active_minutes": 480,
        "sleep": {"minutes_rounded": 390},
        "games_minutes_today": {"明日方舟": 72},
        "sleep_minutes": 390,
    }

    def setUp(self):
        super().setUp()
        self.wr = load_writer()

    def _norm(self, obj):
        return self.wr.normalize(obj, self.CTX)

    def test_不在上下文里的指标被丢掉(self):
        clean, rep = self._norm({"v": 1, "values": [{"id": "编造的指标", "v": 1},
                                                   {"id": "screen_active_minutes", "v": 480}]})
        self.assertEqual([x["id"] for x in clean["values"]], ["screen_active_minutes"])
        self.assertEqual(1, len(rep["dropped_items"]))

    def test_中文键可用_也可用点号路径(self):
        clean, _ = self._norm({"v": 1, "values": [{"id": "明日方舟", "v": 72},
                                                 {"id": "sleep.minutes_rounded", "v": 390}]})
        self.assertEqual(sorted(x["id"] for x in clean["values"]), ["sleep.minutes_rounded", "明日方舟"])

    def test_句子标签被裁掉句读而不是原样放行(self):
        clean, _ = self._norm({"v": 1, "tags": ["今天睡得很少，要早点休息"],
                               "scores": [{"id": "作息规律。周", "v": 70}]})
        self.assertEqual([], self.h.analysis_validate(clean), "裁完必须能过规范")
        self.assertNotIn("，", clean["tags"][0])
        self.assertNotIn("。", clean["scores"][0]["id"])

    def test_值对不上就标unverified而不是删(self):
        clean, rep = self._norm({"v": 1, "values": [{"id": "screen_active_minutes", "v": 9999}]})
        self.assertTrue(clean["values"][0]["unverified"])
        self.assertEqual(1, rep["unverified"])

    def test_枚举与数值越界被收敛或丢弃(self):
        clean, _ = self._norm({"v": 1,
                               "trends": [{"id": "screen_active_minutes", "dir": "暴增", "delta_pct": 10}],
                               "pairs": [{"a": "sleep_minutes", "b": "screen_active_minutes",
                                          "rho": 9, "n": 0}],
                               "scores": [{"id": "负荷", "v": 300}]})
        self.assertEqual([], self.h.analysis_validate(clean), f"收敛后应合规：{clean}")
        self.assertNotIn("trends", clean, "dir 不认识 → 该项丢掉")
        self.assertEqual(clean["pairs"][0]["rho"], 1.0, "rho 收敛到 [-1,1]")
        self.assertEqual(clean["pairs"][0]["n"], 1, "n 至少 1")
        self.assertEqual(clean["scores"][0]["v"], 100.0, "分数上限 100")

    def test_顶级多出的键被丢(self):
        clean, rep = self._norm({"v": 1, "advice": "记得喝水", "tags": ["久坐"]})
        self.assertNotIn("advice", clean)
        self.assertIn("advice", rep["dropped_keys"])

    def test_规范样本原样通过(self):
        clean, _ = self._norm(GOOD)
        self.assertEqual([], self.h.analysis_validate(clean), clean)


class TestCli(AnalysisBase):
    """`hubctl analysis` 是给人和脚本看数据的入口 —— 形状错了外面就解析不了。"""

    def _ctl(self):
        import importlib.util as iu
        os.environ["WHALE_HOME"] = str(self.home)
        spec = iu.spec_from_file_location("ctl_an", ROOT / "hub" / "hubctl.py")
        ctl = iu.module_from_spec(spec)
        spec.loader.exec_module(ctl)
        return ctl

    def test_json输出已解析且形状与接口一致(self):
        self.h.analysis_ingest({"analysis": GOOD, "engine": "cli-model"})
        ctl = self._ctl()

        class A:
            limit = 1
            json = True

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ctl.cmd_analysis(A())
        items = json.loads(buf.getvalue())
        self.assertEqual(len(items), 1)
        self.assertIsInstance(items[0]["analysis"], dict, "★ --json 要给脚本可解析的对象，不是 JSON 字符串")
        self.assertEqual(items[0]["analysis"]["values"][0]["id"], "screen_active_minutes")
        self.assertEqual(items[0]["engine"], "cli-model")

    def test_没数据时给人话而不是报错(self):
        ctl = self._ctl()

        class A:
            limit = 1
            json = False

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ctl.cmd_analysis(A())
        self.assertIn("还没有分析结果", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
