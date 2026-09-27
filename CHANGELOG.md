## v0.1.26 — 2026-09-25

### 修复
- **设备失联提醒会反复念同一条**（用户原话："怎么一直提醒我卡住了"）
  - 文案里带小时数（`已经 101 小时没同步` → `110 小时`），而下游去重是"原文完全相同才跳过" ✗
    → 数字一变就被当成**新消息** → 每轮都发。改为按**天**取值，同一天文案完全一致，去重即可拦下。
  - **已弃用的设备会永远满足"失联"** → 天天念。新增 `device_abandon_hours`（默认 72 小时）：
    失联超过 3 天视为弃用，不再提醒 —— 那不是"没同步"，是设备不用了。
  - 新增测试覆盖这两种情况（防复发）。
- **睡前简报每天都被拦掉（等于从来没有过）**
  - 中枢按"推算的入睡时刻（02:15）提前 15 分钟"→ 定在 **02:00** 发 ✓
  - 而说话层的时段窗口写的是 `(20, 2)`，判断是 `h < 2` → **02:00 正好落在窗口外** ✗
  - → 睡前简报天天被判"不在时段窗口"丢弃 ✗ 用户从来没收到过睡前总结 ✓
  - 修：窗口改为 `(20, 3)`（留一小时余量，覆盖到真实入睡时刻之后 ✓）
- **重试风暴打爆中枢限流 → 她彻底哑掉**（用户："提醒又没了"）
  - `deliver()` 失败后不 ack ✗ 主循环以为没处理完 → **每 5 秒重试一次** ✗ 没有退避
  - 睡前那条路被上面"窗口修好"之后开始重试 → 打爆中枢"每来源 240 次/分" ✗
  - → **她所有调用都被 429 顶回去** ✗ 连正常开口都发不出 → 用户感知"提醒又没了" ✓
  - 实测日志里累计 **102,408 条 429** ✗（10 小时没说话）
  - 修：**在 `deliver()` 内部**做失败退避（一处修全路径 ✓）
    失败 → 静默 5 分钟；限流 429 → 静默 15 分钟；期间连网络都不打 ✓
  - 复验：重启后立刻恢复 ✓ 并发出**历史第一条睡前总结**（`#122 睡前总结已发` ✓）
  - 另一条同类现象的结论：早间简报 09:00 创建、13:29 才轮到发 → **被她正确拦下** ✓
    （这条她做得对 ✓ 是上游排队太晚 ✓ 不属于缺陷 ✓）

## v0.1.25（说话层工程化 · 模型适配 · 策略回归门禁）

### 按主流模型特点适配参数（根治"思考型模型吃光 token"）

- 新增 **`speaker/model_profile.py`**：按模型族给出参数画像 —— 参数名（`o1/o3/gpt-5` 只认
  `max_completion_tokens`）、预算（思考型 2400 / 非思考型 400）、`temperature` 约束
  （o 系列只接受 1；`deepseek-r1` 建议不带；claude 与 `top_p` 互斥）。
  名字认不出的模型**保守按思考型**处理（预算给大不亏，给小才会空）；运行时若响应里出现
  `reasoning_content` 而名字未标思考型，**当场改判**。
- `llm()` 四层保障：按画像调用 → 空/截断翻三倍重试 → 主模型连败切**备用模型**
  （`WHALE_FALLBACK_MODEL`）→ 全失败交回调用方模板（她**永远**有话说）。
- 实测：`deepseek-v4.1-flash` 首发即拿到完整句子（此前每次要白烧两轮重试）。

### 自检与看门狗（把"她哑了"变成能被主动发现的事件）

- `startup_selfcheck()`：启动时验 中枢可达 / token 有效 / 模型可达 / 状态目录可写。
- `watchdog_check()`：接口连错 ≥5、模型返回空 ≥5、活跃时段 ≥6 小时没开口且有料 → 发一条
  **自查消息**（每天最多一次；免打扰时段不判）。
- `health_bump()`：逐类计数（sent / errors / model_empty / blocked-by-reason），
  覆盖全部 11 个"不说/丢弃"出口。

### 策略回归门禁

- `speaker/sim_week.py`：一周模拟，调说话层**真函数**（含假时钟，否则台账跨天不重置），
  `--check` 模式把"时段错位 / 平均条数越界 / 免打扰失效"当**不变量**校验，破了就非 0 退出。
- CI 新增该步骤；另加 `tools/prepush.sh` 一键跑齐三道门禁（lint + 测试 + 版本一致性）。

### 其他

- 修 `build_zipapp.py` 默认输出污染发布目录导致 PyPI 整批失败的问题；
  `publish.yml` 显式声明 `packages-dir: dist/`。
- 路径全部可配置（`WHALE_SPEAKER_DIR` / `WHALE_CA` / `WHALE_ATTRIB` / `WHALE_CONFIG`）。

## v0.1.24

### 说话层（speaker/）· 沉默失败的系统性修复

线上连续出现四个**沉默失败**（看起来正常地什么都不做，日志里要翻几千行才发现），逐个修复并实测：

- **模型空回复导致一整天不说话**：思考型模型把 token 预算花在思考上（实测 297/365/1058 token），
  而调用只给了 `max_tokens=200` → `content` 返回空串 → 判定"不说"。修：空回复或
  `finish_reason=length` 时 `max_tokens` 翻三倍重试（首发 200→700），并把 `finish_reason`
  与思考长度记进日志。
- **判重窗口跨天**：数字归桶后，昨天的"屏幕 945 分钟"与今天的"屏幕 556 分钟"落进同一桶，
  而判重窗口不分天 → 今天所有屏幕/社交类全被当成"昨天说过了"。修：`recent()` 加 `today_only`，
  记录带时间戳，≥100 的数字按 60 归桶，关键词表补齐"社交/时长/分钟"等。
- **回执失败导致重复打扰**：提醒发出去了但 `ack` 被 429 拒掉 → 提醒仍是"待发" → 下一轮又发一遍
  （睡前总结连发三条）。修：本地"已投递 id 台账"（发成功即记账）+ 回执带退避重试。
- **补发过期提醒**：昨晚没发出去的"睡前提醒"今早才补发，出现"早上说晚上的事"。修：时段窗口
  （睡前 20:00–02:00 / 早间 05:00–11:00），出窗口**丢弃 + 回执**，不补发。
- **冷启动死锁**（由一周模拟发现）：还没有反馈时 `p(接受)` 恒等于 `Beta(1,1)=0.50 < 0.67`，
  只有"料≥3"才敢开口 → 正常作息一周一句话都说不出；而反馈又要求她先开口。
  修：冷启动期按每天最多 3 条试探（`COLD_START_PER_DAY`），收到反馈立刻交回 Thompson。

新增两个工具（都是"事前发现"而不是"事后翻日志"）：

- `speaker/sim_week.py` —— 一周模拟。调说话层**真函数**（`next_gap` / `utility_gate` / 话题台账 /
  相似度 / 时段窗口 / 免打扰），带假时钟（否则台账跨天不重置）。可复现（`--seed`），
  输出每天的条数与判定。当前基线：2.3 条/天 · 时段错位 0 · 重复打扰 0。
- `speaker/whale_speaker.py` 内的**自检与看门狗**：`startup_selfcheck()` 启动时验中枢/token/模型/
  状态目录；`watchdog_check()` 在接口连错 ≥5、模型空 ≥5、活跃时段 ≥6 小时没开口且有料时
  发一条**自查消息**（每天最多一次，免打扰不判）；`health_bump()` 逐类计数覆盖全部 11 个
  "不说/丢弃"出口。

其他：`GAP_MIN` 5→15 分钟（叠加倍数后 8~10 分钟一条太密）；定点循环加退避（原来一秒重试 9 次
打爆中枢限流 → 全线 429）；路径全部可配置（`WHALE_SPEAKER_DIR` / `WHALE_CA` / `WHALE_ATTRIB` /
`WHALE_CONFIG`），仓库版里 `/opt/whale`（无写权限）一并修掉 —— 换台机器可直接跑。


**修 `/consent` 的两个真错**（0.1.23 上线后实测发现的 —— 记下来当教训）：

- ① 第一版把 `/consent` 写进了 `do_GET` → **POST 直接 404**。写操作必须在 `do_POST`。
- ② 第二版用了**不存在的** `self._read_body()` → body 永远是空 → 接口误报"只支持 what=health"。
  正确姿势是沿用本文件其它路由一致的 `self._body()`（返回已解析的 dict/list）。
- ★ 这两个错**函数级测试全都发现不了** → 新增 `TestConsentRoute`：用中枢自己的 Handler
  起临时服务、真打一次 HTTP 走完整链路（没同意→丢弃 / 给同意→入库 / 撤回→又丢弃）。
- `hubctl consent`（看状态 / `--grant` / `--revoke`）：以后给同意不用再手写 JSON 请求。
- 影响：0.1.23 上线的同意闸是按预期工作的（健康数据确实被拦下了），但**同意入口是坏的** →
  升级到 0.1.24 后才能真正"给上同意"。

## v0.1.23

**补掉「已知缺口」清单里的四条 —— 都做成机制，不只是文档。**

- **MCU 证书多指纹轮换窗口**：`WHALE_PIN` 支持逗号分隔多指纹，另加 `WHALE_PIN_NEXT` 作轮换位，
  换证书期间新旧指纹并存（否则证书一到期就全量失联）。轮换步骤写进 `mcu/mcu_relay.py` 顶部注释。
- **配对码 / 登录失败次数限流**：`auth_fails` 表 + 5 次 / 10 分钟 → 锁 15 分钟，成功一次即清零，
  只锁该来源不牵连别人；登录页被锁回 429 并告知还要等几秒。
  写测试时**抓到自己实现里的一个真 bug**：锁定期内再来一次失败会把 `locked_until` 覆盖成空 →
  相当于自己给自己解锁，已修。
- **敏感健康数据的显式同意（PIPL / GDPR Art.9）**：新增 `consents` 表（**只追加**，能回答
  "当时他同意的是什么")+ 入库闸口检查：没有同意就丢弃 `health.*` / `sleep.*` 并**如实计数 + 写审计**；
  `POST /consent` 记录同意/撤回，`hubctl consent` 可查可改。
  闸口放在 `ingest_items`（HTTP / 单片机 / 扩展的唯一入口）→ 新数据源绕不过。
- **备份生命周期**：`hubctl backup` 默认只保留最近 7 份（`--keep N` 或 `WHALE_BACKUP_KEEP` 可调），
  自动清理更旧的 —— 备份含全量数据，堆着就是多一份泄漏面。
- 测试 +18（`tests/test_throttle_consent.py`）：限流（含"限流自己坏掉不能把人关门外"）、
  同意（含撤回后立刻生效、历史只追加）、备份保留数、中继多指纹解析。

## v0.1.22

- **直发出口再加三个，全部走官方接口**：
  - **钉钉** `channels.dingtalk_webhook`（+ 可选 `dingtalk_secret` 走官方加签算法）
  - **Discord** `channels.discord_webhook`（官方 webhook，`{"content": ...}`）
  - **QQ** `channels.qq_appid` + `qq_secret` + `qq_target`（**官方机器人 API**：先取
    `access_token`，再 `POST /v2/users/<openid>/messages` 或 `/v2/groups/<group_openid>/messages`，
    不装任何 SDK；token 内存缓存并提前 2 分钟续期）
- 配合 Hermes 平台插件：**钉钉 / Discord / 飞书 / 企业微信 / ntfy / Telegram / Slack / WhatsApp
  都有官方插件**（bundled，`hermes plugins enable <名>-platform` 即可），本仓库的直发出口
  是给**非 Hermes 部署**用的那条路。
- 出口测试扩到 21 项（钉钉 payload 与加签、Discord 204、QQ 取 token + 私聊/群聊路径）。

## v0.1.21

- **新增两个原生直发出口：ntfy 与 Bark**（issue #14 的前两个）。
  ntfy 走的是**裸文本 POST**（不是 JSON —— 那是它自己的协议），Bark 走**路径式
  `GET /<key>/<标题>/<内容>`**，两者都支持可选的鉴权 / 铃声配置；
  都**不自动使用**（避免与说话层重复推送，只给 cron / 扩展 / 手动调用）。
- 测试用**真 HTTP 桩服务**抓"实际发出的字节"来断言请求格式（裸文本/路径式/鉴权头/铃声/四路同发），
  与原有用例合并后共 15 个 —— payload 形状写错在本地就能被发现，不用等真有事。
- 出口状态列出全部五个通道（依旧**不打印任何地址本身** —— 那带密钥）。

## v0.1.20 — 发布到 PyPI · 补齐 5 项工程欠账

### 发布
- **`pip install whalecare` 正式可用**（PyPI 上线 0.1.19/0.1.20，零运行时依赖）。
  包壳 `whalecare/` 只做"定位并执行那份单文件中枢"，不改一行逻辑；
  `.github/workflows/publish.yml` 走 Trusted Publishing（仓库不存任何 API token），
  并在发布前强制校验：版本一致性 + 片段与产物逐字节等价 + wheel 内含 hub.py + 无依赖声明。
  实测：`pip install whalecare` → `whalecare` 命令真的把中枢跑起来了。

### 工程欠账（对应 5 个 issue，做完即关闭）
- **`/health` 的接口列表不再手抄**：新增 `tests/test_health_endpoint_list.py` ——
  从源码扫出真实路由（精确 + 前缀两种写法）与 `/health` 列表**双向比对**，
  还带一条"故意注入假路由必须能抓到"的反向验证。**首次运行就抓出列表漏了 22 条真实路由**
  （`/memory` `/remind` `/review` `/timetable/today` `/channels` `/feedback` …），已补齐 16 → 38 条。
- **日期边界测试**：`tests/test_date_edge_cases.py` —— 生成器跑满 400 天（跨年）后日期键自洽、
  一年前的值绝不落进"上期"、区间两端闭区间、闰日 `YYYY-02-29` 正确参与筛选与聚合、
  未来日期不污染基线。
- **构建可复现**：`docs/ANDROID-RELEASE.md` 记录实测能出包的完整基线
  （JDK 17.0.20 · Gradle 8.11.1 · AGP 9.4.1 · compileSdk/targetSdk 37 · minSdk 26 · build-tools 36.0.0）
  + 依赖锁定的具体命令；`docs/FDROID.md` 同步状态。
- **采集器双语界面**：17 处硬编码中文抽成字符串资源，新增 `values-en/strings.xml`；
  系统语言为英文时界面显示英文，中文仍为默认。已用 `:app:assembleDebug` 编译验证通过。
- **文档英译起步**：`docs/PRIVACY.en.md`（隐私设计全文），术语按 `docs/COMMUNITY.md` 词表统一。

## v0.1.19 — 设备下行口（结论留档：硬件方向短期不做）

> **结论**：接口可行、已端到端验证，但**短期不做硬件**（STM32 小屏 + 语音模块）。
> 保留下行口本身（硬件无关，任何 ESP32/树莓派小屏都能用）；STM32 专属的接线文档/扩展示例/PC 模拟器已删除，不留废件。

### 保留（接口侧）

### 新增
- **`GET /mcu/inbox` / `GET /mcu/ack`**：屏幕/音箱类设备取提醒的**下行口** —— 回一行纯文本
  (`ok|<id>|<文本>` / `none` / `err:token`)，单片机不用解析 JSON；`enc=gb2312` 让中文 TTS 模块直接可用。
- **可朗读化处理**：下发前剥掉 `（动作）` 标注、emoji、markdown，超 120 字按句号截断 ——
  念出来的话不带动作（与人设里"不许假装做物理动作"同一条规矩）。
- **`hub/tools/mcu_sim.py`**：PC 端假设备，**不用买元件**就能验证整条链（自动从 `hub.json` 读设备 token，
  按请求编码解码显示），实测已打通真中枢。
- **`docs/STM32.md`**：接线表（含 ESP-01 供电、TTS 共地、SPI 走线三个坑）、可直接复制的参考代码、
  约 90 元 BOM、FAQ；**`hub/ext/stm32_hooks.example.py`**：改说话风格/夜间静音走扩展层，不动核心。

# 变更记录

版本号只递增**第三位**（本项目是自用系统，不做对外兼容承诺）。

## v0.1.18 — 隐式反馈（弱信号）+ 料分字段对齐 + schema v4

### 为什么加隐式反馈（这是"她为什么一直不说话"的根治）
实测她从建立起**一条手动反馈都没收到**（日志长期 `反馈 0✓/0✗`）→ p_接受恒等于 Beta(1,1)=0.50，
永远低于阈值 0.67 → 只有"料≥3"才敢开口，**整条自适应机制在饿着**。
光等手动 ✓/✗ 可能永远等不到，所以补一个**弱信号**：
- 她说完 **30 分钟内主人回话了** → 这次开口受欢迎（`good`，w=0.6）
- 主人在场（前后 3 小时有动静）**却一直没回** → 这次开口是打扰（`bad`，w=0.4）
- 之后 **3 小时都没人影**（睡了/出门）→ **什么都不记**（绝不把"没看见"当成"嫌烦"）

强证据压过弱证据：`feedback` 加 `w`（证据强度）与 `src`（manual/implicit），
Beta 后验按**分数计数** `(1+Σw_ok)/(2+Σw_ok+Σw_bad)`（schema v4 迁移）。
主人的"最后说话时间"只从**本机** Hermes 会话库只读读取，不外发；读不到就什么都不记。
开关：`WHALE_IMPLICIT=0` 可关。测试：`tests/test_implicit_feedback.py`（7 例，含"不在场不记"）。

### 料分读的字段与中枢对齐（真 bug：明摆着的料全没算分）
决策日志里她永远「料=1/2 → 不说」，而 `llm-preview` 里明明有雷阵雨、电脑磁盘只剩 1.1%、今天 3 节课。
根因是 `material_score` 读的键中枢根本不给（**字段名对不上时不报错，只是永远算 0 分**）：
`weather_today.rain_prob`（实际是 `desc="雷阵雨"`+`weather_now.rain_24h`）、`classes_today`（实际 `classes` 列表）、
`games_minutes_today`（不存在）、`pc_health`（压根没读）、`sleep`（实际是字典 `{minutes_rounded}`）。
已对齐并补上磁盘（≤5% 记 2 分）/内存（≥92%）。
实测：拿服务器那份真实上下文重算，**料分 2 → 7**（雷阵雨/3节课/快递/磁盘1.1%/屏幕536分/反常1项）。
新增 `tests/test_material_signals.py`：静态比对「料分读的键」vs「中枢给的键」，换回旧代码该测试必红。

### 补正（重要）：上面那条料分修复**一开始修错了函数**
说话层里有**两份**料分实现：`material_score()`（用于展示/日志）与 **`material_of()`（闸门真正用的）**，
而我只改了前者 → 重启后日志里料分**还是 2**。查下去发现根因更难看：
`material_of()` 里 `screen_usage_minutes` / `calendar` / `weather_alert` **中枢根本不给**，
那些料永远算 0 分；两份逻辑各改各的、谁也不管谁 —— **"两份实现"本身就是根因**。
已修正：`material_of` 改成对 `material_score` 的**薄封装**（只留一份逻辑），
把闸门份独有的三条信号（温差≥10 / 单类别≥90 分钟 / 天气预警）合并进来，
删掉旧实现，并加断言 `material_of(ctx) == material_score(ctx)[0]` 防再分家。
实测（同一份服务器真实上下文）：**闸门看到的料分 2 → 8**。

### 顺带
- `hubctl schema` 现在会报 v4；迁移框架加了第 4 个迁移就是这个（幂等、可重放）。

## v0.1.17 — A 批：迁移框架 / 反馈带桶 / 策略外挂 / 直发出口（+ 工具链升级）

### 新增
- **schema 迁移框架**：`PRAGMA user_version` + 有序迁移链（`@migration`），每个迁移**幂等**、
  **失败不让中枢起不来**；`hubctl schema` 看版本与待跑迁移，`tests/test_migrate.py` 覆盖
  「全新库 / 老库且数据不丢 / 连跑三次 / 每个迁移重放 / 迁移数=版本号」。
  起因就是 ctx 那次手写 `ALTER` 差点把中枢搞挂 —— 这类债不能留。
- **反馈带上场景桶**：`/feedback` 不带桶时由中枢**按上报时刻自己算**（客户端一行都不用改），
  分桶 Thompson 这才真能攒到样本。
- **说话策略可外挂**：`whale_strategy.py`（或 `$WHALE_STRATEGY`）可覆盖 `next_gap` / `material_score`，
  **返回 None 即回落内置**、按 mtime 热加载、写坏只记一行日志不影响说话；附可抄的
  `speaker/whale_strategy.example.py`。
- **直发出口**（不经 Hermes 网关）：企业微信群机器人（`{"msgtype":"text",...}` 形状）+ 通用 webhook；
  `POST /push`、`POST /push/test`、`GET /channels`；出口状态**不打印 webhook 地址本身**（它带 key）。
  `tests/test_channels.py` 用**本地桩服务器**逐字校验 payload（测试绝不往外面发东西）。

### 又抓到一个"静默失效"级真 bug（靠跨模块一致性测试）
说话层的 `band_key()` 用**机器本地时区**，而中枢的桶用**固定 +8** —— 服务器时区若不是 +8，
两边算出的桶名会错位，桶后验永远取不到（不报错、只是悄悄退回全局）。
实测在 UTC 下 **85% 的分钟都错位**。已把说话层改成固定 +8，并加
`tests/test_band_consistency.py`：**一周里每一分钟**都比一遍，任何漂移当场红。

### 变更（采集器工具链升级，一个任务做完）
`AGP 8.9.2 → 9.4.1` · `Gradle 8.13 → 9.7.1` · `compileSdk/targetSdk 36 → 37` ·
`Kotlin 2.1.20 → 2.4.20` · `core-ktx 1.17.0 → 1.19.0`
- 这三条必须一起动：core-ktx 1.19.0 硬要求 AGP ≥ 9.1；AGP 9 要求 Gradle 9；compileSdk 要 37。
- `kotlinOptions` 在 Kotlin 2.4 已移除 → 迁移到 `compilerOptions{jvmTarget}`。
- 清掉 `gradle.properties` 里 AGP 9 已移除的 `android.useAndroidX` / `android.nonTransitiveRClass` /
  `android.defaults.buildfeatures.buildconfig`（它们本就是默认值，留着会让 Kotlin 插件拒绝加载）。
- CI 失败注解扩成「关键行 20 + 尾部 45 行原文」—— 真正的异常常藏在没命中关键词的行里。

### 顺带
- ROADMAP 标掉 3 条早已完成的（Docker / CI / 部署教程），补上本批 4 条。

## v0.1.16 — 高密度合成数据压测 + 反事实调参（外部评审要的"别凭感觉调参"）

### 新增
- **`tests/make_fake_history.py --density real`**：按真实上报节奏造历史（屏幕每 3 分钟、App 每 10 分钟、
  PC/MCU 每 5–10 分钟），180 天 = **41 万行 / 103 MB / 8.8 秒造完**；同时造 decisions 与 feedback，
  供回放/调参用。原来的 sparse 模式保留（每天约 10 条摘要，够测逻辑）。
- **`hub/tools/stress_report.py`**：量库大小/行数/每天密度/索引 · 关键函数耗时（中位数+冷启动）·
  HTTP 接口耗时（真起一个 Handler）· 对照预算给结论。
- **`hub/tools/tune_gap.py`**：**反事实回放调参**。按时间轴重放决策，只用"那一刻之前"收到的反馈构造后验，
  换一组阈值重算"当时会不会开口"，并用**留出法**（前 70% 挑参数、后 30% 验证）防过拟合。
  口径刻意用**接受率**而不是"认可条数"——后者随开口数单调上升，拿它当目标等于"永远推荐多说"。
- **`decisions.ctx` 列**：把做决定时的**输入**（band/material/said/silent/hour）存下来。
  只存结论的话，回放只能靠猜 —— 这是本次最关键的 schema 改动（旧库自动 ALTER 补列）。

### 压测找出的真问题（都是"40 万行才暴露"的）
- **`cat_app()` 是纯函数却被调 14 万次**（每次线性扫分类表）→ 加记忆化。
- **`_cat_day_series()` 把 21 天 14 万行拉回 Python 逐行 `json.loads`** → 改成窗口函数在库内预聚合
  （`ROW_NUMBER() OVER (PARTITION BY day, meta ORDER BY ts DESC)`；老 SQLite 自动退回旧写法）。
  实测 `llm_context()` **2.0s → 0.93s**，`care_now()` 0.62s → 0.45s，`/today` 由"直接断连"变为 0.67s。
  正确性已验：两种算法算出的「类别×天」分钟数**逐项一致**。
- **我自己引入的严重回归（被压测当场抓到）**：`ALTER TABLE decisions ADD COLUMN ctx` 写在
  `executescript` 里 → 第二次启动报 `duplicate column name` → **整个 init_db 中断、中枢起不来**，
  而且它之前的所有建表语句都会跟着失效（`terminals` 表没建成 → `/today` 直接断连）。
  已改为 `executescript` 之外的 PRAGMA 守卫，并**实测"旧库迁移只跑一次、连续启动两次都正常"**。

### 顺带发现（设计层面的真结论）
把阈值从 0.40 扫到 0.80，开口数是从 29.9 条/天 阶跃到 7.3 条/天，而**接受率几乎不动** ——
因为后验一旦收敛，`UTIL_THRESHOLD` 就只剩两档作用（"总是开口" / "仅料足才开口"）。
**它是个粗开关，不是频率旋钮**；真正的频率控制在 `next_gap`（时间带 × 各因子）。
想细调频率要动的是那边，不是阈值。

## v0.1.15 — 修一个"页面能打开但全是坏的"老 bug（/dash 与 Web 管理台）

- **`_send()` 对字符串也做 `json.dumps`** → HTML 被包成 `"\"<!doctype html>…\n<meta …>\""`：
  前导多一个引号、**真换行变成字面量 `\n`**、CSS 里的 `"Segoe UI"` 被转义成 `\"`（字体失效）。
  现在按类型分派：bytes 原样 / str 直接 utf-8 / dict·list 才 JSON。
  **`/dash` 从写出来那天起就是坏的**，管理台也中招 —— 这类 bug 只有"在浏览器里真看一眼"才会暴露，
  所以顺手补了 `TestWebPages` 回归测试（断言 str 不被 JSON 编码、页面里没有字面量 `\n`、
  表单会填入当前配置值）。
- `android` 工作流：构建失败时把 Gradle 报错**摘成 check-run 注解**。原因：Actions 原始日志
  要仓库权限才读得到，而注解是公开可读的 —— 外部贡献者不用再贴截图。

## v0.1.14 — 更名为 whalecare（鲸鲸）

- **项目更名：`whale-hub` → `whalecare`**。理由：这套系统早就不是"一个 hub"了 ——
  现在是**数据中枢 + 会自己判断该不该开口的伴侣**，`hub` 只描述了其中一个组件。
  "care" 正好是它的核心行为（主动关心）。
- 连带改动：`pyproject` 的 name/urls/script、docker service 与镜像名、SBOM vendor、
  自签证书 CN、扩展 User-Agent、`dist/whalecare.pyz`（原 whalehub.pyz）、
  以及 README/docs/模板里的全部引用（27 个文件）。片段目录 `hub/src/whalehub/` → `hub/src/whalecare/`。
- **刻意不改的两处**（它们是**运行时键**，改了会直接断功能）：
  `speaker/whale_speaker.py` 里的 `WEBHOOK_URL .../webhooks/whale-hub`（网关侧注册好的路由）
  与 `SECRET_FILE .whale_hub_secret`（已存在的 HMAC 密钥文件）。`hubctl` 命令名同样保留
  ——"中枢"确实仍是个数据 hub，改它要连服务器上的软链一起动。
- 旧仓库地址 `DpVoliin/whale-hub` 由 GitHub 自动 301 跳转到新名，star / issue / 链接都不丢。
- README 首屏补了一句**英文定位语** + GitHub topics（20 个）——中文项目在英文检索里
  最大的短板是"没有可被搜到的英文描述"，这次一并补上。

## v0.1.13 — 审计日志 / 一次性配对码 / Web 管理台 + CI 修复

### 新增
- **审计日志 `audit` 表**（P2）：记录**鉴权失败 / 配置修改 / 导出与备份 / 扩展加载报错 / 配对 / token 轮换**。
  设计红线：**只记「动作 + 对象 + 结果」，不记数据内容** —— 所以它可以直接给人看、可以外发。
  三处可读：`hubctl audit [--stats]` · `GET /audit` · 管理台里的表格。
- **一次性配对码**（P1）：`hubctl pair --device stm32_room` 生成短码（15 分钟、**用过即废**）；
  设备/中继拿它换一次 token（`GET /api/pair?c=码&d=设备名`，回一行纯文本，单片机直接读）。
  不再需要把长期明文口令写在设备里。
- **Web 管理台**（P2）：标准库拼 HTML，**不引任何前端框架**（守住零依赖）。数据源健康度 /
  决策日志 / 审计 / 开关（关心、隐私、规则） / 改人设 / 生成配对码 / 扩展状态。
  鉴权与 API **完全同一套 token**：登录页把 token 换成 HttpOnly + SameSite=Strict 的会话 cookie，
  轮换 token 即废掉所有旧会话；API 侧仍然只认 `X-Token` 头。删除/导出这类破坏性动作**刻意只在 CLI**。

### 修复（都是真 bug，其中两个让"已宣布完成"的功能在静默失效）
- **`/bands` 只挂在 POST**：说话层无 body 时走 GET → 永远 404 → 分桶 Thompson **一直退回全局后验**。
  改成 GET（读类接口本就该 GET），POST 保留兼容。
- **说话层 `TZ` 未定义**：断点投递（v0.1.10 上线的四项文献算法之一）在异常里静默失效，从未生效。
- **中枢缺 `import pathlib`**：`state.json`（调度状态落盘）读写抛 NameError → 重启防重复/防漏发一直没生效。
- **`whale_web` 用 `lstrip("www.")`**：按字符集剥，"www.weibo.com" 会被剥成 "eibo.com"（域名比对错）。改正则。
- **`mcu_relay.py` 缺 `import pathlib`**：文件一导入就 NameError —— 中继**从来跑不起来**。
- 顺手清掉 ruff 抓出的 9 处未用变量/导入、`zip(strict=)`。

### 变更（安全）
- **MCU 中继强制 HTTPS + 真正的证书固定**：不再用 `create_default_context`（它会**叠加系统根 CA**，
  等于公共 CA 也能伪造）—— 改成只信任 `WHALE_CA` 这一张证书；另支持 `WHALE_PIN=<sha256 指纹>`
  逐字节比对（实测指纹不符会被明确拒绝并报"疑似中间人"）。

### 修复（CI —— 此前 36 次运行**全部失败**，与 dependabot 无关）
- **`ci` 的 ruff 从未通过**：配置里的 `UP` 那组规则要求重写全库刻意的 %-格式化（194 处）等，
  实测 1012 处违规；且把片段源码当独立模块 lint，光假阳性 F821 就 645 处。
  → 规则收窄到 `E,F,W,I,B`（理由写在 `pyproject.toml` 注释里），**改为 lint 合并产物 `hub/hub.py`**
  （片段本就是半成品）。现在 ruff 本地全绿，并因此抓出上面两个真 bug。
- **`android` 从未构建成功**：runner 自带 Gradle **9.7.1**，而 AGP 8.9.2 只支持 Gradle 8.x，且仓库里
  没有 wrapper → 用系统 gradle 必挂。→ 显式钉 `gradle-version: 8.13`（顺带让产物可复现），加 `--stacktrace`。
- **`android` 引用了不存在的签名文件**：`keystore/debug.keystore` 不入库，AGP 校验签名时直接失败
  → 改成"文件存在才启用自用签名，否则退回 AGP 默认 debug 签名"。
- **`scorecard` 引用了已删除的标签** `github/codeql-action/upload-sarif@v3`（上游只剩 v4.x）→ 升 v4；
  Scorecard 本体是 Docker 容器（镜像从 ghcr.io 拉，实测 pull 失败）→ 标为**不阻断**并写明理由（它只是体检报告）。
- 工作流里的 action 统一升到当前主版本（`checkout@v7` / `setup-python@v7` / `setup-java@v6` /
  `cache@v6` / `upload-artifact@v7` / `gradle/actions@v6`）：Node 20 已进入强制迁移期。

### 变更（杂项）
- `VERSION` 从 `0.1.0`（早已过时）对齐到 `0.1.13`；`pyproject.toml` 的 `version` 同步（原来停在 0.1.6）。

## v0.1.12 — P2 传播与合规材料 + 采集器健康开关

### 新增（文档）
- **`docs/DEPLOY-GUIDE.md`**：部署图文教程（含权限逐个说明 + **常见报错对照表**）
- **`docs/DEMO-SCRIPT.md`**：2 分钟 demo 视频分镜（第一幕就是"她主动发来一条有用的消息"）
- **`docs/AWESOME-SUBMISSIONS.md`**：可粘贴的收录/推广文案（awesome-selfhosted YAML 条目、
  V2EX/少数派/Reddit 标题与正文），并在文末重申"不做买 star/刷榜"
- **`docs/FDROID.md`**：F-Droid 提交材料与自查表，重点写了最容易被标的
  `Tethered Network Services` anti-feature 怎么应对

### 变更（采集器 0.7.0）
- **健康数据开关默认改为「关」**：心率/血氧/压力属 GDPR 特殊类别数据，
  必须**显式同意**（设置页文案写明"打开即表示你同意采集"），不再靠系统权限弹窗代替同意。
- **修一个串门的门禁**：媒体通知（在听什么）此前被**健康开关**管着 ——
  关掉健康数据等于把音乐也关了。现在三段各归各位：音乐→曲名开关、健康→健康开关、订单→订单开关。

## v0.1.11 — 分桶 Thompson（P2 #32）

- **中枢 `GET /bands`**：把反馈按"场景桶"（星期×时段）分组估 `p(接受)`；
  **每桶样本 <4 条就标 `reliable=false`** —— 文献（EOPA arXiv:2608.04416）强调
  稀疏反馈要先分桶，但桶本身也要够样本，否则"一次运气就改阈值"。
- **说话层的期望效用 gate 优先用桶后验**（`_band_posterior()`，5 分钟缓存），
  桶不可靠时自动退回全局后验；决策理由里会标出来源（`[桶 工作日·早上]` / `[全局]`），
  复盘时能看清"这次是靠哪个后验做的决定"。

## v0.1.10 — 说话层接入文献算法（P2 #31）+ 两个真 bug

### 新增（4 项算法，都有出处）
- **断点投递**（Iqbal & Bailey 2007）：用 `screen.idle_minutes ≤ 2` + 上报很新 判断"人刚拿起手机"，
  那是天然断点 → 允许把间隔压到 0.6 倍。拿不到信号就不假装（只是不享受加成）。
- **Goldilocks 时间窗**（arXiv:2504.09332，MIT Media Lab 2025）：每个话题有自己的时段
  （睡点只在晚上、带伞只在早上、游戏盘点在下午到夜里）→ **发送前**检查，不在窗内就不说。
  定点提醒/上课/紧急走另一条路，不受影响。
- **期望效用 gate**（Horvitz 1999 CHI）：开口 iff `p(接受) > C_落空/(C_落空+C_漏报)`，
  `p(接受)` 用反馈的 **Beta 后验**估计（不再拍脑袋）；后验偏低时只有"料 ≥3"才允许开口。
  成本参数可用 `WHALE_C_MISS` / `WHALE_C_FALSE` 调。
- **打扰仪表盘**：`hubctl interruption` —— 开口率、接受率、钟点分布、最近 5 条理由。

### 修复（都是跑起来才暴露的真 bug）
- **决策日志一直是 0 条**：说话层那段上报代码写在 `return` **之后**（死代码），
  而且引用了两个没定义的变量。现在挪到正确位置并抽成 `_log_decision()`。
- **中枢根本没有 `POST /decision` 路由**：`decisions` 表和数据写入函数都在，
  但路由没接（CHANGELOG 声称的链路其实只通了一半）→ 补上并实测（POST 200 → GET 能查到）。
- `hubctl` 不认 `WHALE_HOME`（中枢认）→ 改为三级优先级对齐：`WHALE_DB` → `$WHALE_HOME/hub.db` → 脚本旁边。

## v0.1.9 — 走完成熟项目清单（数据主权 · 工程化 · 传播前置）

### 新增
- **`GET /export` / `POST /erase`**：数据可携带与物理删除（GDPR Art.20/17）。
  `?redact=1` 顺手脱敏（App 名/原文/坐标都剥掉）；删除必须显式 `confirm=ERASE-ALL`，
  删前自动备份、删后 VACUUM。**4012 行导出实测 0.04 秒**。
- **Python 3.11/3.12/3.13 矩阵 + 覆盖率门槛 + SBOM 步骤**（零依赖清单短到能人工通读）
- **OpenSSF Scorecard** workflow · **Android CI**（此前完全没有）· **dependabot**
- **Dockerfile + docker-compose.yml**（一键起中枢，数据挂 `./data`，删容器不删数据）
- **Fastlane 元数据**（zh-CN / en-US）· **`docs/SCHEMA.md` / `LOCAL-FIRST.md` / `ANDROID-RELEASE.md`**
- **README 30 秒上手 + 徽章 + 数据主权说明**

### 修复
- `/export` 遇 BLOB 字段会 500（bytes 不能 JSON 序列化）→ 转 base64；并跳过 FTS 影子表
- CI 的 `setup-python` 写死 3.11，加了矩阵却没生效 → 改为 `${{ matrix.python }}`

## v0.1.8 — 发布就绪与隐私回归

### 修复
- **给模型的上下文里带精确经纬度（真泄漏，被注入式测试抓到）**：`weather_now` 之前直接把
  存储里的 meta 原样塞进上下文，里面带着 `lat/lon`（那是取天气用的坐标，不该给模型）；
  现在改成**白名单**字段（城市/天气/湿度/风/降水/AQI/观测时刻 + 距今多少分钟），
  并且只要检测到存储里有坐标就记一条"进模型前剥掉了什么"。
- **CI 提示漏了一步**：产物等价性检查失败时，提示里只说"跑 build_single.py"，
  但该脚本只写 `hub/dist/hub.py`，**必须再 `cp` 回 `hub/hub.py`** —— 贡献者照着做还是会红。
  提示与 CONTRIBUTING 都补上了这一步。

### 新增
- **`tests/make_fake_history.py`**：合成历史库生成器（固定种子，只在临时目录建库）。
  覆盖 8 类坑：残缺日 / 整段缺失两周 / 连续同值（MAD=0）/ 单日 10 倍突变 /
  跨零点跨月 / 时区偏移 / App 改名 / 超长静默。
- **`tests/test_privacy_injection.py`**：**注入式**脱敏测试（15 条 PII 真的写进临时库，
  再断言它们不出现在 `llm_context()` 里）。比原来"样例不在库里当然搜不到"的泄漏探测强得多。
- **Android 发布签名**：`collector/app/build.gradle.kts` 支持从 `keystore.properties`
  读发布密钥（该文件不入库），没有密钥时 release 走未签名构建（**CI 与外部贡献者不会因此失败**）。
  新增 `collector/tools/make_release_keystore.sh` 一键生成密钥 + 写配置。
- **`docs/ANDROID-RELEASE.md`**：发布签名的完整步骤与"密钥丢失等于无法升级"的提醒。

## v0.1.7 — 开源就绪（工程卫生 + 单文件分发）

### 新增
- **`WHALE_HOME` 环境变量**：配置与数据库的位置不再硬编码。优先级
  `$WHALE_HOME` → 旧部署（`hub.json` 就在程序旁）→ `~/.whale`。
  **升级无感**：已有实例继续用原来的路径，不打扰。
- **单文件分发 `dist/whalecare.pyz`**：`python3 hub/tools/build_zipapp.py` 打成一个
  ~54KB 的可执行文件，用户 `python3 whalecare.pyz` 就能跑，不需要 venv/pip。
  **纯标准库**（标准库 zipapp），不引入 PyInstaller。
- **零依赖护栏 `hub/tools/check_no_deps.py`**：CI 里扫描所有运行时代码的顶层 import，
  出现第三方库就让构建失败。把"零依赖"从口号变成机器守卫。
- **核心算法单元测试 `tests/test_hub_core.py`**：18 个用例，覆盖 `_robust`
  （median+MAD 与 σ 下限）、`_day_series`（残缺日门槛）、`_surprise_of`
  （小样本收缩 + 低可信度收缩 + 单向判定）、`code_fingerprint`、`WHALE_HOME` 解析，
  以及**片段与产物的字节等价断言**。
- **开源标配文件**：`SECURITY.md`（威胁模型 + 已做防护 + 诚实列出已知弱点）、
  `CONTRIBUTING.md`（amalgamation 工作流 + 预提交钩子）、`.gitignore`（隐私数据与密钥）、
  Issue / PR 模板。
- **`pyproject.toml` 补全**：加 `[project]` / `[build-system]`，
  `dependencies = []` —— 机器可读的零依赖声明。

### 修复
- **说话层静默降级（P0）**：主模型（如 `gpt-5.6-luna`）稳定 500 时，此前只会在
  `WHALE_DEBUG` 下打印，用户唯一的感觉是"她今天说话变傻了"。现在：
  1. 支持 `model.fallback_models` 配置，主模型失败自动换备选；
  2. 全部失败时上报 `health.speaker_degraded` 到中枢 —— **用户能看见**。
- **`/opt/whale` 硬编码**：`speaker/whale_voice.py` 的角色卡/配置路径改为
  `WHALE_HOME` 优先、`/opt/whale` 兜底。`whale_voice.py` 现在也认
  `WHALE_CARD` / `WHALE_CONFIG` 单独指定。
- **zipapp 下的 `code_fingerprint()`**：`__file__` 指向压缩包内部时改为从
  zip 读取，不再返回 `"?"`。
- **CI 报错文案**：产物不等价时，直接打印可照抄的修复命令与差异摘要，
  外部贡献者不用猜"该跑哪个脚本"（此前 docstring 路径 `tools/build_single.py`
  与实际 `hub/tools/` 不符）。

## v0.1.6 — 运维与可观测
- **事件级幂等**：采集端带 `event_id` 时按 id 去重（比"值相同 + 60 秒窗口"更严；旧客户端自动兼容）
- **自动备份**：每日 04:00 打包 `hub.db` + 配置，滚存 7 份
- **日志轮转**：单文件超 50MB 压缩归档（此前曾涨到 209MB 无人管）
- **自检**：每日 08:00 查"数据源是否活着 / 她多久没说过话"，异常主动推送
- **脱敏回归测试集**：`tests/test_privacy_regression.py`（PII 样式 + 29 条真实形态样本）
- **报文版本字段**：采集端带 `v` 会记进 meta，日后改字段能判断对面版本

## v0.1.5 — 决策可回放
- **结构化决策日志**：`decisions` 表记"间隔/理由/料分/已说条数/场景桶"（不再只有一行中文）
- **FTS5 检索情节**：`/memory?q=`，中文用 trigram 分词器（≥3 字走 BM25，<3 字退回 LIKE）
- 索引是**可重建的派生数据**：分词方案变了就重建，原始 `episodes` 永不动

## v0.1.4 — 会记、会复盘、会问、能学
- 情节记忆（`episodes`）· 周/月复盘（本期 vs 上期 + 上期结论核对）
- 主动提问（有数据支撑、一天最多一个）· 反馈入口（挂件 ✓/✗ → Thompson 后验）
- 每日上限按命中率重算；后验**跨天保留**（修掉"每天零点清空学习成果"）

## v0.1.3 — 数据健康度与扩展
- 数据健康度（新鲜度/覆盖/可信度 → 直接放大基线收缩）
- 外挂扩展层 `hub/ext/`（一个文件 = 一个数据源，失败隔离）
- 人设包 `personas/`；代码片段化

## v0.1.2 — 决策算法
- 个人基线异常检测（median + MAD + 小样本收缩 + 残缺日门槛）
- 事实层去重（值未跨档不重复播报）· 同类提醒指数退避

## v0.1.1 — 第二批数据源与联网
- 在听什么 / 游戏 / 订单快递 / 电量 / 闹钟 · 联网查询四道闸 · 小设备一行接入

## v0.1.0 — 初版
- 手机采集 → 中枢脱敏 → 主动开口；课表 / 天气 / 简报 / 定点提醒
