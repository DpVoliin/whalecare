# 贡献指南

谢谢你想帮鲸鲸变得更好。这个项目是一个人的自用系统，但它**欢迎外部贡献**——只是有几条这个项目特有的规矩，先读 5 分钟能省你一次 CI 红。

---

## 最重要的：amalgamation 工作流（**必读**）

鲸鲸的中枢是「**片段源码 → 合并成单文件**」的结构。这是刻意的设计（见 `docs/adr/`），不是历史包袱。

```
hub/src/whalecare/          ← 你改这里（15 个片段，数字前缀 = 合并顺序）
  ├── 00_header.py          导入与常量
  ├── 10_core.py            数据库与核心工具
  ├── 20_timetable.py       课表
  ├── 30_rules.py           规则引擎
  ├── 40_privacy.py         脱敏与分类
  ├── 50_weather.py         天气
  ├── 60_analysis.py        统计与异常检测   ← 最常改的
  ├── 70_care.py            主动关心逻辑
  ├── 80_persona.py         人设包
  ├── 90_ext.py             扩展层
  ├── 93_channels.py        出口通道
  ├── 95_scheduler.py       定时任务
  ├── 97_http.py            HTTP 端点
  ├── 98_admin.py           管理/状态页
  └── 99_main.py            入口
                      │
                      │  hub/tools/build_single.py   ← 机械合并（按文件名前缀排序）
                      ▼
hub/hub.py                 ← 生成产物（约 5,400 行），**必须提交**
hub/dist/hub.py            ← 合并中间产物，**不进 git**
```

### 两个 lint 门禁（有意的分工，别合并）

| 对象 | 命令 | 规则 | 说明 |
|---|---|---|---|
| **产物** `hub/hub.py` | `ruff check .` | 根 `pyproject.toml`，**严格** | CI 强制。它是完整模块，能抓到只有跨片段才暴露的真 bug（例：缺 `import pathlib`）|
| **片段** `hub/src/whalecare/` | `ruff check hub/src/whalecare/` | 片段自己的 `ruff.toml` | 片段不可独立导入（彼此靠全局变量通信），单文件会刷 **662 处** F821 假阳性；这份配置只关掉跨片段假阳性，保留其余真错误 |

> 新人最常见的两个坑：① 只改片段不重新合并 → CI 的"逐字节等价"必红；
> ② 用错门禁去看片段（`ruff check hub/src/whalecare/` 缺了 `--config` 那套）→ 误以为代码质量很差。
> 两者都有明确命令，照着上面这张表走就行。

### 改代码的正确姿势

```bash
# 1. 改片段
vim hub/src/whalecare/60_analysis.py

# 2. 重新合并（命令是 hub/tools/，不是 tools/）
python3 hub/tools/build_single.py        # 片段 → hub/dist/hub.py
cp hub/dist/hub.py hub/hub.py            # ★ 产物拷回 hub/hub.py（必须提交）

# 3. 确认产物一致
cmp hub/hub.py hub/dist/hub.py && echo "✅ 一致"

# 4. 两个文件一起提交
git add hub/src/whalecare/60_analysis.py hub/hub.py
```

**影响**：片段与产物必须**逐字节一致**，CI 会验证。改了片段忘了合并 → CI 红，报错会告诉你该跑哪条命令。

> 💡 **建议装 pre-commit hook**：让它在你本地自动合并，就不用记这套流程了。
> ```bash
> printf '#!/bin/sh\npython3 hub/tools/build_single.py && git add hub/hub.py\n' > .git/hooks/pre-commit
> chmod +x .git/hooks/pre-commit
> ```

### 为什么搞这么复杂？

三个理由（ADR-001 有完整讨论）：
1. **单文件分发**是核心卖点——用户 `python hub.py` 就能跑，不需要装包、不需要 venv
2. **片段化开发**让合并后那份长文件仍然能被人类阅读和维护
3. **字节等价断言**让"忘了合并"这种错误不可能溜进仓库

---

## 不知道从哪开始？

看 [`docs/COMMUNITY.md`](docs/COMMUNITY.md) —— 那里有版本策略（什么时候算 1.0）和一批
带**证据 + 验收标准**的小任务，每条都能直接认领。

## 推送前先过门禁（**别跳**）

```bash
bash hub/tools/prepush.sh      # 五道：lint(产物+片段) · 全套测试 · 版本一致性 · 说话层一周模拟 · 片段↔产物等价
```

为什么把它写成脚本：2026-09-24 我改坏过一个文件（`BASE` 定义被覆盖），修复脚本自己写错括号
没执行，**而我直接推了** —— 仓库里那份 speaker 一加载就 NameError，8 个测试挂、CI 红 ✗。
顺序必须是"先验后推"，不能反着来；这个脚本把顺序固定下来。

## 开发环境

```bash
git clone <repo>
cd whalecare
python3 -m venv .venv && . .venv/bin/activate

# 运行时零依赖，但开发需要：
pip install ruff        # lint + format

# 跑起来（会生成 hub.json 和 hub.db）
cd hub && python3 hub.py
```

**运行时依赖必须保持为零。** 这是硬约束——CI 有白名单检查（`ci.yml` 的"零依赖检查"步骤）。想加第三方库前，先开 issue 讨论，通常答案是"用标准库实现"。

## 跑测试

```bash
python3 -m unittest discover tests -v
```

现有测试：
- `tests/test_privacy_regression.py` — 脱敏回归（6 条 PII 规则 + 29 条真实形态）

**欢迎补测试**，尤其是 `hub/src/whalecare/60_analysis.py` 里的统计函数（当前覆盖不足，是项目最大的技术债）。

## 代码风格

- `ruff check .` 和 `ruff format .`（配置在 `pyproject.toml`，line-length 120）
- 中文注释、中文 docstring —— **这个项目的语言是中文**，请不要把注释改成英文
- 注释写"为什么"，不写"做了什么"（代码本身说的是"做了什么"）

## 提交信息

参考现有 `git log` 的风格：`v0.1 · 决策日志上报链路（说话层→中枢）+ POST /decision`

- 用中文
- 说清"改了什么 + 为什么"
- 改动大时在正文里展开

## Pull Request

PR 模板里有勾选项，最重要的三条：
1. ✅ 跑过 `python3 hub/tools/build_single.py` 并提交了 `hub/hub.py`
2. ✅ 跑过 `python3 -m unittest discover tests`
3. ✅ 更新了 `CHANGELOG.md`（如果是用户可感知的改动）

## 不做什么（non-goals）

先看 `docs/ROADMAP.md`。明确不做：多租户 SaaS、端侧大模型、社交/支付写操作、微信机器人公开分发。

如果你的想法落在这几项里，**大概率会被婉拒**，不是因为想法不好，是因为它会让项目偏离"个人自托管工具"这个定位。

## 有问题？

开 GitHub Discussions 的 Q&A，或直接开 issue。个人维护，响应可能慢，但都会看。
