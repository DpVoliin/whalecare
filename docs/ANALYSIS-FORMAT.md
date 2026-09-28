# 分析出口 · 数据格式规范（v1）

> 机器可读的正式定义：[`docs/analysis.schema.json`](analysis.schema.json)（JSON Schema 2020-12）。
> 本文是它的**人读版**：说明为什么这样定、每个字段什么意思、怎么校验、常见错怎么改。
> 两份必须一致 —— 中枢单文件里嵌了规范的原文，`tests/test_analysis_schema.py` 会在漂移时立刻报红。

## 这是什么

whalecare 有 **9 个出口**。前 8 个发的都是**话**（自然语言，给人看，会拼问候、会截断到 1800 字）。
第 9 个「分析出口」发的是**数据**：它是**让 AI 分析处理数据之后，把分析结果本身发出去**，
**而不是句子** —— 给**机器**消费（你自己的看板、脚本、挂件、单片机）。

一句话对比：

| | 说话层（前 8 个出口） | 分析出口（第 9 个） |
|---|---|---|
| 产物 | 句子：「主人，今天屏幕用得好久呀，起来走走吧」 | 数据：`{"id":"screen_active_minutes","v":554,"unit":"min"}` |
| 谁看 | 人 | 程序 |
| 加工 | 会做话术包装、语气、称呼 | **一个字都不加工**，原样 JSON |
| 格式约束 | 靠人设与提示词 | **规范 + 硬门禁**（不合规拒收） |

## 数据怎么流

```
中枢 /llm-preview ──(已脱敏)──▶ 说话层 whale_analyze.py
                                     │ 让模型把数据算成 JSON（禁止写句子）
                                     ▼
                            收进规范 + 干跑中枢校验器（不合规 → 回喂错误重写一次）
                                     ▼
                       POST /analysis ──▶ 中枢：按规范**硬校验**
                                     │        ├─ 不合规 → 拒收（不落库、不分发）+ 审计
                                     │        └─ 合规   → 落库 analyses 表
                                     ▼
                        分发到 channels.analysis_webhook / channels.analysis_file
                        或由消费者自己拉：GET /analysis
```

**隐私边界**：喂给模型的仍然只有 `/llm-preview` 那份**脱敏后**的上下文
（不含通知原文、日程标题、App 名、分钟级时间、定位）—— 分析出口没有、也不会另开数据源。
产物里出现的指标 `id` 必须是那份上下文里**真实存在的键**（可溯源），
所以"分析出口"不会变成一条绕过脱敏的新管道。详见 [`PRIVACY.md`](PRIVACY.md)。

## 顶层信封

`POST /analysis` 的 body（也可见于 webhook / 文件里的同名结构）：

```json
{
  "analysis": { ...本规范描述的 v1 对象... },
  "engine": "deepseek-v4.1-flash",
  "day": "2026-09-28"
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `analysis` | ✅ | 规范本体（下面的字段都在这一层里）。也可以把本体直接当 body 传 |
| `engine` | 否 | 谁产的（模型名 / 脚本名）。**建议给** —— 数字对不上时靠它对账 |
| `day` | 否 | 缺省取 `analysis.day`，再缺省取服务端当天 |

出口发出去的信封（webhook / 文件）长这样，多一层元数据、**没有句子**：

```json
{ "type": "analysis", "schema": 1, "at": "2026-09-28T21:04:11", "day": "2026-09-28",
  "engine": "deepseek-v4.1-flash", "analysis": { "...": "..." } }
```

## 规范本体（v1）

顶层是一个 JSON 对象，**只允许**下面这些键；`v` 必填且必须等于 `1`。

| 键 | 类型 | 上限 | 说明 |
|---|---|---|---|
| `v` | 整数 | — | **必填**，固定 `1`。不认识的版本，读方应拒收 |
| `day` | 字符串 | — | `YYYY-MM-DD` |
| `values` | 数组 | 40 | 指标现值 |
| `trends` | 数组 | 40 | 趋势（相对基线） |
| `outliers` | 数组 | 40 | 异常点 |
| `pairs` | 数组 | 20 | 两指标共变 |
| `scores` | 数组 | 20 | 派生评分 |
| `tags` | 数组 | 12 | 状态标签（短词） |
| `notes` | 数组 | 6 | 数据侧说明（**不是**嘱咐） |

### 两种字符串，规矩不同（这是本规范最要紧的一处）

- **指标 id**（`values[].id` / `trends[].id` / `outliers[].id` / `pairs[].a|b`）
  必须与中枢 `llm_context()` 里的键**逐字相同**（点号路径或叶子键都认），
  1~48 字，**不含空白、句读、引号、括号**。允许中文 —— 游戏名、分类名本身就是合法的键
  （`"明日方舟"`、`"短视频"`），不许为了"看起来像程序"把它改写成拼音或英文，那样溯源就断了。
- **短标签**（`tags[]` / `notes[]` / `scores[].id` / `unit` / `vs`）
  1~24 字，**不含句读** `。，！？；：、`、不含换行。这是刻意的：标签里塞进一句嘱咐，
  下游就会开始渲染"话"，这个出口的意义就没了。

### 各数组的条目

```jsonc
"values":   [{"id": "<键>", "v": 554, "unit": "min", "unverified": false}]
//                                                      ↑ 可选：数值对不上上下文时由写端标 true
"trends":   [{"id": "<键>", "dir": "up|down|flat", "delta_pct": 14, "vs": "7日均值", "conf": "mid"}]
"outliers": [{"id": "<键>", "side": "high|low", "z": 2.1, "conf": "mid"}]
"pairs":    [{"a": "<键>", "b": "<键>", "rho": 0.62, "n": 9, "conf": "mid"}]
"scores":   [{"id": "作息规律", "v": 72, "of": 100}]
```

字段细则：

| 字段 | 必填 | 取值 | 说明 |
|---|---|---|---|
| `values[].v` | ✅ | 数字 | **必须是数字本身**，不带单位后缀（单位放 `unit`） |
| `values[].unit` | 否 | 标签 | `min` / `count` / `bpm` / `percent` / `score`… |
| `values[].unverified` | 否 | 布尔 | 写端对不上上下文时标 `true`；读端应视作**弱证据** |
| `trends[].dir` | ✅ | `up`/`down`/`flat` | 方向必须由数值算出，不是形容 |
| `trends[].delta_pct` | 否 | 数字 | 相对变化百分比（`+12` = 涨 12%） |
| `trends[].vs` | 否 | 标签 | 基线是什么（如 `7日均值`） |
| `outliers[].side` | ✅ | `high`/`low` | 偏高还是偏低 |
| `outliers[].z` | 否 | 数字 | 标准分（不是主观判断） |
| `pairs[].rho` | 否 | −1 ~ 1 | 相关系数 |
| `pairs[].n` | 否 | ≥1 整数 | **样本天数**。没有 `n` 的相关系数是耍流氓 —— 强烈建议给 |
| `scores[].v` / `of` | `v` ✅ | 0~100 / ≥1 | `of` 缺省 100 |
| `conf`（各处） | 否 | `low`/`mid`/`high` | **样本天数 n < 5 必须是 `low`** |

## 硬门禁：不合规会怎样

中枢在 `POST /analysis` 上做**硬校验**（零依赖实现，定义在 `hub/src/whalecare/93_channels.py`）：

- **不合规 → 拒收**：既不落库、也不分发给任何出口，返回里带上**逐条错误**，并写一条 `analysis_reject` 审计。
- **合规 → 落库**（`analyses` 表，默认保留最近 50 份，`channels.analysis_keep` 可调）+ 分发。
- 想先自查：`POST /analysis?validate=1` 只校验不落库（写端就是这么干的）。
- 取规范本身：`GET /analysis/schema`（返回 JSON Schema 原文，消费者不必翻仓库）。
- 体积上限 64KB（它是数据，不该长成散文）。

> 为什么是"拒收"而不是"警告"：格式是这个出口的**契约**。
> 放一段话进去会污染所有下游（看板、脚本、设备），而且事后没法补救 ——
> 宁可少一份分析，也不要一份脏的。

## 怎么自己验证

```bash
# 1) 干跑校验（不发）
curl -s -H "X-Token: $T" -H 'Content-Type: application/json' \
  -d "$(cat my-analysis.json)" https://<中枢>:11443/analysis?validate=1 | python3 -m json.tool

# 2) 取规范
curl -s -H "X-Token: $T" https://<中枢>:11443/analysis/schema | python3 -m json.tool

# 3) 真发
curl -s -H "X-Token: $T" -H 'Content-Type: application/json' \
  -d "$(cat my-analysis.json)" https://<中枢>:11443/analysis

# 4) 拉最近几份（挂件/脚本用这个）
curl -s -H "X-Token: $T" "https://<中枢>:11443/analysis?limit=3" | python3 -m json.tool

# 5) 本地用标准库校验（jsonschema 不是依赖，按需自装）
python3 -c "import json,jsonschema;jsonschema.validate(json.load(open('my-analysis.json'))['analysis'],json.load(open('docs/analysis.schema.json')));print('合规')"
```

命令行看结果：`hubctl analysis`（最近一份）/ `hubctl analysis -n 5 --json`（给脚本用）。

## 定时跑（每天一次）

分析不必手动点。有两种跑法，按"谁来消费"选：

```bash
# ① 手动跑一次：分析 + 入库（+ 分发到已配置的出口）
python3 speaker/whale_analyze.py --now

# ② 只打印**一屏数据**（通知 / 日志用；仍然不写句子，只是排版）
python3 speaker/whale_analyze.py --now --digest

# ③ stdout 只留数据、进度走 stderr —— cron 用这个，整段输出可以直接当消息发出去
python3 speaker/whale_analyze.py --now --digest --quiet
```

`--digest` 长这样（真实输出，一行一组，没有称呼也没有建议）：

```
2026-09-28 · deepseek-v4.1-flash
values: tracks_today_count=29count · screen_total_minutes_today=285min · weather_now.humidity=56percent
scores: 屏幕娱乐占比 94/100 · 数据完整度 35/100
tags: 晴 · 高温 · 上课日 · 数据缺失 · 短视频为主
notes: 电脑源今日无上报 · 手机源仅39条上报 · 仅单日数据无法算趋势与相关
```

**cron 一行**（放在**跑说话层的那台机器**上 —— 模型 key 在那儿）：

```cron
40 22 * * * cd /home/ubuntu/.hermes/scripts && . ./.whale_env && python3 whale_analyze.py --now --digest --quiet >> /tmp/whale_analyze.log 2>&1
```

**退出码**：0 = 成功入库；非 0 = 这一轮没成（模型没返回 / 两次都不符合规范）/ 进程异常 ——
所以把它当**看门狗**用：只在出错时才需要报警，正常时它会照常打一屏数据。

**定时跑的三条注意**：
1. **它不占说话额度、也不影响提醒节奏** —— 分析出口与说话层是两条独立的路（同一份脱敏上下文）。
2. 想让它产出的数据被别的程序消费，配置 `channels.analysis_file`（原子写 JSON）或
   `channels.analysis_webhook`；不配也能用，消费者自己拉 `GET /analysis`。
3. 每次跑会调一次模型（一次调用，token 量很小）；不需要每天跑就改成每周，别让它空转。

## 配置出口

```jsonc
{
  "channels": {
    "analysis_webhook": "https://你的看板/hook",   // POST 一份 JSON
    "analysis_file": "/home/you/analysis.json",   // 或落文件（原子写：先 .tmp 再 rename）
    "analysis_keep": 50                           // 库里保留最近几份
  }
}
```

两个都不填也能用：分析照样入库，消费者自己拉 `GET /analysis` 即可。
`hubctl channels` 会把分析出口单独列出来（它是**数据出口**，配了它不会让她多说话）。

## 变更规矩

- **破坏性改动必须升 `version`**（那就变成 v2，`v` 字段跟着变），读方遇到不认识的 `v` 应拒收。
- 加**可选**字段属于兼容改动，`version` 不动，但要同步四处：
  `docs/analysis.schema.json` → 中枢里嵌的副本 → 中枢校验器 `_ANALYSIS_ITEM_RULES` → 写端提示词。
  第二处用 `/tmp/sync_analysis_schema.py` 同步；漂移会被 `tests/test_analysis_schema.py` 拦下。
- 规范文档里的 `examples` 会被测试当**教具**校验 —— 示例错了比没写更糟，所以它必须真的合规。
