# 数据库表结构（每一张表存什么、留多久、哪些字段进过模型）

> 一句话：**原始数据只在你自己的服务器上**；进模型的东西一律先过脱敏，`/llm-preview` 可以随时核对。

## 表清单

| 表 | 存什么 | 保留 | 会进模型吗 |
|---|---|---|---|
| `metrics` | 所有采集到的数据点：`ts/day/device/metric/value/unit/source/confidence/meta` | `privacy.retention_days`（默认 365 天，`0`=永久） | **只有聚合后的少数几项**（见下） |
| `reminders` | 待推送/已推送的提醒（定点提醒、规则触发） | 随 metrics 一起清理 | ❌ |
| `scheduled` | 用户设的定点提醒（`at/text/daily`） | 永久（除非你删） | ❌（只用于投递） |
| `fired` | 已触发记录（防重复触发） | 永久 | ❌ |
| `chats` | 与她的对话记录 | 手动删 | ❌（`/memory` 检索时给"最近说过的事"摘要） |
| `episodes` | **情节记忆**：她说过什么/发生过什么（带 `sig` 指纹去重） | 手动删 | ✅ 只给"最近几条"的短摘要 |
| `episodes_fts*` | 情节的全文索引（trigram，中文可搜） | 随 episodes 重建 | ❌ 派生数据 |
| `decisions` | **决策日志**：每次"说/不说"的间隔、理由、料分、场景桶 | 手动删 | ❌（给人回放用） |
| `feedback` | **反馈（两种来源）**：`verdict` + `w`（证据强度：手动点=1.0，隐式推断=0.4~0.6）+ `src`（`manual`/`implicit`）+ `band`（场景桶，客户端不传时由中枢按上报时刻自算）。隐式的三条边界见 README「你懒得点 ✓/✗ 也能学」 | 手动删 | ❌（只影响阈值，不进上下文） |
| `audit` | **审计日志**：`action/target/actor/result/note` —— 鉴权失败、配置修改、导出/删除/备份、扩展报错、配对、token 轮换 | 手动删（`hubctl audit --day`） | ❌ **且设计上物理不存数据内容**（只记动作与对象，所以能安全外发） |
| `pair_codes` | **一次性配对码**：`code/device/expires_at/used_at/used_by`。码用过即废，15 分钟过期；过期的自动清 | 自动清理 + 手动删 | ❌ |
| `timetable` | 课表原文（岛课表导出的 JSON） | 手动删 | ⚠️ **只取节次与类型**，课程名/教师/教室都剥掉 |
| `terminals` / devices | 设备名与最后上报时间 | 永久 | ❌ |
| `analyses` | **分析出口的数据**：让 AI 分析处理后产出的结构化结果（指标现值/趋势/异常/共变/评分/标签）+ `ts/day/engine`。它已经是脱敏后的派生数据，**不含通知原文** | 最近 `channels.analysis_keep` 份（默认 50，0=不裁） | ❌（是模型的**产物**，不是输入）|

## 进模型的字段（唯一入口：`llm_context()`）

允许进模型的**只有**：分类名、粗粒度数值、小时级时间、城市级天气、曲名/歌手、游戏名、金额区间、节次与类型。

**明确剥掉**（每次都在 `/llm-preview` 的 `what_model_never_sees` 里列出）：

```
通知原文 · 日程标题与地点 · 具体 App 名（游戏名保留）· 分钟级时间
电脑的进程名/窗口标题/文件名 · 蓝牙 MAC · 精确经纬度 · 设备名与型号
待办原文（只给条数）· 睡眠原始值与精确入睡时刻 · 订单商品名/店铺/精确金额
```

## 数据可携带与删除（GDPR Art.20 / Art.17）

```bash
# 全量导出（机器可读 JSON；?redact=1 顺手脱敏，方便分享）
curl -H "X-Token: $TOKEN" "https://你的服务器:11443/export?redact=1" -o my-data.json

# 物理删除（必须显式确认；删前自动备份一份，删完 VACUUM 回收）
curl -X POST -H "X-Token: $TOKEN" -H 'Content-Type: application/json' \
     -d '{"confirm":"ERASE-ALL"}' "https://你的服务器:11443/erase"

# 只删某一类：scope = metrics | episodes | chats | reminders
```

> 命令行等价物：`hubctl dump --redact --encrypt` / `hubctl prune`。
> **"删掉 `hub.db` 就是彻底删除"** —— 这套系统没有云端副本，也不需要"注销账号"。

## 迁移策略（v0.1.17 起是框架，不是土办法）

版本号存在 **`PRAGMA user_version`**（当前 v5：001 基础表 / 002 decisions.ctx / 003 audit+pair_codes / 004 feedback.w+src / 005 audit 链式 hash(prev_hash+hash)）；迁移是 `hub/src/whalecare/10_core.py` 里的**有序函数列表**
（`@migration` 装饰器，注册顺序 = 版本顺序），`init_db()` 启动时自动补跑，`hubctl schema` 可查。

三条硬规矩（都是被现实咬出来的）：

1. **每个迁移必须幂等** —— 老库 `user_version=0` 但表已存在，会被当成"从头跑一遍"；
   不幂等（比如裸 `ALTER ADD COLUMN`）就会在第 N 次启动时炸。
   *踩过*：ctx 那次 `ALTER` 写在 `executescript` 里 → 第二次启动 `duplicate column name`
   → **整个 init_db 中断**，它**之后**的建表语句全部没执行（`terminals` 没建成 → `/today` 断连）。
2. **迁移失败不许把中枢带崩** —— 自用系统先保证可用：报出来、停在上一版、`hubctl schema` 能看出落点。
3. **测试要覆盖"老库"与"连跑两次"** —— 见 `tests/test_migrate.py`（全新库 / 老库且数据不丢 / 幂等 / 迁移数=版本号）。

## 迁移策略

现在**没有**迁移框架：改表结构时用 `ALTER TABLE ... ADD COLUMN`（向后兼容），
破坏性改动必须写进 `docs/adr/` 并附一个**幂等迁移函数**（已落地：`PRAGMA user_version` + `_MIGRATIONS` 有序列表，`hubctl schema` 可看落在哪一版）。
