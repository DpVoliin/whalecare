#!/usr/bin/env bash
# 推送前门禁：五道都过才允许推。
#
# 为什么要有它（2026-09-24 教训）：
#   我改坏了一个文件（BASE 定义被覆盖）→ 修复脚本自己写错括号没执行 → **我却直接推了** ✗
#   → 仓库里那份 speaker 一加载就 NameError，8 个测试挂，CI 红 ✗
#   顺序必须是"先验后推"，不能反着来 ✓ 这个脚本把顺序固定下来。
set -e
cd "$(dirname "$0")/../.."
echo "── ① lint（产物 + 片段）──"
ruff check .
ruff check hub/src/whalecare/
echo "── ② 全套测试 ──"
python3 -m unittest discover -s tests -q
echo "── ③ 版本一致性（六处）──"
python3 -m unittest tests.test_version_consistency -q
echo "── ④ 说话层策略回归（一周模拟）──"
python3 speaker/sim_week.py --days 7 --seed 42 --check
echo "── ⑤ 片段与产物逐字节等价 ──"
python3 hub/tools/build_single.py >/dev/null
cmp hub/dist/hub.py hub/hub.py
echo
echo "✓ 五道门禁全过，可以推了 ✓"
