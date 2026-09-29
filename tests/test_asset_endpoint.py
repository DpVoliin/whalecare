#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`/asset/<name>` —— 把主人的形象素材从中枢发给 app / 挂件。

为什么要走中枢、而不是把图放进仓库（2026-09-29 定）：
  · 素材是**主人的私有资源** ✗ 公开仓库只留占位图 ✓（既有规矩 ✓）
  · 放中枢能做到"换图不用重新发版" ✓ 也天然只有自己能取 ✓
  · 别人用开源版时取不到 → app 自动回落占位图 ✓（和桌面挂件一个套路 ✓）

这个文件守三件事（都是"错了不报错、但会静默出事"的那类 ✗）：
  ① 鉴权：没 token / token 错 → 401（素材是私有的 ✓ 不能裸奔 ✗）
  ② 名字白名单 + basename：`../` 穿越**必须**拿不到东西 ✓（这个错了就是任意文件读取 ✗✗）
  ③ 真的把**字节**发出去（Content-Type + Content-Length 正确 ✓）——
     接口能"返回 200 但内容不对"是可预期的失败 ✓ 所以这里比对原始字节 ✓
"""
import importlib.util
import os
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
HUB_SRC = ROOT / "hub" / "hub.py"

# 1x1 透明 PNG（最小合法图 ✓ 只用来验字节一致性 ✓）
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


def load_hub(home: pathlib.Path):
    home.mkdir(parents=True, exist_ok=True)
    os.environ["WHALE_HOME"] = str(home)
    spec = importlib.util.spec_from_file_location("whalecare_asset", HUB_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        mod.init_db()
    except Exception:
        pass
    return mod


class TestAssetEndpoint(unittest.TestCase):
    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp(prefix="whale-asset-"))
        self.h = load_hub(self.home)
        self.adir = self.home / "assets"
        self.adir.mkdir(parents=True, exist_ok=True)
        (self.adir / "whale_avatar.png").write_bytes(PNG)
        # 一份"不该被读到"的邻居文件（穿越测试的靶子 ✓）
        (self.home / "hub.json").write_text('{"token":"should-never-leak"}', encoding="utf-8")
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self.h.Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.port}"
        self.tok = self.h.CFG["token"]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _get(self, path, token=None, raw=False):
        r = urllib.request.Request(self.base + path)
        if token is not None:
            r.add_header("X-Token", token)
        try:
            with urllib.request.urlopen(r, timeout=15) as x:
                body = x.read()
                return x.status, (body if raw else body.decode("utf-8", "replace")), x.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), e.headers.get("Content-Type", "")

    # ① 带对的 token → 真的拿到那份字节
    def test_有token时返回原始字节(self):
        code, body, ctype = self._get("/asset/whale_avatar.png", token=self.tok, raw=True)
        self.assertEqual(code, 200)
        self.assertEqual(body, PNG, "★ 返回的字节必须和磁盘上完全一致（否则是静默坏图 ✗）")
        self.assertEqual(ctype, "image/png")

    # ② 没 token / token 错 → 401（素材私有 ✓ 不能裸奔）
    def test_无token或错token一律401(self):
        code, _, _ = self._get("/asset/whale_avatar.png")
        self.assertEqual(code, 401, "不能匿名读取私有素材")
        code2, _, _ = self._get("/asset/whale_avatar.png", token="wrong-token")
        self.assertEqual(code2, 401, "错 token 也必须拒")

    # ③ ★ query token 必须**被拒**（中枢全局只认 header ✓）
    #    为什么这不是缺陷：?t= 会把 token 写进服务器日志、浏览器历史和代理记录 ✗
    #    所以 app 取素材必须走代码带 header ✓ 不能直接塞进 <img src> ✓
    def test_query_token必须被拒(self):
        code, body, _ = self._get(f"/asset/whale_avatar.png?t={self.tok}")
        self.assertEqual(code, 401, "query token 必须拒绝（防止 token 进日志）")
        self.assertNotIn("t=", body)

    # ④ ★ 目录穿越必须拿不到东西（这个错了就是任意文件读取 ✗✗）
    def test_目录穿越拿不到文件(self):
        for bad in ("/asset/../hub.json", "/asset/../../etc/passwd", "/asset/..%2fhub.json"):
            code, body, _ = self._get(bad, token=self.tok)
            self.assertIn(code, (400, 401, 404), f"{bad} 不该成功，实际 {code}")
            self.assertNotIn("should-never-leak", body, f"{bad} 泄漏了邻居文件内容 ✗✗")
            self.assertNotIn("root:", body, f"{bad} 读到了 /etc/passwd ✗✗")

    # ⑤ 文件名不合规 → 400；文件不存在 → 404
    def test_非法名与不存在(self):
        code, _, _ = self._get("/asset/bad;name.png", token=self.tok)
        self.assertEqual(code, 400, "含非法字符的名字应被拒")
        code2, _, _ = self._get("/asset/nope_does_not_exist.png", token=self.tok)
        self.assertEqual(code2, 404, "文件不存在应是 404（好和'没权限'区分开 ✓）")

    # ⑥ 明确不支持的扩展名 → 400（白名单之外一律不放行）
    def test_白名单外的扩展名被拒(self):
        (self.adir / "evil.sh").write_text("#!/bin/sh\necho hi", encoding="utf-8")
        code, body, _ = self._get("/asset/evil.sh", token=self.tok)
        self.assertEqual(code, 400, "白名单外扩展名应被拒")
        self.assertNotIn("echo hi", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
