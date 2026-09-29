package dev.dpvoliin.whalecollector

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat

/**
 * 她的**通知出口**（2026-09-29 加）。
 *
 * 为什么要有它：微信那条路会限流 —— 实测 "iLink sendmessage rate limited" ✗
 * 一条提醒发不出去就等于没提醒 ✓ 而本地通知没人限你、也天然私密（不过第三方 ✓）。
 *
 * 设计要点：
 *   · 和微信**不重复**：中枢的待发队列按目标分开 ✓ 手机取 for=phone_vivo ✓ 微信取 for=weixin ✓
 *   · **先弹通知再回执** ✓ 顺序反了会漏（大忌：回执成功但用户没看到 ✓）
 *   · 通知上带 👍/👎 → 直接 POST /feedback ✓✓
 *     这是最大的收益：反馈是 Thompson 学习的唯一输入 ✓ 从"进 app 点"变成"点一下通知" ✓
 *   · 头像：优先用从 /asset 缓存下来的真图 ✓ 拿不到就回落 app 内置占位图 ✓
 */
object Notifier {

    const val CH_REMINDER = "whale_reminder"
    const val ACTION_GOOD = "dev.dpvoliin.whalecollector.FB_GOOD"
    const val ACTION_BAD = "dev.dpvoliin.whalecollector.FB_BAD"
    const val EXTRA_NID = "nid"
    const val EXTRA_TEXT = "text"

    /** 建渠道（Android 8+ 必须 ✓ 重复建是幂等的 ✓） */
    fun ensureChannel(ctx: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (nm.getNotificationChannel(CH_REMINDER) != null) return
        nm.createNotificationChannel(
            NotificationChannel(
                CH_REMINDER,
                "她的话",
                NotificationManager.IMPORTANCE_HIGH
            ).apply {
                description = "鲸鲸主动发来的提醒（不经过微信，不会被限流）"
                enableLights(true)
                enableVibration(true)
            }
        )
    }

    private fun icon(ctx: Context): Int =
        ctx.resources.getIdentifier("whale_avatar", "drawable", ctx.packageName)
            .takeIf { it != 0 } ?: android.R.drawable.ic_dialog_info

    /** 弹一条提醒。返回是否真的发出去了（权限被关时为 false ✓ 调用方据此决定要不要回执） */
    fun show(ctx: Context, nid: Int, text: String, kind: String = ""): Boolean {
        ensureChannel(ctx)
        val open = PendingIntent.getActivity(
            ctx, 0, Intent(ctx, SettingsActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        fun action(action: String, label: String, requestCode: Int): NotificationCompat.Action {
            val i = Intent(ctx, FeedbackReceiver::class.java).apply {
                this.action = action
                putExtra(EXTRA_NID, nid)
                putExtra(EXTRA_TEXT, text)
            }
            val pi = PendingIntent.getBroadcast(
                ctx, requestCode, i,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            return NotificationCompat.Action.Builder(0, label, pi).build()
        }
        val n: Notification = NotificationCompat.Builder(ctx, CH_REMINDER)
            .setSmallIcon(icon(ctx))
            // 有真头像就用作大图（通知上就是她本人 ✓ 取不到就留空 ✓ 不影响功能）
            .apply { runCatching { Assets.avatar(ctx)?.let { setLargeIcon(it) } } }
            .setContentTitle("鲸鲸")
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))   // 长句可展开 ✓
            .setAutoCancel(true)
            .setContentIntent(open)
            .addAction(action(ACTION_GOOD, "👍 说得对", 1))
            .addAction(action(ACTION_BAD, "👎 别说了", 2))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .build()
        return try {
            NotificationManagerCompat.from(ctx).notify(nid, n)
            true
        } catch (e: SecurityException) {
            // 通知权限被关了 —— 不吞掉，让状态行能如实告诉主人 ✓
            P.lastReport = "失败：通知权限没开，提醒发不出来"
            false
        }
    }

    fun cancel(ctx: Context, nid: Int) {
        runCatching { NotificationManagerCompat.from(ctx).cancel(nid) }
    }
}

/**
 * 通知上 👍/👎 的落点：直接回中枢的 /feedback。
 *
 * 中枢只认 `good` / `bad` 两个词（写死校验 ✓ 传别的会被 400 顶回 —— 2026-09 踩过）。
 * 反馈里带 **原文** → 中枢的归因窗口能对上"她说的哪一句被你否了" ✓
 * 这比"进 app 里点"的样本率高得多 ✓ 而样本正是她学得准不准的唯一输入 ✓
 */
class FeedbackReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        P.attach(ctx)
        val verdict = if (intent.action == Notifier.ACTION_BAD) "bad" else "good"
        val nid = intent.getIntExtra(Notifier.EXTRA_NID, 0)
        val text = intent.getStringExtra(Notifier.EXTRA_TEXT) ?: ""
        Notifier.cancel(ctx, nid)
        // 网络动作放后台线程（广播是主线程 ✓ 不能阻塞）
        Thread {
            runCatching { Hub.feedback(ctx, verdict, text) }
        }.start()
    }
}
