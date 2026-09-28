#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""鲸鲸的「分析出口」写端 —— 让 **AI 分析数据**，然后把**分析结果（数据）**发出去。

和 `whale_speaker.py`（她的嘴）的分工，一句话说清：

    whale_speaker.py  → 吃脱敏上下文 → 让模型写**句子** → 发微信   （给人看）
    whale_analyze.py  → 吃**同一份**脱敏上下文 → 让模型算**数据** → 发分析出口（给机器看）

所以这里**刻意不许模型写句子**：产物是 JSON（数值 / 方向 / 异常 / 相关 / 评分 / 标签），
一个字段都不做"话术包装"。这不是风格问题，是这个出口存在的全部意义。

## 格式是**规范**，不是"尽力而为"

产物的格式定死在 **`docs/analysis.schema.json`**（human 版：`docs/ANALYSIS-FORMAT.md`）。
遵守方式是**机制**，三层，一层都不靠自觉：

  ① **提示词**里写死 schema，并明确禁止句子/缺席补数；
  ② 本地 `normalize()` 把模型输出**收进规范**（不在上下文里的指标 id → 丢掉并记账；
     标签超长/带句读 → 裁掉；缺字段 → 不补，直接不要这一项）；
  ③ **发之前先干跑中枢的校验器**（`POST /analysis?validate=1`）—— 那是规范的**权威实现**。
     不合规就把错误列表**回喂给模型重写一次**；还不合规就**不发**（宁可不发，不发脏数据）。

三条硬规矩（改之前先读）：
  ① **只吃 `/llm-preview` 的 `would_send_to_model`**，不另开数据源 ——
     那份东西已经过了中枢的脱敏闸。多喂一份没脱敏的数据 = 把整条隐私链的努力作废。
  ② **不许编数据**：指标 id 必须能在上下文里找到（找不到就丢），
     数值若对不上上下文就标 `unverified=true`。这条用户最在意：「宁标未查到，不编造」。
  ③ **模型不写句子**：字符串只能是短标签（≤24 字、不含句读）—— 规范与校验器都会拦。

用法：
    python3 whale_analyze.py --now            # 跑一次并 POST 到中枢（默认）
    python3 whale_analyze.py --now --print    # 跑一次，打印结果（仍会发）
    python3 whale_analyze.py --now --dry      # 只分析+校验，不发
    python3 whale_analyze.py --show -n 3      # 看中枢里最近 3 份分析
    python3 whale_analyze.py --now --digest   # 只打印**一屏数据**（定时任务+通知用这个）
    python3 whale_analyze.py --now --digest --quiet   # stdout 只留数据，进度走 stderr
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time

BASE = pathlib.Path(os.getenv("WHALE_ANALYZE_DIR", "/home/ubuntu/.hermes/scripts"))
sys.path.insert(0, str(BASE))

LOCAL_LOG = BASE / ".whale_analysis.jsonl"      # 本地留痕（最近 20 份），断网也能回看
LOCAL_KEEP = 20

SCHEMA_URL = "/analysis/schema"                 # 规范（机器可读）由中枢提供

#: --quiet：进度行改走 stderr，stdout 只留数据摘要（定时任务靠这个拿到干净输出）
QUIET = [False]


def say(msg):
    print(msg, file=sys.stderr if QUIET[0] else sys.stdout)

#: 顶层字段白名单（与规范一致）
TOP_KEYS = ("v", "day", "values", "trends", "outliers", "pairs", "scores", "tags", "notes")

PROMPT_SYSTEM = """你是一个**数据分析器**，不是聊天助手，也不是助手人格。

你的唯一产物是一个 JSON 对象（UTF-8，无 markdown 代码块，无任何解释文字）。

绝对禁止：
- 禁止写任何句子、问候、称呼、建议、安慰、嘱咐（例如"记得早点休息"）。
- 禁止编造数据里没有的指标。没有的项**直接不要**，不要猜、不要补。
- 禁止输出推理过程、注释或多余顶层字段。

`id` 字段必须**逐字等于**输入里出现过的键名（点号路径或叶子键都认），否则这一项会被丢弃。
所有字符串只能是**短标签**：不超过 12 个字，不含 。，！？；：、和换行。
所有数值必须是数字本身（不要带单位后缀，单位放 unit 字段）。

输出 schema（字段可缺，但不许出现上面的顶层键之外的键）：
{
  "v": 1,
  "day": "YYYY-MM-DD",
  "values":   [{"id": "<输入里的键>", "v": 数字, "unit": "min|count|bpm|percent|score"}],
  "trends":   [{"id": "<键>", "dir": "up|down|flat", "delta_pct": 数字, "vs": "短标签", "conf": "low|mid|high"}],
  "outliers": [{"id": "<键>", "side": "high|low", "z": 数字, "conf": "low|mid|high"}],
  "pairs":    [{"a": "<键>", "b": "<键>", "rho": -1到1, "n": 天数, "conf": "low|mid|high"}],
  "scores":   [{"id": "短标签（如 作息规律/负荷）", "v": 0-100, "of": 100}],
  "tags":     ["短标签"],
  "notes":    ["数据侧说明（缺了哪项等），不是嘱咐"]
}

置信度规矩：算某条用的样本天数 n < 5 时，conf 只能是 "low"。"""


def _speaker():
    """复用 whale_speaker 的 hub()/llm()（同一份模型配置与模型画像适配 —— 不再维护第二套）。

    `--quiet` 时把说话层的调试行**改道到 stderr**（不静音，方便排障；只是别污染 stdout 的数据）。
    """
    import whale_speaker as w
    if QUIET[0]:
        w._dbg = lambda *a, **k: print("[speak]", *a, file=sys.stderr, flush=True)
    return w


# ────────────────────────────── 输入：脱敏上下文 ──────────────────────────────
def fetch_context():
    """取**她本来就能看到**的那份脱敏上下文（红线①：不另开数据源）。"""
    w = _speaker()
    prev = w.hub("/llm-preview") or {}
    ctx = prev.get("would_send_to_model") or {}
    if not isinstance(ctx, dict) or not ctx:
        raise RuntimeError("中枢没给 would_send_to_model（库空？还是 /llm-preview 挂了？）")
    return ctx, (prev.get("what_model_never_sees") or [])


def flatten(ctx):
    """把嵌套上下文摊成 (可用键集合, 数值集合, 键=值行)。用于**可溯源校验**。"""
    keys, nums, lines = set(), set(), []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                p = "%s.%s" % (path, k) if path else str(k)
                keys.add(str(k))
                keys.add(p)
                walk(v, p)
        elif isinstance(node, list):
            for v in node[:20]:
                walk(v, path)
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            nums.add(float(node))
            lines.append("%s=%s" % (path, node))
        elif isinstance(node, str):
            lines.append("%s=%s" % (path, node[:24]))

    walk(ctx, "")
    # ★ 去重（2026-09-28 实测）：同一个路径在列表里出现多次（每节课都有 classes.start_hour
    #   之类），会重复成多行 —— 模型看到重复键会把它当成**数据有问题**写进 notes
    #   （实测原话："classes.start_hour等键重复出现"）。去重后上下文更干净、也更省 token。
    return keys, nums, list(dict.fromkeys(lines))


# ────────────────────────────── 模型调用 ──────────────────────────────
def _strip_fence(text):
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    i, j = t.find("{"), t.rfind("}")
    return t[i:j + 1] if i >= 0 and j > i else t


def ask_model(ctx, day, fix_hint=""):
    """让模型把上下文算成 JSON。返回 (obj, model_name)；失败抛异常（**绝不兜底成假数据**）。"""
    w = _speaker()
    lines = flatten(ctx)[2]
    user = ("日期：%s\n\n以下是已经脱敏的全部可用数据（键=值）：\n%s\n\n"
            "请按 schema 输出分析 JSON。只写能从这些数据里算出来的项。"
            % (day, "\n".join(lines[:400])))
    if fix_hint:
        user += ("\n\n★ 你上一次的输出不符合格式规范，校验器报了下面这些错，请重新输出一版完整 JSON：\n"
                 + fix_hint[:1500])
    out = w.llm([{"role": "system", "content": PROMPT_SYSTEM},
                 {"role": "user", "content": user}],
                timeout=60, max_tokens=1200, temperature=0.3, retries=2)
    if not out:
        raise RuntimeError("模型没有返回内容（空回复/截断；看说话层日志里的模型画像那行）")
    obj = json.loads(_strip_fence(out))
    if not isinstance(obj, dict):
        raise RuntimeError("模型返回的不是一个 JSON 对象：%s" % str(obj)[:80])
    try:
        import yaml
        m = (yaml.safe_load(w.CONFIG.read_text(encoding="utf-8")) or {}).get("model") or {}
        name = m.get("default") or "unknown"
    except Exception:
        name = "unknown"
    return obj, name


# ────────────────────────────── 收进规范（红线②③）──────────────────────────────
_SENT = "。，！？；：、\n\r"
_ID_BAD = set(' \t\r\n。，！？；：、"\'（）()[]【】{}<>《》/\\|')


def _lbl(x, limit=24):
    """收成短标签：去句读、去空白、限长。空掉就返回 ""。"""
    s = re.sub("[%s]+" % re.escape(_SENT), "", str(x or "").strip())
    return s[:limit].strip()


def _mid(x, keys):
    """指标 id 必须可溯源（与上下文键逐字相同）；不可溯源 → ""。"""
    s = str(x or "").strip()
    if not s or len(s) > 48 or any(c in _ID_BAD for c in s):
        return ""
    return s if (s in keys or s.split(".")[-1] in keys) else ""


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def normalize(obj, ctx):
    """把模型输出**收进规范**。返回 (clean, report)。

    规范要求什么就产出什么（不补字段、不发明数据）：
      ① 顶层只留白名单键 ② id 不可溯源 → 丢 ③ 标签统一裁成短标签
      ④ 数值对不上上下文 → 标 unverified（不删：均值/差值这类派生值合法）
    """
    keys, nums, _ = flatten(ctx)
    rep = {"dropped_keys": [], "dropped_items": [], "unverified": 0}
    clean = {"v": 1, "day": str(obj.get("day") or "")[:10] or time.strftime("%Y-%m-%d")}

    for k in obj:
        if k not in TOP_KEYS:
            rep["dropped_keys"].append(str(k))

    def take(k, n=40):
        v = obj.get(k)
        return v[:n] if isinstance(v, list) else []

    for r in take("values"):
        if not isinstance(r, dict):
            continue
        i = _mid(r.get("id"), keys)
        val = _num(r.get("v"))
        if not i or val is None:
            rep["dropped_items"].append("values:%s" % r.get("id"))
            continue
        it = {"id": i, "v": val}
        u = _lbl(r.get("unit"), 16)
        if u:
            it["unit"] = u
        if not any(abs(val - n) <= max(1.0, abs(n) * 0.02) for n in nums):
            it["unverified"] = True
            rep["unverified"] += 1
        clean.setdefault("values", []).append(it)

    for r in take("trends"):
        if not isinstance(r, dict):
            continue
        i, d = _mid(r.get("id"), keys), str(r.get("dir") or "").strip().lower()
        if not i or d not in ("up", "down", "flat"):
            rep["dropped_items"].append("trends:%s" % r.get("id"))
            continue
        it = {"id": i, "dir": d}
        if _num(r.get("delta_pct")) is not None:
            it["delta_pct"] = _num(r.get("delta_pct"))
        if _lbl(r.get("vs")):
            it["vs"] = _lbl(r.get("vs"))
        if str(r.get("conf")) in ("low", "mid", "high"):
            it["conf"] = r["conf"]
        clean.setdefault("trends", []).append(it)

    for r in take("outliers"):
        if not isinstance(r, dict):
            continue
        i, s = _mid(r.get("id"), keys), str(r.get("side") or "").strip().lower()
        if not i or s not in ("high", "low"):
            rep["dropped_items"].append("outliers:%s" % r.get("id"))
            continue
        it = {"id": i, "side": s}
        if _num(r.get("z")) is not None:
            it["z"] = _num(r.get("z"))
        if str(r.get("conf")) in ("low", "mid", "high"):
            it["conf"] = r["conf"]
        clean.setdefault("outliers", []).append(it)

    for r in take("pairs", 20):
        if not isinstance(r, dict):
            continue
        a, b = _mid(r.get("a"), keys), _mid(r.get("b"), keys)
        if not a or not b:
            rep["dropped_items"].append("pairs:%s~%s" % (r.get("a"), r.get("b")))
            continue
        it = {"a": a, "b": b}
        if _num(r.get("rho")) is not None:
            it["rho"] = max(-1.0, min(1.0, _num(r.get("rho"))))
        n = _num(r.get("n"))
        it["n"] = int(n) if n and n >= 1 else 1
        if str(r.get("conf")) in ("low", "mid", "high"):
            it["conf"] = r["conf"]
        clean.setdefault("pairs", []).append(it)

    for r in take("scores", 20):
        if not isinstance(r, dict):
            continue
        lbl, val = _lbl(r.get("id"), 24), _num(r.get("v"))
        if not lbl or val is None:
            rep["dropped_items"].append("scores:%s" % r.get("id"))
            continue
        it = {"id": lbl, "v": max(0.0, min(100.0, val))}
        of = _num(r.get("of"))
        it["of"] = of if of and of >= 1 else 100
        clean.setdefault("scores", []).append(it)

    for src, dst, mx in (("tags", "tags", 12), ("notes", "notes", 6)):
        vals = [_lbl(x, 24) for x in take(src, mx)]
        vals = [v for v in vals if v]
        if vals:
            clean[dst] = vals
    return clean, rep


# ────────────────────────── 规范校验：走中枢的权威实现 ──────────────────────────
def spec_check(analysis):
    """**干跑中枢的校验器**（`POST /analysis?validate=1`）—— 规范只有一份权威实现。"""
    w = _speaker()
    r = w.hub("/analysis?validate=1", {"analysis": analysis})
    return list(r.get("errors") or []), bool(r.get("ok"))


# ────────────────────────────── 落本地 + 发中枢 ──────────────────────────────
def save_local(rec):
    try:
        lines = LOCAL_LOG.read_text(encoding="utf-8").splitlines() if LOCAL_LOG.exists() else []
        lines.append(json.dumps(rec, ensure_ascii=False))
        LOCAL_LOG.write_text("\n".join(lines[-LOCAL_KEEP:]) + "\n", encoding="utf-8")
    except Exception as e:
        say("  本地留痕写不进去（不影响发送）：%s" % str(e)[:60])


def run_once(do_post=True, do_print=False, do_digest=False, day=None):
    w = _speaker()
    day = day or time.strftime("%Y-%m-%d")
    ctx, never = fetch_context()
    keys, _, lines = flatten(ctx)
    say("  上下文 %d 个顶层块 · 模型看到 %d 行 · 可用键 %d 个（屏蔽 %d 项）"
          % (len(ctx), len(lines), len(keys), len(never)))

    hint, clean, rep, model, errs = "", None, {}, "unknown", []
    for attempt in (1, 2):
        obj, model = ask_model(ctx, day, hint)
        clean, rep = normalize(obj, ctx)
        errs, ok = spec_check(clean)
        n = sum(len(clean.get(k) or []) for k in ("values", "trends", "outliers", "pairs", "scores"))
        say("  第 %d 次：模型 %s → 合规 %d 条（丢不可溯源 %d / 丢越界键 %d / 值未对上 %d）规范校验%s"
              % (attempt, model, n, len(rep["dropped_items"]), len(rep["dropped_keys"]),
                 rep["unverified"], "通过 ✓" if ok else "不过 ✗ %d 条" % len(errs)))
        if ok:
            break
        hint = "\n".join("- " + e for e in errs[:12])

    if errs:
        say("  ✗ 两次都不符合规范，**不发**（宁可不发，不发脏数据）：")
        for e in errs[:8]:
            say("      - %s" % e)
        return None

    rec = {"day": clean.get("day") or day, "engine": model,
           "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "analysis": clean, "audit": rep}
    save_local(rec)
    if do_digest:
        # ★ 摘要必须走 **stdout**（`--quiet` 下 say() 是给 stderr 的）—— cron 只接 stdout
        print(digest(rec), flush=True)
    if do_print:
        say(json.dumps(clean, ensure_ascii=False, indent=2))
    if not do_post:
        say("  --dry：没有发出去")
        return rec
    res = w.hub("/analysis", {"analysis": clean, "engine": model, "day": clean.get("day") or day})
    say("  已发 /analysis → %s" % json.dumps(res, ensure_ascii=False)[:240])
    return rec


def digest(rec):
    """紧凑的**数据**摘要（一行一组，仍然不写句子）。

    为什么要有它：定时任务（cron）要往外发的是"这次算出了什么"，而不是一大段 JSON ——
    聊天/通知里塞 5 段缩进 JSON 没人看。这里只做**排版**，不做话术：没有称呼、没有建议、
    没有连接词，读起来就是一屏数据。（真正的产物仍然是那份规范 JSON，见 --print / GET /analysis）
    """
    a = (rec or {}).get("analysis") or {}
    out = ["%s · %s" % (a.get("day") or "-", (rec or {}).get("engine") or "-")]

    def f(v):
        return ("%.2f" % v).rstrip("0").rstrip(".") if isinstance(v, float) else str(v)

    for k, rows in (("values", a.get("values")), ("trends", a.get("trends")),
                    ("outliers", a.get("outliers")), ("pairs", a.get("pairs")),
                    ("scores", a.get("scores"))):
        rows = rows or []
        if not rows:
            continue
        if k == "values":
            s = " · ".join("%s=%s%s" % (r.get("id"), f(r.get("v")), r.get("unit") or "") for r in rows[:8])
        elif k == "trends":
            s = " · ".join("%s%s%.0f%%(%s)" % (r.get("id"), {"up": "↑", "down": "↓", "flat": "="}.get(r.get("dir"), "?"),
                                               r.get("delta_pct") or 0, r.get("vs") or "-") for r in rows[:6])
        elif k == "outliers":
            s = " · ".join("%s %s z=%s" % (r.get("id"), r.get("side"), f(r.get("z") or 0)) for r in rows[:6])
        elif k == "pairs":
            s = " · ".join("%s~%s rho=%s n=%s" % (r.get("a"), r.get("b"), f(r.get("rho") or 0), r.get("n"))
                           for r in rows[:6])
        else:
            s = " · ".join("%s %s/%s" % (r.get("id"), f(r.get("v")), f(r.get("of") or 100)) for r in rows[:6])
        out.append("%s: %s" % (k, s))
    for k in ("tags", "notes"):
        if a.get(k):
            out.append("%s: %s" % (k, " · ".join(a[k])))
    n = sum(1 for r in (a.get("values") or []) if r.get("unverified"))
    if n:
        out.append("unverified: %d" % n)
    au = (rec or {}).get("audit") or {}
    if au.get("dropped_items"):
        out.append("丢弃(不可溯源): %d" % len(au["dropped_items"]))
    return "\n".join(out)


def show(n=1, schema=False):
    w = _speaker()
    if schema:
        d = w.hub(SCHEMA_URL)
        print(json.dumps(d, ensure_ascii=False, indent=2)[:4000])
        return
    d = w.hub("/analysis?limit=%d" % max(1, int(n)))
    for it in d.get("items") or []:
        print("  #%s %s  引擎 %s" % (it.get("id"), str(it.get("ts"))[:16], it.get("engine") or "-"))
        print("     %s" % json.dumps(it.get("analysis"), ensure_ascii=False)[:600])
    if not (d.get("items") or []):
        print("  还没有分析结果（跑一次：python3 whale_analyze.py --now）")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--schema" in argv:
        return show(schema=True)
    if "--show" in argv:
        n = 1
        if "-n" in argv:
            try:
                n = int(argv[argv.index("-n") + 1])
            except Exception:
                n = 1
        return show(n)
    QUIET[0] = "--quiet" in argv
    if "--now" not in argv:
        print(__doc__)
        return 0
    return 0 if run_once(do_post=("--dry" not in argv), do_print=("--print" in argv),
                         do_digest=("--digest" in argv)) else 1


if __name__ == "__main__":
    sys.exit(main())
