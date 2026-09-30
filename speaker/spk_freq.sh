#!/bin/bash
# 鲸鲸·频率开关（低频 / 标准 / 高频）
#
# 用法（三档都是**显式**的，脚本不替你猜；改完 2 分钟内生效，不用重启）：
#     bash spk_freq.sh            # 看现在是什么档 + 今天说了几条
#     bash spk_freq.sh quiet      # 静默：阈值 65 / 上限 4 条 / 间隔 ×2.50 / 不闲聊（只在早中晚三个时段）
#     bash spk_freq.sh low        # 低频：阈值 55 / 上限 9 条 / 间隔 ×1.70 / 不闲聊
#     bash spk_freq.sh normal     # 标准：阈值 38 / 上限 12 条 / 间隔 ×1.00 / 闲聊 1 条
#     bash spk_freq.sh high       # 高频：阈值 22 / 上限 24 条 / 间隔 ×0.60 / 闲聊 2 条
#
# 为什么是"档"而不是一个数字：档位同时改**开口阈值、每天上限、间隔倍率、闲聊额度**四样，
# 只调其中一样会出现"上限放开了但阈值还卡着，等于没变"这种假生效（踩过）。
# 紧急通道（磁盘快满/电量见底/气象预警/下节课快到）**不受档位限制** —— 该说的永远说。
S="/home/ubuntu/.hermes/scripts"
PY=/usr/bin/python3
MODE_FILE="$S/.whale_freq.json"
CARD_PY="$S/whale_salience.py"
[ -f "$CARD_PY" ] || CARD_PY="/home/ubuntu/whale-hub-oss/speaker/whale_salience.py"

LABEL=""
case "${1:-show}" in
  quiet|silent) LABEL="静默" ;;
  low)          LABEL="低频" ;;
  normal|std)   LABEL="标准" ;;
  high)         LABEL="高频" ;;
  ""|show|--show|-s|status) LABEL="" ;;
  *) echo "  用法：bash spk_freq.sh [quiet|low|normal|high|show]"; exit 1 ;;
esac

if [ -n "$LABEL" ]; then
  $PY - "$1" "$MODE_FILE" "$CARD_PY" <<'PYEOF'
import json, pathlib, sys
what, mode_file, card_py = sys.argv[1], sys.argv[2], sys.argv[3]
mode = "normal" if what in ("normal", "std") else what
# 直接借打分引擎的档位表（同一份定义，不许两处各写一套）
mods = None
try:
    import importlib.util as iu
    spec = iu.spec_from_file_location("sal_tool", card_py)
    m = iu.module_from_spec(spec); spec.loader.exec_module(m)
    mods = m.MODES
except Exception as e:
    print("  读档位表失败：%s" % e); sys.exit(1)
if mode not in mods:
    print("  未知档位：%s" % mode); sys.exit(1)
tmp = mode_file + ".tmp"
pathlib.Path(tmp).write_text(json.dumps({"mode": mode}, ensure_ascii=False), encoding="utf-8")
import os; os.replace(tmp, mode_file)
c = mods[mode]
print("  ✓ 已切到【%s】：开口阈值 %d / 紧急 %d / 每天上限 %d 条 / 间隔 ×%.2f / %s"
      % (c["label"], c["speak"], c["urgent"], c["cap"], c["gap_mult"],
         ("闲聊 %d 条" % c["chat"]) if c["chat"] else "不闲聊"))
PYEOF
  echo "  （说话层最迟 2 分钟内按新档位工作；下面看它有没有读到）"
  sleep 100
fi

echo "  ── 当前档位 ──"
$PY - "$MODE_FILE" "$CARD_PY" <<'PYEOF'
import json, sys, pathlib
mode_file, card_py = sys.argv[1], sys.argv[2]
mods = {"quiet": {"label": "静默", "speak": 65, "urgent": 85, "cap": 4,  "gap_mult": 2.50, "chat": 0},
        "low": {"label": "低频", "speak": 55, "urgent": 72, "cap": 9,  "gap_mult": 1.70, "chat": 0},
        "normal": {"label": "标准", "speak": 38, "urgent": 62, "cap": 12, "gap_mult": 1.00, "chat": 1},
        "high": {"label": "高频", "speak": 22, "urgent": 50, "cap": 24, "gap_mult": 0.60, "chat": 2}},
try:
    import importlib.util as iu
    spec = iu.spec_from_file_location("sal_tool", card_py)
    m = iu.module_from_spec(spec); spec.loader.exec_module(m)
    mods = m.MODES
except Exception:
    pass
try:
    cur = (json.loads(pathlib.Path(mode_file).read_text()) or {}).get("mode") or "normal"
except Exception:
    cur = "normal（还没设过 → 默认标准）"
c = mods.get(cur if cur in mods else "normal", mods["normal"])
print("  档位：%s" % c["label"])
print("  开口阈值 %d / 紧急 %d / 每天上限 %d 条 / 间隔 ×%.2f / %s"
      % (c["speak"], c["urgent"], c["cap"], c["gap_mult"],
         ("闲聊 %d 条" % c["chat"]) if c["chat"] else "不闲聊"))
PYEOF

echo "  ── 今天实际表现 ──"
$PY - <<'PYEOF'
import json, pathlib, time
S = pathlib.Path("/home/ubuntu/.hermes/scripts")
today = time.strftime("%Y-%m-%d")
try:
    h = json.loads((S / ".whale_health.json").read_text())
    if h.get("day") == today:
        print("  已开口 %s 条（最近一次 %s）" % (h.get("sent", 0), str(h.get("last_sent_ts") or "")[11:16]))
        b = h.get("blocked") or {}
        if b:
            print("  被拦下：%s" % "、".join("%s %s 次" % (k, v) for k, v in b.items()))
    else:
        print("  （今天的健康账还没开始记）")
except Exception as e:
    print("  读健康账失败：%s" % e)
try:
    p = json.loads((S / ".whale_pace.json").read_text())
    if p.get("day") == today:
        print("  主动说话计数：%s 句（连续沉默 %s 次）" % (p.get("said", 0), p.get("silent", 0)))
except Exception:
    pass
try:
    d = json.loads((S / ".whale_salience.json").read_text())
    r = d.get("repeats") or {}
    if r:
        print("  同一件事已重复：%s" % "、".join("%s×%s" % (k, v) for k, v in list(r.items())[:6]))
    n = d.get("nag") or {}
    if n:
        print("  老问题降噪：%s" % "、".join("%s 第%s天%s" % (k, v.get("days"),
              "（已收口）" if v.get("closed") else "") for k, v in n.items()))
except Exception:
    pass
PYEOF

if [ -n "$LABEL" ]; then
  echo "  ── 说话层日志（最近的档位相关行）──"
  grep -a "频率档\|开口价值\|紧急开口" "$S/whale_speaker.log" 2>/dev/null | tail -4 | cut -c1-140
fi
