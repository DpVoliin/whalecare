package dev.dpvoliin.whalecollector

import android.content.Intent
import android.os.Bundle
import android.widget.Button
import android.widget.ImageView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/**
 * 首页（2026-09-29 重排）。
 *
 * 这里**只放"看一眼就知道"的东西**：
 *   ① 她的形象（真图从中枢 /asset 拉 ✓ 缓存在本地 ✓ 拉不到就用矢量占位 ✓）
 *   ② 一句话状态（有事说事，没事就说没事 ✓ 不堆术语）
 *   ③ 她今天说过的话（中枢 /today 的 reminders ✓ 这是主人最想看的 ✓）
 *   ④ 连接情况（最后一次成功上报在多久之前 ✓ 待补发多少条 ✓）
 *   ⑤ 两个按钮（立即上报 / 设置）—— 按钮就两个 ✗ 需要更多就去设置 ✓
 *
 * 之前所有开关、授权、保活、导入课表都挪去 [SettingsActivity] ✓
 * （老界面连同它的逻辑**原样保留**，只是换了个入口 ✓ 这样零回归风险 ✓）
 */
class MainActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        P.attach(this)
        setContentView(R.layout.activity_home)
        runCatching { Notifier.ensureChannel(this) }

        findViewById<Button>(R.id.btnHomeSettings).setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }
        findViewById<Button>(R.id.btnHomeSync).setOnClickListener {
            // 让服务立刻跑一轮采集 + 顺手让她看一眼有没有要说的 ✓
            runCatching { startForegroundService(Intent(this, CollectorService::class.java)) }
            Thread { runCatching { Hub.checkReminders(this) } }.start()
            android.widget.Toast.makeText(this, "好，我去看一眼 ✓", android.widget.Toast.LENGTH_SHORT).show()
        }
        loadAvatar()
    }

    override fun onResume() {
        super.onResume()
        loadAvatar()
        refresh()
    }

    /** 真头像：后台取（网络不压主线程 ✓）→ 拿到再回主线程换图 ✓ 取不到就保持矢量占位 ✓ */
    private fun loadAvatar() {
        Thread {
            val bm = runCatching { Assets.avatar(this) }.getOrNull() ?: return@Thread
            runOnUiThread { runCatching { findViewById<ImageView>(R.id.ivHero).setImageBitmap(bm) } }
        }.start()
    }

    /** 状态 + 她今天说过的话（都放后台线程 ✓） */
    private fun refresh() {
        Thread {
            // ① 一句话状态：先说最有用的那条 ✓
            val status = when {
                P.hubUrl.isBlank() || P.token.isBlank() -> "还没填中枢地址 ✓ 去设置里填上就能开始"
                P.lastReport.startsWith("失败") -> P.lastReport
                else -> "我在 ✓ " + (if (P.notifyOn) "提醒会直接弹在通知里" else "通知关着（去设置里打开）")
            }
            // ② 连接卡
            val head = P.lastReport.ifBlank { getString(R.string.home_never) }
            val q = P.queueSize()
            val link = buildString {
                append("中枢：").append(if (P.hubUrl.isBlank()) "未填写" else "已配置 ✓").append('\n')
                append("上次：").append(head).append('\n')
                append("待补发：").append(q).append(" 条")
            }
            // ③ 她今天说过的话
            val d = runCatching { Hub.today(this) }.getOrNull()
            val said = buildString {
                val rs = d?.optJSONArray("reminders")
                val texts = (0 until (rs?.length() ?: 0)).mapNotNull { i ->
                    rs?.optJSONObject(i)?.optString("text")?.takeIf { it.isNotBlank() }
                }
                if (texts.isEmpty()) {
                    append(getString(R.string.home_said_empty))
                } else {
                    texts.takeLast(3).reversed().forEachIndexed { i, t ->
                        if (i > 0) append("\n\n")
                        append("· ").append(t)
                    }
                }
            }
            runOnUiThread {
                runCatching {
                    findViewById<TextView>(R.id.tvHomeStatus).text = status
                    findViewById<TextView>(R.id.tvLink).text = link
                    findViewById<TextView>(R.id.tvSaid).text = said
                }
            }
        }.start()
    }
}
