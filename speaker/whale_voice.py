#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""鲸鲸的"嘴"：把冷冰冰的事实改写成她自己的说法（角色卡 + few-shot + 去重复 + 重试）。

为什么放在这台机器上：API key 只在本机（不落服务器）。中枢只负责"算出该说什么事实"，
说什么话由这里现场生成；模型不可用时自动回落到中枢给的模板句 —— 宁可话朴素，也不能不提醒。

用法：
    from whale_voice import speak, speak_many
    speak("心率 150，偏高")                    # → 一句话
    speak_many(["今天 3 节课", "电脑磁盘只剩 1.2%"])   # → 合成一条人话

★★ 2026-09-29 修掉的两个"让她不说人话"的根因（实测数据，不是推测）：

  ① **token 预算太少 → 正文永远是空的 → 100% 回落中枢模板**
     这里原来写死 `max_tokens=160`。线上模型是**思考型**（deepseek-v4.1-flash）：
     实测同一句事实，max_tokens=160 → 思考吃掉 428 字、`finish_reason=length`、`content=''`；
     给 2400 → 正常输出"（默默记下）主人今天 3 节课，头一节 08:00 电气控制与PLC，在 13-908…"。
     ⇒ 于是 68 条真实语料里，所有提醒都是**中枢模板原文**（"主人早；明早 08:00 有课…；
       pc 的数据已经 158 小时没同步了，看一眼设备；第一节 08:00 在 7-305"），一次都没被"说人话"。
     现在预算走 `model_profile`（思考型 2400 / 非思考型 400），并看 `finish_reason` 判断截断后加倍重试。

  ② **质量闸要求正文里必须出现"（"** → 逼她每条硬凑一个"（看了下数据）"前缀
     ⇒ 语料里满是"（翻了翻磁盘）/（算了下今天）"这种模板腔，而一句自然的
       "主人，C 盘只剩 1.5% 了" 会被判不合格、重试三次、然后回落模板。
     现在"（动作或情绪）"是**可选**的，改成**负向守卫**：写了她做不到的物理动作（递水/拉窗帘/
     塞手机/关灯…）才判不合格。质量闸该拦"不像人话的假话"，而不是逼人凑格式。
"""
import json
import os
import pathlib
import re
import time
import urllib.request


def _scripts_dir() -> pathlib.Path:
    """说话层要读的角色卡/配置在哪。

    优先级：WHALE_HOME → /opt/whale（旧部署默认）→ ~/.whale
    """
    home = os.getenv("WHALE_HOME")
    if home:
        h = pathlib.Path(home).expanduser()
        # 两种常见布局都认：$WHALE_HOME/.hermes/scripts 或 $WHALE_HOME/scripts
        for sub in (h / ".hermes" / "scripts", h / "scripts", h):
            if sub.exists():
                return sub
        return h / ".hermes" / "scripts"
    legacy = pathlib.Path("/opt/whale/.hermes/scripts")
    if legacy.exists():
        return legacy
    return pathlib.Path.home() / ".whale"


SCRIPTS = _scripts_dir()
CARD_PATH = pathlib.Path(os.getenv("WHALE_CARD", SCRIPTS / "whale_card.json"))
RECENT_PATH = SCRIPTS / ".whale_said.jsonl"   # 最近说过的话（防重复）
CONFIG = pathlib.Path(os.getenv("WHALE_CONFIG", SCRIPTS.parent / "config.yaml"))


# 中枢配置文件（用于兜底捞 token）—— 与中枢 _resolve_home() 的口径保持一致
def _hub_cfg_path() -> pathlib.Path:
    import os as _os
    h = _os.getenv("WHALE_HOME")
    if h:
        return pathlib.Path(h).expanduser() / "hub.json"
    legacy = pathlib.Path("/opt/whale/hub/hub.json")
    if legacy.exists():
        return legacy
    return pathlib.Path.home() / ".whale" / "hub.json"


BASE_CFG = _hub_cfg_path()
RECENT_KEEP = 6
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

# 她做不到的事：出现这些词 = 假动作，直接判不合格（比"必须带括号"有用得多）
FAKE_ACTIONS = ("递水", "递上", "递来", "拉窗帘", "拉上窗帘", "掀开", "拍你", "拍拍你",
                "塞手机", "关灯", "盖被子", "帮你关", "给你倒", "端过来", "抱抱你",
                "摸摸头", "揉", "扶你", "系好")
# 中枢模板里的内部注释：对人没有意义，念出来就是"不像人"
INTERNAL_NOTE = re.compile(r"（(?:先按默认[^）]*|攒几天[^）]*|数据依赖[^）]*|可选[^）]*)）")
# 合并简报时要去掉的列表符号（人不会用"· "说话）
BULLET = re.compile(r"^\s*[·•\-*]\s*", re.M)


def _dbg(*a):
    if os.getenv("WHALE_DEBUG"):
        print("[voice]", *a, flush=True)


def strip_internal(s: str) -> str:
    """去掉面向运维的内部注释（如"（先按默认，攒几天睡眠数据就更准）"）与列表符号。"""
    s = INTERNAL_NOTE.sub("", s or "")
    s = BULLET.sub("", s)
    return re.sub(r"[ \t]{2,}", " ", s).strip()


def _llm_conf():
    """读出模型配置。支持 fallback 模型链 —— 主模型挂了不至于整层哑掉。"""
    import yaml
    try:
        m = (yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}).get("model") or {}
    except Exception:
        m = {}
    base = (m.get("base_url") or "").rstrip("/")
    key = m.get("api_key") or os.getenv("OPENCODE_GO_API_KEY") or ""
    primary = m.get("default") or "deepseek-v4.1-flash"

    # fallback 链：配置里显式写 fallback_models；否则至少不把主模型重复一遍
    fb = [x for x in (m.get("fallback_models") or []) if x and x != primary]
    if not fb:
        fb = [x for x in (os.getenv("WHALE_FALLBACK_MODEL") or "", "glm-5.3")
              if x and x != primary]

    return {
        "base": base,
        "key": key,
        "model": primary,
        "models": [primary] + fb,
        "headers": m.get("default_headers") or {},
    }


def _report_degraded(reason: str):
    """连续回落时，把「说话层哑了」这件事推给中枢 —— 用户必须能看见。

    为什么要有这个：以前只有 WHALE_DEBUG 时才打印，用户唯一的感觉是
    "她今天说话变傻了"，完全不知道主模型在报 500。静默降级比崩溃更危险。
    """
    try:
        hub = os.getenv("WHALE_HUB", "http://127.0.0.1:11440")
        tok = os.getenv("WHALE_TOKEN", "")
        if not tok:
            try:
                import yaml as _y
                c = _y.safe_load((pathlib.Path(BASE_CFG)).read_text(encoding="utf-8")) or {}
                tok = c.get("token", "")
            except Exception:
                pass
        if not tok:
            return
        payload = json.dumps({
            "metric": "health.speaker_degraded",
            "value": 1,
            "unit": "flag",
            "meta": {"reason": reason[:200], "model": _llm_conf()["model"]},
        }).encode()
        req = urllib.request.Request(
            hub.rstrip("/") + "/ingest", data=payload,
            headers={"Content-Type": "application/json", "X-Token": tok})
        urllib.request.urlopen(req, timeout=6).read()
        _dbg("已上报 speaker_degraded")
    except Exception as e:
        _dbg("上报降级状态失败：", type(e).__name__, e)


def _recent(n=RECENT_KEEP):
    try:
        lines = RECENT_PATH.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(x).get("text", "") for x in lines if x.strip()]
    except Exception:
        return []


def _remember(text: str):
    try:
        with RECENT_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"text": text}, ensure_ascii=False) + "\n")
        lines = RECENT_PATH.read_text(encoding="utf-8").splitlines()
        if len(lines) > 40:
            RECENT_PATH.write_text("\n".join(lines[-40:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def _clean(s: str, flatten: bool = False) -> str:
    s = (s or "").strip().strip('"「」“”\'')
    s = re.sub(r"^(鲸鲸|助手)[:：]\s*", "", s)
    s = re.sub(r"^```[a-z]*\n?|```$", "", s).strip()          # 偶尔把整句包进代码块
    if flatten:
        s = re.sub(r"\s*\n+\s*", " ", BULLET.sub("", s))
        return re.sub(r"[ \t]{2,}", " ", s).strip()
    return s.split("\n")[0].strip()


def _budget(model: str, base: str, fallback: int = 800) -> int:
    """★ 按模型特点给预算：思考型 2400 / 非思考型 400（见文件头 ①）。"""
    try:
        import model_profile as _mp
        return _mp.profile_for(model, base).budget
    except Exception:
        return fallback


def _build_msgs(card, facts, limit: int, compose: bool):
    msgs = [{"role": "system", "content": card["system_prompt"]}]
    for ex in card.get("mes_example", [])[:5]:
        msgs.append({"role": "user", "content": ex["user"]})
        msgs.append({"role": "assistant", "content": ex["char"]})
    recent = _recent()
    if recent:
        msgs.append({"role": "user",
                     "content": "（最近已经说过这几条，别再用同样的动作和说法）\n" + "\n".join(recent)})
        msgs.append({"role": "assistant", "content": "明白，换动作、换说法。"})
    if compose:
        msgs.append({"role": "user", "content":
                     "[今天的几条事实]\n" + "\n".join("- " + f for f in facts) + "\n"
                     "把它们**合成一条**微信消息：像人一样取舍，别逐条念；同一个东西（同一节课、"
                     "同一个人）只提一次；几条互相矛盾的只留一种说法；"
                     "不要用项目符号，就是一段家常话。整条不超过 %d 字。" % limit})
    else:
        msgs.append({"role": "user", "content": f"[事实] {facts[0]}"})
    return msgs


def _call(conf, card, facts, limit, compose, timeout, budget):
    model = conf["model"]
    prof = None
    try:
        import model_profile as _mp
        prof = _mp.profile_for(model, conf["base"])
    except Exception:
        _mp = None
    body = {"model": model, "messages": _build_msgs(card, facts, limit, compose),
            "temperature": 1.05, "top_p": 0.95}      # 稍微放开一点才有活人味
    if prof is not None:
        _mp.apply_to_body(body, prof, budget, 1.05)
    else:
        body["max_tokens"] = budget
    req = urllib.request.Request(conf["base"] + "/chat/completions",
                                data=json.dumps(body, ensure_ascii=False).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + conf["key"])
    # 必须带 UA：opencode 前面有 Cloudflare，Python 默认 UA 会被 403（error 1010）拦掉
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    for k, v in (conf["headers"] or {}).items():
        req.add_header(k, str(v))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    ch = (data.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    return (_clean(msg.get("content"), flatten=compose),
            ch.get("finish_reason") == "length",
            len(str(msg.get("reasoning_content") or "")))


def _norm(x: str) -> str:
    return str(int(x)).lstrip("0") if x.isdigit() else x.replace(":", "").replace(".", "")


def _ok(text: str, facts, limit: int) -> bool:
    """质量闸：太短/太长/丢关键数字/写了做不到的物理动作 → 不合格。

    ★ 这里**不再要求出现"（"**（原来要求它 → 逼她凑格式 → 满屏"（看了下数据）"，见文件头 ②）。
    """
    if not text or len(text) < 4:
        _dbg(f"闸：太短/空 原文={text!r}")
        return False
    if len(text) > limit:
        _dbg(f"闸：超长 {len(text)} > {limit} 原文={text!r}")
        return False
    for w in FAKE_ACTIONS:
        if w in text:
            _dbg(f"闸：写了做不到的动作「{w}」原文={text!r}")
            return False
    if re.search(r"\[SILENT\]|\[SEARCH", text):
        _dbg("闸：模型吐出了控制指令")
        return False
    fact_nums = {_norm(n) for n in re.findall(r"\d+(?:[.:]\d+)?", " ".join(facts))}
    text_nums = {_norm(n) for n in re.findall(r"\d+(?:[.:]\d+)?", text)}
    for n in fact_nums:
        if len(n) >= 2 and n not in text_nums:      # 只死守"有意义的数字"（如 409）；8 点/08:00 视作同一个
            _dbg(f"闸：丢了数字 {n} 原文={text!r}")
            return False
    return True


def _generate(facts, limit, compose, timeout, tries, remember) -> str:
    """真正的生成：按模型链试，逐稿加倍预算，全失败返回空串。"""
    facts = [strip_internal(f) for f in (facts or []) if str(f or "").strip()]
    if not facts:
        return ""
    try:
        card = json.loads(CARD_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        _dbg("角色卡读不了：", e)
        return ""
    conf = _llm_conf()
    if not conf["base"] or not conf["key"]:
        _dbg("缺 base_url / key")
        return ""

    last_err = ""
    for model in conf["models"]:
        conf_m = dict(conf, model=model)
        budget = _budget(model, conf_m["base"])
        for i in range(tries):
            try:
                text, truncated, rlen = _call(conf_m, card, facts, limit, compose, timeout, budget)
                if text and not truncated and _ok(text, facts, limit):
                    if remember:
                        _remember(text)
                    if model != conf["model"]:
                        _dbg(f"已降级到 {model} 生成成功")
                    return text
                if truncated:
                    _dbg(f"[{model}] 第 {i + 1} 稿被截断（思考 {rlen} 字 · 预算 {budget}）→ 加倍重试")
                    budget *= 3                       # 思考型偶尔会想很久
                else:
                    _dbg(f"[{model}] 第 {i + 1} 稿不合格（思考 {rlen} 字）")
                    break                             # 内容不合格 → 直接换下一个模型，别原地烧钱
            except Exception as e:
                last_err = f"{type(e).__name__}: {str(e)[:120]}"
                _dbg(f"[{model}] 第 {i + 1} 次调用失败：{last_err}")
                try:
                    _dbg("响应体:", e.read().decode()[:200])
                except Exception:
                    pass
            time.sleep(1.0)
        _dbg(f"[{model}] 用尽 {tries} 次")
    _dbg("全部模型都失败 → 回落模板句，并上报降级")
    _report_degraded(last_err or "all models failed")
    return ""


def speak(fact: str, fallback: str = "", timeout: int = 40, remember: bool = True, tries: int = 3) -> str:
    """把一条事实改写成鲸鲸的一句话；失败回落模板句（宁可话朴素，不能不提醒）。"""
    fact = strip_internal(fact)
    text = _generate([fact], 90, False, timeout, tries, remember)
    return text or fallback or fact


def speak_many(facts, fallback: str = "", limit: int = 140, timeout: int = 50,
               remember: bool = True, tries: int = 2) -> str:
    """把**多条事实合成一条人说的人话**（早间/睡前简报、同一轮的多条提醒）。

    为什么必须走模型：这里原来是 `"；".join(facts)` —— 真实语料里因此出现了
    "主人早；明早 08:00 有课（电气控制与PLC），今晚别熬太晚；pc 的数据已经 158 小时没同步了，
    看一眼设备；第一节 08:00 在 7-305" 这种**四段拼接**：8:00 说了三遍、同一件事两种说法、
    还带"～小时没同步"这种运维口吻。机器拼出来的句子再准也不像人说话。
    失败回落 `"；".join(facts)`：信息一条不丢，只是不好听。
    """
    facts = [strip_internal(f) for f in (facts or []) if str(f or "").strip()]
    if not facts:
        return fallback
    if len(facts) == 1:
        return speak(facts[0], fallback=fallback or facts[0], timeout=timeout, remember=remember)
    if len(facts) > 6:
        facts = facts[:6]                      # 太多条模型会念成报告；先留最相关的几条
    text = _generate(facts, limit, True, timeout, tries, remember)
    return text or fallback or "；".join(facts)


def close_once(topic: str, timeout: int = 40) -> str:
    """对"一直没好转的老问题"说一句收口话（"我先不天天念了"）。

    真实语料：电脑没上报这件事被连报 8 天（53→62→86→110→134→158 小时）。天天念同一个
    没变化的问题，价值是负的 —— 说一次收口，比第 9 次报数更像人。失败返回空串（调用方直接静默）。
    """
    return _generate([f"{topic}（连着好几天都是同一个状态、一直没变化）"], 90, False, timeout, 2, False)


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if args and args[0] == "--many":
        print("  " + speak_many(args[1:], remember=False))
    else:
        fact = args[0] if args else "心率 150，偏高"
        n = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
        for i in range(n):
            print(f"  {i + 1}. {speak(fact, remember=False)}")
