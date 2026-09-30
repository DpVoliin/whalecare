#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安全审查（cloudflare/security-audit-skill run-1）两条 confirmed 发现的回归测试。

C3 — `/consent` 是状态写端点，必须在鉴权之后：
     修之前它在 do_POST 里位于 `if not self._auth(q)` **之前**并直接 return，
     于是任何未认证的网络客户端都能翻转健康数据的显式同意
     （granted=true → health.*/sleep.* 开始入库；false → 健康指标被静默丢弃）。

C4 — 凭据绝不能进日志：
     `/api/mcu` 允许在 query 里带 token（给最小设备用）、配对链接带一次性码，
     而基类默认会把整行请求（含 query）打进日志 → token/码就这样落盘。

这两条都是"错了不报错、只是悄悄失守"的类型，所以必须有测试盯住。
"""
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer

REPO = pathlib.Path(__file__).resolve().parents[1]


def load_hub(home: str):
    """在指定 WHALE_HOME 下载入 hub.py（与其它测试同一套做法）。"""
    os.environ["WHALE_HOME"] = home
    import importlib.util
    spec = importlib.util.spec_from_file_location("whalecare_secfix", str(REPO / "hub" / "hub.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    try:
        m.init_db()
    except Exception:
        pass
    return m


class SecFixBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="secfix-")
        cls.h = load_hub(cls.tmp)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), cls.h.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.tok = cls.h.CFG["token"]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def post(self, path, body, token=None):
        data = json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, method="POST")
        r.add_header("Content-Type", "application/json")
        if token:
            r.add_header("X-Token", token)
        try:
            with urllib.request.urlopen(r, timeout=10) as x:
                return x.status, json.loads(x.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()


class TestConsentRequiresAuth(SecFixBase):
    """C3 回归：/consent 不能再被匿名调用。"""

    def test_匿名POST被拒(self):
        st, _ = self.post("/consent", {"what": "health", "granted": True})
        self.assertEqual(st, 401, "匿名翻转健康同意必须被拒（修 C3 的核心断言）")

    def test_带token可以正常用(self):
        st, body = self.post("/consent", {"what": "health", "granted": True}, token=self.tok)
        self.assertEqual(st, 200, "采集器/脚本带 X-Token 时必须照常可用（不能修坏了）")
        self.assertTrue(body.get("ok"))

    def test_改完仍能被拒回来(self):
        self.post("/consent", {"what": "health", "granted": False}, token=self.tok)
        self.assertFalse(self.h.consent_granted("health"), "撤销同意要真的生效（别只测放行）")


class TestLogRedaction(SecFixBase):
    """C4 回归：凭据不能明文落日志。"""

    def _run_handler_with(self, request_line: str) -> str:
        """直接构造一条请求行喂给 log_message，捕获它打出来的字。"""
        handler = self.h.Handler.__new__(self.h.Handler)
        handler.client_address = ("10.0.0.9", 12345)
        buf = io.StringIO()
        with redirect_stdout(buf):
            handler.log_message('"%s" %s %s', request_line, "200", "-")
        return buf.getvalue()

    def test_query里的token被打码(self):
        out = self._run_handler_with(f"GET /api/mcu?d=x&m=y&token={self.tok} HTTP/1.1")
        self.assertNotIn(self.tok, out, "token 明文进了日志（C4 未修好）")
        self.assertIn("token=***", out, "应当打码成 token=***")

    def test_配对码被打码(self):
        code = "PAIR-ABCD1234"
        out = self._run_handler_with(f"GET /pair?code={code} HTTP/1.1")
        self.assertNotIn(code, out, "配对码明文进了日志")
        self.assertIn("code=***", out)

    def test_普通请求行不受影响(self):
        out = self._run_handler_with("GET /health HTTP/1.1")
        self.assertIn("/health", out, "可观测性不能被改没（正常路径照旧记录）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
