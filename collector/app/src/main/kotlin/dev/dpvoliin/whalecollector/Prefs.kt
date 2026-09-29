package dev.dpvoliin.whalecollector

import android.content.Context
import org.json.JSONObject
import java.io.File

/**
 * 本地配置与待上报队列。全部只存本机；队列用于"中枢没连上时先攒着"。
 *
 * 只做三件事：读写配置、追加队列、把队列整批吐给上报器。
 */
object P {
    private const val F = "collector"

    // 预置好中枢地址与 token（私有自用 App，省得在手机上敲一长串；换服务器时在设置页改）
    private const val DEFAULT_HUB = ""
    private const val DEFAULT_TOKEN = ""

    var hubUrl: String
        get() {
            val v = sp().getString("hub", DEFAULT_HUB) ?: DEFAULT_HUB
            // 老版本存过 http 地址 → 自动迁到 https（不禁明文就会把上传全拦掉）
            if (v.startsWith("http://") && v.contains("YOUR_SERVER_IP")) {
                val fixed = v.replace("http://", "https://").replace(":11440", ":11443")
                sp().edit().putString("hub", fixed).apply()
                return fixed
            }
            return v
        }
        set(v) = sp().edit().putString("hub", v.trim().trimEnd('/')).apply()

    var token: String
        get() = sp().getString("token", DEFAULT_TOKEN) ?: DEFAULT_TOKEN
        set(v) = sp().edit().putString("token", v.trim()).apply()

    var deviceName: String
        get() = sp().getString("device", "phone_vivo") ?: "phone_vivo"
        set(v) = sp().edit().putString("device", v.trim()).apply()

    /** 各采集开关。 */
    var usageOn: Boolean
        get() = sp().getBoolean("usage", true)
        set(v) = sp().edit().putBoolean("usage", v).apply()

    var calendarOn: Boolean
        get() = sp().getBoolean("calendar", true)
        set(v) = sp().edit().putBoolean("calendar", v).apply()

    // 健康数据（心率/血氧/压力）= GDPR 里的"特殊类别数据"，**必须默认关**，
    // 由用户在设置页显式打开才算同意。别靠系统权限弹窗代替同意。
    var healthOn: Boolean
        get() = sp().getBoolean("health", false)
        set(v) = sp().edit().putBoolean("health", v).apply()

    /** 上报曲名？（关掉后只报"在听音乐"，不给歌名 —— 曲名算中等敏感）。 */
    var musicTitleOn: Boolean
        get() = sp().getBoolean("music_title", true)
        set(v) = sp().edit().putBoolean("music_title", v).apply()

    /** 采集订单/快递？（只报类型与金额区间，绝不存商品名）。 */
    /** 用**通知**收她的提醒（默认开 ✓ 不经过微信 → 不会被限流 ✓） */
    var notifyOn: Boolean
        get() = sp().getBoolean("notify", true)
        set(v) = sp().edit().putBoolean("notify", v).apply()

    var ordersOn: Boolean
        get() = sp().getBoolean("orders_on", true)
        set(v) = sp().edit().putBoolean("orders_on", v).apply()

    /** 睡眠记录（每行一条 JSON：推算作息用）。 */
    var sleepRecords: String
        get() = sp().getString("sleeprec", "") ?: ""
        set(v) = sp().edit().putString("sleeprec", v).apply()

    /** 本地存的课表（导入时留一份，用来判断"现在是不是在上课"）。 */
    var timetableJson: String
        get() = sp().getString("tt", "") ?: ""
        set(v) = sp().edit().putString("tt", v).apply()

    /** 当前采集节奏（分钟），由服务按"你在不在用"实时算出来，显示在设置页。 */
    var lastIntervalMinutes: Int
        get() = sp().getInt("interval", 0)
        set(v) = sp().edit().putInt("interval", v).apply()

    /** 上次上报结果（给设置页显示，便于排障）。 */
    var lastReport: String
        get() = sp().getString("last", "还没上报过") ?: ""
        set(v) = sp().edit().putString("last", v).apply()

    private fun sp() = app().getSharedPreferences(F, Context.MODE_PRIVATE)

    private var appRef: Context? = null
    fun attach(context: Context) {
        appRef = context.applicationContext
    }

    private fun app(): Context = appRef ?: throw IllegalStateException("P.attach(context) 没调用")

    private fun queueFile(): File = File(app().filesDir, "queue.jsonl")

    /** 上报失败时把这一批攒起来（最多留 500 条，避免无限增长）。 */
    fun enqueue(items: List<JSONObject>) {
        runCatching {
            val f = queueFile()
            val lines = items.joinToString("\n") { Sec.enc(it.toString()) }
            f.appendText(lines + "\n")
            val all = f.readLines()
            if (all.size > 500) f.writeText(all.takeLast(500).joinToString("\n") + "\n")
        }
    }

    /** 取出并清空队列（拿不到就返回空）。 */
    fun drainQueue(): List<JSONObject> {
        val f = queueFile()
        if (!f.isFile) return emptyList()
        val out = mutableListOf<JSONObject>()
        runCatching {
            f.readLines().forEach { line ->
                if (line.isNotBlank()) runCatching { out.add(JSONObject(Sec.dec(line))) }
            }
            f.delete()
        }
        return out
    }

    fun queueSize(): Int = runCatching { queueFile().let { if (it.isFile) it.readLines().size else 0 } }.getOrDefault(0)
}
