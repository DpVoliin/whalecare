# 路线图

> 怎么读：**打勾 = 已经在线上真跑过**（不是"写完了"）。没打勾的都写清了**卡在哪** ——
> 没有"待定"这种占位。历史版本索引在最下面。

## 已完成

### 中枢（`hub/hub.py`，零第三方依赖）
- [x] HTTP + SQLite + 规则引擎 + 脱敏 + 定点提醒 + 简报 + 状态页
- [x] **代码模块化**：`hub/src/whalecare/` 片段 + `build_single.py` 合并成单文件（CI 断言**字节等价**）
- [x] **schema 迁移框架**：`PRAGMA user_version` + 有序迁移链（每个迁移幂等、失败不让中枢起不来）；
      当前 **v7**，`hubctl schema` 看版本与待跑迁移
- [x] **数据主权端点**：`/export`（GDPR Art.20）· `/erase`（Art.17，需 confirm）
- [x] **可选 Docker 部署**：`Dockerfile` + `docker-compose.yml`（主线仍是"一个文件 + 一条命令"）

### 采集（手机 / 电脑 / 小设备）
- [x] **Android 采集器**：屏幕用量（按分类）、日程、健康通知、媒体会话、游戏、订单快递、
      电量/充电/下一个闹钟、蓝牙外设电量；Keystore 加密队列、开机自启
- [x] **自适应采集频率**（"在用 / 上课 / 推算作息"分档 + 亮屏事件触发）
- [x] **PC 采集器 / 桌面挂件**（`desktop/`，同一个 exe）：透明置顶角色、拖动吸附、点她出气泡、
      ✓/✗ 反馈、电脑使用时长/类别分钟/久坐/系统盘/内存/开机时长/窗口切换
- [x] **小设备下行口**：`/mcu/inbox` + `/mcu/ack`（硬件无关的一行纯文本 · gb2312 直通中文 TTS ·
      可朗读化剥动作标注）+ 上行 `/api/mcu`

### 说话层（`speaker/`）
- [x] 角色卡 + few-shot + 现场生成 + 事实指纹去重 + 质量闸 + 模板回落
- [x] **节奏自适应**：`next_gap()` 按当下数据算 5–90 分钟，每天最多 12 句主动
- [x] **四个有文献出处的机制**：断点投递 · Goldilocks 时间窗 · 期望效用门控（Horvitz 1999）·
      分桶 Thompson（EOPA arXiv:2608.04416）
- [x] **说话策略可外挂**：`whale_strategy.py` 覆盖 `next_gap`/`material_score`，返回 None 回落内置，热加载
- [x] **睡前小总结**（时间点由睡眠数据推算）· 免打扰 23:00–07:00

### 出口（8 个直发通道 + 主路微信）
- [x] **主路**：说话层 → 网关 webhook → 微信（HMAC 签名，零 LLM 成本）
- [x] **企业微信群机器人** · **通用 webhook** · **企业微信应用消息**
- [x] **ntfy** · **Bark** · **钉钉**（含官方加签）· **Discord** · **QQ**（**官方机器人 API**，不装 SDK）
- [x] 全部**不自动使用**（避免与说话层重复推送），`hubctl channels` 看状态、`POST /channels?test=1` 发测试

### 记忆与学习
- [x] **情节记忆**：`episodes` 表 + FTS5/BM25 检索（进模型的只有检索出的摘要）
- [x] **周/月复盘**：本期 vs 上期 + 读上期结论核对改善
- [x] **主动提问**：有数据支撑的问题，一天最多一个，问过不重复
- [x] **反馈闭环**：挂件 ✓/✗ → `/feedback`（带场景桶 + 证据强度权重）→ Beta 后验；
      **45 天半衰期时间衰减**（TV-TS 思路：人的作息会漂，旧反馈权重自动降）
- [x] **离线策略评估**：`hubctl eval` —— 回放式评估接受率/期望效用/90% 置信区间/regret/成本敏感性，
      用你自己的决策日志 + 反馈，不需要上线冒险

### 隐私与安全
- [x] 脱敏后才进模型（`/llm-preview` 可逐条核对）· 原文默认不落库
- [x] **审计日志 + 哈希链**：`hubctl audit --verify`（改中间任意一条都会被发现）
- [x] **常数时间比较**（`hmac.compare_digest`）· 会话 Cookie `HttpOnly; SameSite=Strict` + HTTPS 时 `Secure`
- [x] **WAL** 并发 · 备份 AES-256 · 中继强制校验证书 · 防火墙收口 · 自动保留策略
- [x] **威胁模型文档**：`docs/THREAT-MODEL.md`（信任边界 + 7 类对手 + **我们不防什么** + 已知缺口表）
- [x] 安全加固清单：`docs/SECURITY.md` · `docs/PRIVACY.md`（含实测自检项）

### 工程与分发
- [x] **PyPI 正式发布**（`pip install whalecare`，0.1.19 → 0.1.22 已上线，**零运行时依赖**）
- [x] **CI 三条**：`ci`（合并断言 + 产物等价 + 151 测试 + 零依赖 + ruff 双门禁 + 脱敏回归 + SBOM +
      **版本六处一致性门禁**）· `android`（Gradle 9.7.1 + AGP 9.4.1 + compileSdk 37）· `scorecard`
- [x] **两个 lint 门禁分工**：产物走根 pyproject 严格规则；片段走专用 `ruff.toml`（压掉跨片段假阳性）
- [x] `docs/` 齐备：ARCHITECTURE / PRIVACY / SECURITY / THREAT-MODEL / SCHEMA / LOCAL-FIRST /
      DEPLOY-GUIDE / DEMO-SCRIPT / EXTENSIONS / MCU / ANDROID-RELEASE / FDROID / COMMUNITY + 5 篇 ADR
- [x] 仓库简介与 topics 已配（GitHub 右侧那栏）

### 社区
- [x] `docs/COMMUNITY.md`：版本策略（为什么 0.1.x / 何时到 1.0）+ 可认领任务 + 标签约定 + 术语词表
- [x] 明确的 **help wanted** 任务留在 issue 里（不写完，留给第一个外部贡献者）

## 待办（每条写清卡点）

- [ ] **手表真实健康数据** —— 卡点：**厂商没有开放平台** ✗。降级路线：Health Connect → 通知监听
      → 无障碍 → 截图 OCR 兜底
- [ ] **Health Connect 作为可选数据源** —— 卡点：需要真机 + 用户显式授权（GDPR Art.9 特殊类别数据）
- [ ] **系统勿扰（DND）状态作为可打断性输入** —— 卡点：需要 Android 端配合；现有断点投递只用了
      屏幕空闲与课表
- [ ] **macOS 采集器** —— 卡点：没有 mac 机器可测
- [ ] **手机上跑 agent**（Termux + proot） —— 卡点：未验证；理论可搬（纯 Python/Linux）
- [ ] **F-Droid 提交 · awesome-selfhosted PR · demo 视频/截图** —— 卡点：**需要你的账号 / 真机画面**
- [ ] **隐式反馈权重的人工标定**（评审建议 N≈200） —— 卡点：需要你抽时间标一批；
      现在权重是启发式，但 `hubctl eval --sweep` 已让"偏差可见"
- [x] **健康数据的显式同意（PIPL / GDPR Art.9）**：`consents` 表 + 入库闸口（v0.1.23）
- [ ] **PIPL 其余条款细化**（告知同意文本、跨境等）—— 卡点：取决于要不要在国内推广
- [ ] **飞书出口** —— 卡点：官方接口要 app id/secret + 租户授权；Hermes 插件已能覆盖
- [ ] **Whalecare-Bench**（评审 v3 的长期建议）—— 把 `hubctl eval` 长成公开评测集 + 基线 + 排行榜。
      这是把项目从"又一个自托管工具"抬到"这个细分方向的参考实现"的唯一杠杆；卡点：需要更多真实反馈样本

## 已评估、短期不做（留结论，不反复纠结）

- **STM32 小屏 + 语音模块**：接口侧已验证可行（`/mcu/inbox` + `/mcu/ack` 一行纯文本 · gb2312 · 可朗读化），
  成本约 90 元 + 需一台常开机器做中继。**放弃原因**：短期用不上，不为它增加维护面。
  硬件无关的下行口**保留**（任何 ESP32 / 树莓派小屏都能用）。
- **云端方案**（加密云 / 把数据上云）：与"本地优先、数据主权"这条主线冲突，不做。

## 扩展方式（给别人接自己的设备）

任何设备只要能 `POST /ingest` 就能接入，中枢代码不用改：

```json
[{"device": "phone_x", "metric": "screen.active_minutes", "value": 260},
 {"device": "laptop_x", "metric": "app.usage_minutes", "value": 96,
  "meta": {"app": "VS Code", "pkg": "code"}}]
```

指标字典（当前）：
`sleep.total_minutes` / `sleep.deep_minutes` / `health.heart_rate` / `health.spo2` /
`health.stress` / `steps.total` / `screen.active_minutes` / `screen.idle_minutes` /
`app.usage_minutes` / `calendar.event` / `task.todo` / `music.track` / `order.event` /
`device.battery_percent` / `device.charging` / `device.next_alarm` / `weather.day` /
`temp` / `hum`（单片机）/ `pc.continuous_active_minutes` / `pc.disk_free_percent` /
`pc.mem_percent` / `pc.uptime_hours` / `pc.window_switches_today`

> 口径提醒：同一时空里**累计值**（当天累计分钟数、当天窗口切换次数）一律"取当天最新"，
> **不要 SUM** —— 设备是每隔几分钟把"今天到目前为止"重报一次的。

## 版本索引

| 版本 | 是什么 |
|---|---|
| v0.1.9 | 走完 P0/P1/P2 主体：数据主权端点 · Python 矩阵 · SBOM · Scorecard · Android CI · Docker · 文档齐备 |
| v0.1.13 | 审计日志 · MCU 中继 TLS + 一次性配对码 · Web 管理台 |
| v0.1.16 | 压测合成数据 + 反事实调参（`stress_report.py` / `tune_gap.py`） |
| v0.1.17 | schema 迁移框架 · 反馈带场景桶 · 说话策略可外挂 · 首批直发出口 |
| v0.1.19 | 版本五处统一 + 一致性门禁 · 片段专用 lint（678 假阳性压到 0） |
| v0.1.20 | **PyPI 上线**（pip 可装）· 补齐 5 项工程欠账（含 `/health` 接口列表自动化：首跑就抓出漏了 22 条路由） |
| v0.1.21 | 直发出口加 **ntfy + Bark** · 出口测试用真 HTTP 桩抓实际字节断言格式 |
| v0.1.22 | 直发出口再加 **钉钉 + Discord + QQ（官方机器人 API）**；README 中英双语同步 8 个出口 |
| v0.2.0 | **第 9 个出口：分析出口（数据出口）** —— 让 AI 分析数据后输出**结构化数据**而不是句子；配套**数据格式规范**（JSON Schema + 人读文档）并做成中枢侧**硬门禁**（不合规拒收）· `analyses` 表（schema v7） |

### 外部评审 v3 条目落实情况（2026-09-22）

| 评审说缺的 | 现状 |
|---|---|
| 分析层无可复现评测（P0） | ✅ `hubctl eval`：回放式评估 + regret + 置信区间 + 成本敏感性 |
| 威胁模型文档（P1） | ✅ `docs/THREAT-MODEL.md` |
| 配对码/登录抗枚举（P1） | ✅ 5 次/10 分钟 → 锁 15 分钟（v0.1.23） |
| 审计防篡改（P1） | ✅ 哈希链 + `hubctl audit --verify`（实测篡改一条即报出位置） |
| WAL（P1） | ✅ `PRAGMA journal_mode=WAL` + 测试断言 |
| 非平稳 / TV-TS（P1） | ✅ 反馈权重 45 天半衰期衰减 |
| 常数时间比较 / Cookie Secure（P1） | ✅ 两者均已加 |
| 可打断性传感（P1） | ⏳ 部分（屏幕空闲 + 课表已有；**DND 未接**，见待办） |
| amalgamation 产物移出版本控制 | ✗ **不采纳**（与"部署 = 拷一个文件"的核心取舍冲突） |
| FTS5（评审说"若无"） | ✅ 早有（`episodes_fts` + BM25），评审读不到源码 |
| 打扰成本模型（评审说"明显缺"） | ✅ 早有（`C_FALSE`/`C_MISS` + Horvitz 门控），评审读不到源码 |
