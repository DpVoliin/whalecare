package dev.dpvoliin.whalecollector

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.OutputStreamWriter
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

/**
 * 上报器：把指标批量 POST 给中枢（`/ingest`），带 X-Token。
 *
 * 约定（与中枢一致）：一条指标 = {device, metric, value, unit?, ts?, meta?}
 * 失败不丢数据 —— 整批进本地队列，下次成功时先补发队列。
 */
object Hub {

    private val io = Executors.newSingleThreadExecutor()

    /** 异步发送；结果通过 onResult 回主线程（可为 null）。 */
    fun send(context: Context, items: List<JSONObject>, onResult: ((String) -> Unit)? = null) {
        if (items.isEmpty()) {
            onResult?.invoke("没有要上报的数据")
            return
        }
        val app = context.applicationContext
        io.execute {
            val msg = runCatching { doSend(app, items) }.getOrElse { "失败：${it.message?.take(60)}" }
            if (msg.startsWith("失败")) {
                P.enqueue(items)                                   // 失败：这批先攒着
            } else {
                val pend = P.drainQueue()                          // 成功：顺手补发历史
                if (pend.isNotEmpty()) {
                    val r2 = runCatching { doSend(app, pend) }.getOrElse { "失败" }
                    if (r2.startsWith("失败")) P.enqueue(pend)      // ★ 补发也失败 → 放回去，别丢！
                }
            }
            P.lastReport = msg
            android.os.Handler(android.os.Looper.getMainLooper()).post { onResult?.invoke(msg) }
        }
    }

    private fun doSend(context: Context, items: List<JSONObject>): String {
        val url = P.hubUrl
        if (url.isBlank() || P.token.isBlank()) return "失败：还没填中枢地址/token"
        val conn = TlsTofu.open(context, "$url/ingest").apply {   // ★ 统一出口：套上证书固定 ✓
            requestMethod = "POST"
            connectTimeout = 8000
            readTimeout = 10000
            doOutput = true
            setRequestProperty("Content-Type", "application/json; charset=utf-8")
            setRequestProperty("X-Token", P.token)
        }
        val body = JSONArray().apply { items.forEach { put(it) } }.toString()
        OutputStreamWriter(conn.outputStream, Charsets.UTF_8).use { it.write(body) }
        val code = conn.responseCode
        val text = (if (code in 200..299) conn.inputStream else conn.errorStream)
            ?.bufferedReader()?.use { it.readText() } ?: ""
        conn.disconnect()
        if (code !in 200..299) return "失败：HTTP $code ${text.take(60)}"
        val accepted = runCatching { JSONObject(text).optInt("accepted", items.size) }.getOrDefault(items.size)
        return "成功：上报 $accepted 条（${nowHm()}）"
    }

    private fun nowHm(): String =
        java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.US).format(java.util.Date())

    /** 造一条指标（统一出口，避免各处手写 key 写错）。 */
    fun metric(device: String, name: String, value: Any?, unit: String = "", meta: JSONObject? = null): JSONObject =
        JSONObject().apply {
            put("device", device)
            put("metric", name)
            put("value", value)
            put("unit", unit)
            put("ts", isoNow())
            if (meta != null) put("meta", meta)
        }

    fun isoNow(): String =
        java.time.OffsetDateTime.now(java.time.ZoneOffset.ofHours(8)).withNano(0).toString()

    // ─────────────────────────── 她的**通知出口**（2026-09-29 加）

    /** 取待发提醒（peek=1 → **不认领** ✓ 先弹通知成功了再回执 ✓ 顺序反了会漏提醒） */
    fun pending(context: Context): List<JSONObject> = runCatching {
        val url = P.hubUrl
        if (url.isBlank() || P.token.isBlank()) return emptyList()
        val conn = TlsTofu.open(context, "$url/pending?for=${P.deviceName}&limit=5&peek=1")
        conn.connectTimeout = 8000; conn.readTimeout = 10000
        conn.setRequestProperty("X-Token", P.token)
        val txt = conn.inputStream.bufferedReader().use { it.readText() }
        conn.disconnect()
        val arr = JSONObject(txt).optJSONArray("items") ?: return emptyList()
        (0 until arr.length()).mapNotNull { arr.optJSONObject(it) }
    }.getOrElse { emptyList() }

    /** 回执：告诉中枢"这条已经送到主人眼前了" ✓（不回执会重复发 ✓ 2026-09 踩过） */
    fun ack(context: Context, ids: List<Int>) {
        if (ids.isEmpty()) return
        runCatching {
            val url = P.hubUrl
            val conn = TlsTofu.open(context, "$url/ack")
            conn.requestMethod = "POST"
            conn.doOutput = true
            conn.connectTimeout = 8000; conn.readTimeout = 10000
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            conn.setRequestProperty("X-Token", P.token)
            val body = JSONObject().apply {
                put("ids", org.json.JSONArray(ids))
                put("for", P.deviceName)
            }.toString()
            conn.outputStream.use { it.write(body.toByteArray()) }
            conn.responseCode
            conn.disconnect()
        }
    }

    /** 反馈只认 good / bad（中枢写死校验 ✓ 传别的会被 400 顶回 ✓ 2026-09 踩过） */
    fun feedback(context: Context, verdict: String, text: String) {
        val v = if (verdict == "bad") "bad" else "good"
        runCatching {
            val url = P.hubUrl
            val conn = TlsTofu.open(context, "$url/feedback")
            conn.requestMethod = "POST"
            conn.doOutput = true
            conn.connectTimeout = 8000; conn.readTimeout = 10000
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            conn.setRequestProperty("X-Token", P.token)
            val body = JSONObject().apply {
                put("verdict", v)
                put("text", text.take(120))
                put("src", "notif")
            }.toString()
            conn.outputStream.use { it.write(body.toByteArray()) }
            conn.responseCode
            conn.disconnect()
        }
    }

    /**
     * 一轮"取提醒 → 弹通知 → 回执"。
     *
     * 顺序说明（重要 ✓）：**先弹通知，成功了才回执** ✗ 反过来 = 回执成功但通知被权限拦掉
     * → 提醒永远丢了、中枢还以为送达了 ✓ 最难查的那种 ✓
     */
    fun checkReminders(context: Context): Int {
        if (!P.notifyOn) return 0
        val items = pending(context)
        if (items.isEmpty()) return 0
        val acked = mutableListOf<Int>()
        for (it in items) {
            val id = it.optInt("id", 0)
            val text = (it.optString("text") ?: "").trim()
            if (id <= 0 || text.isEmpty()) continue
            val ok = Notifier.show(context, id, text, it.optString("kind") ?: "")
            if (ok) acked.add(id)
        }
        ack(context, acked)
        if (acked.isNotEmpty()) {
            P.lastReport = "收到她的 ${acked.size} 条提醒（${nowHm()}）"
        }
        return acked.size
    }
}
