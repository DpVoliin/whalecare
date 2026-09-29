package dev.dpvoliin.whalecollector

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.widget.SwitchCompat
import androidx.core.content.ContextCompat

/**
 * 设置页：填中枢地址与 token、开开关、授权、立即同步、导入课表。
 *
 * 设计口径：**每个按钮只管一件事**（不搞"智能自动决策"），状态行只显示真实信息。
 */
class MainActivity : AppCompatActivity() {

    private lateinit var etHub: EditText
    private lateinit var etToken: EditText
    private lateinit var etDevice: EditText
    private lateinit var swUsage: SwitchCompat
    private lateinit var swCalendar: SwitchCompat
    private lateinit var swHealth: SwitchCompat
    private lateinit var swMusic: SwitchCompat      // 曲名开关
    private lateinit var swOrders: SwitchCompat     // 订单开关
    private lateinit var swNotify: SwitchCompat     // 用通知收她的提醒（不走微信 ✓）
    private lateinit var tvStatus: TextView

    private val pickTimetable = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri: Uri? ->
        if (uri == null) return@registerForActivityResult
        val text = runCatching {
            contentResolver.openInputStream(uri)?.bufferedReader()?.use { it.readText() }
        }.getOrNull()
        if (text.isNullOrBlank()) {
            toast("读不到文件内容")
            return@registerForActivityResult
        }
        if (!looksLikeBackup(text)) {
            toast("这不像岛课表导出的备份（要含 courses / periods）")
            return@registerForActivityResult
        }
        Thread {
            val ok = Collectors.pushTimetable(this, text)
            runOnUiThread {
                toast(if (ok) "课表已同步到中枢" else "课表同步失败（检查中枢地址/token）")
                refresh()
            }
        }.start()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        P.attach(this)
        setContentView(R.layout.activity_main)

        etHub = findViewById(R.id.etHub)
        etToken = findViewById(R.id.etToken)
        etDevice = findViewById(R.id.etDevice)
        swUsage = findViewById(R.id.swUsage)
        swCalendar = findViewById(R.id.swCalendar)
        swHealth = findViewById(R.id.swHealth)
        tvStatus = findViewById(R.id.tvStatus)

        // 回填
        etHub.setText(P.hubUrl)
        etToken.setText(P.token)
        etDevice.setText(P.deviceName)
        // 两个新开关：曲名 / 订单（隐私开关，默认开，可关）
        findViewById<androidx.appcompat.widget.SwitchCompat>(R.id.swMusic).also { swMusic = it }
        findViewById<androidx.appcompat.widget.SwitchCompat>(R.id.swOrders).also { swOrders = it }
        swNotify = findViewById(R.id.swNotify)
        swUsage.isChecked = P.usageOn
        swCalendar.isChecked = P.calendarOn
        swHealth.isChecked = P.healthOn
        swMusic.isChecked = P.musicTitleOn
        swOrders.isChecked = P.ordersOn
        swNotify.isChecked = P.notifyOn

        // ── 后台保活：能程序化的走官方 API，不能的跳厂商页面，状态如实显示 ✓
        val tvKeepAlive = findViewById<TextView>(R.id.tvKeepAlive)
        fun refreshKeepAlive() { tvKeepAlive.text = KeepAlive.statusText(this) }
        refreshKeepAlive()

        findViewById<Button>(R.id.btnBatteryExempt).setOnClickListener {
            KeepAlive.requestIgnoreBatteryOptimizations(this)
        }
        findViewById<Button>(R.id.btnAutoStart).setOnClickListener {
            KeepAlive.openAutoStart(this)
        }
        findViewById<Button>(R.id.btnBgPower).setOnClickListener {
            KeepAlive.openBackgroundPower(this)
        }
        findViewById<Button>(R.id.btnExactAlarm).setOnClickListener {
            KeepAlive.openExactAlarmSettings(this)
        }

        // 证书指纹：显示已固定值（可跟服务器 openssl 输出逐字核对 ✓）+ 重置入口
        findViewById<Button>(R.id.btnResetTls).setOnClickListener {
            TlsTofu.resetFingerprint(this)
            refresh()
            android.widget.Toast.makeText(this, "已重置，下次上报会重新学习中枢证书", android.widget.Toast.LENGTH_LONG).show()
        }

        findViewById<Button>(R.id.btnSave).setOnClickListener {
            P.hubUrl = etHub.text.toString()
            P.token = etToken.text.toString()
            P.deviceName = etDevice.text.toString().ifBlank { "phone_vivo" }
            P.usageOn = swUsage.isChecked
            P.calendarOn = swCalendar.isChecked
            P.healthOn = swHealth.isChecked
            // 顺手要一下蓝牙权限：给了才能读耳机/手表电量，不给也不影响其他采集
            runCatching {
                if (android.os.Build.VERSION.SDK_INT >= 31 &&
                    checkSelfPermission("android.permission.BLUETOOTH_CONNECT") !=
                    android.content.pm.PackageManager.PERMISSION_GRANTED) {
                    requestPermissions(arrayOf("android.permission.BLUETOOTH_CONNECT"), 91)
                }
            }
            P.musicTitleOn = swMusic.isChecked
            P.ordersOn = swOrders.isChecked
            P.notifyOn = swNotify.isChecked
            CollectorService.start(this)
            toast("已保存，采集服务已启动")
            refresh()
        }

        findViewById<Button>(R.id.btnSyncNow).setOnClickListener {
            val items = mutableListOf<org.json.JSONObject>()
            if (P.usageOn) items += Collectors.usage(this)
            if (P.calendarOn) items += Collectors.calendar(this)
            Hub.send(this, items) { msg ->
                toast(msg)
                refresh()
            }
        }

        findViewById<Button>(R.id.btnNotifAccess).setOnClickListener {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
        }
        findViewById<Button>(R.id.btnUsageAccess).setOnClickListener {
            startActivity(Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS))
        }
        findViewById<Button>(R.id.btnCalendarPerm).setOnClickListener {
            requestPermissions(arrayOf(Manifest.permission.READ_CALENDAR), 1)
        }
        findViewById<Button>(R.id.btnImportTt).setOnClickListener {
            pickTimetable.launch(arrayOf("application/json", "text/plain", "*/*"))
        }
        findViewById<Button>(R.id.btnStop).setOnClickListener {
            CollectorService.stop(this)
            toast("采集服务已停止")
            refresh()
        }

        ensureNotifPermission()
        refresh()
    }

    override fun onResume() {
        super.onResume()
        refresh()
    }

    private fun ensureNotifPermission() {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 2)
        }
    }

    /** 状态行：只写真信息（权限是否到位、上次上报结果、队列积压）。 */
    private fun refresh() {
        // 保活状态（每次刷新都更新 ✓ 从系统设置回来时 onResume → refresh 会走到这里）
        // ★ 一句话状态（2026-09-29 UI 重做）：让她"现在什么情况"一眼可见 ✓
        //   规则：先说最有用的那条 ✓ 全好就短一句 ✓ 不堆术语 ✓
        runCatching {
            val head = findViewById<TextView>(R.id.tvHeadline)
            val q = when {
                P.hubUrl.isBlank() || P.token.isBlank() -> "还没填中枢地址 ✓ 填完就能开始"
                else -> {
                    val rep = if (P.lastReport.isBlank()) "还没上报过" else P.lastReport
                    val qn = if (P.queueSize() > 0) " · 待补发 ${P.queueSize()} 条" else ""
                    val notif = if (P.notifyOn) "通知已开 ✓" else "通知关着（收不到提醒）"
                    "$rep$qn · $notif"
                }
            }
            head.text = q
        }
        // ★ 她的真头像（2026-09-29）：从中枢 /asset 拉，缓存在本地 ✓
        //   放**后台线程**取（网络不能压主线程 ✗）→ 拿到再回主线程换图 ✓
        //   取不到就保持布局里的**矢量占位** ✓（绝不让头像变空白 ✗）
        Thread {
            val bm = runCatching { Assets.avatar(this) }.getOrNull()
            if (bm != null) {
                runOnUiThread { runCatching {
                    findViewById<android.widget.ImageView>(R.id.ivAvatar).setImageBitmap(bm)
                } }
            }
        }.start()

        runCatching { findViewById<TextView>(R.id.tvKeepAlive).text = KeepAlive.statusText(this) }
        runCatching {
            val fp = TlsTofu.pinnedFingerprint(this)
            findViewById<TextView>(R.id.tvTlsFp).text =
                if (fp == null) "中枢证书指纹：还没连过（第一次成功上报后会自动记住 ✓）"
                else "中枢证书指纹（可跟服务器核对 ✓）：\n$fp"
        }
        val notifOn = runCatching {
            Settings.Secure.getString(contentResolver, "enabled_notification_listeners")?.contains(packageName) == true
        }.getOrDefault(false)
        val usageOn = Collectors.hasUsageAccess(this)
        val calOn = ContextCompat.checkSelfPermission(this, Manifest.permission.READ_CALENDAR) ==
            PackageManager.PERMISSION_GRANTED
        tvStatus.text = buildString {
            val ver = runCatching {
                packageManager.getPackageInfo(packageName, 0).versionName
            }.getOrDefault("?")
            append("版本：").append(ver).append('\n')
            append("中枢：").append(if (P.hubUrl.isBlank()) "未填写" else P.hubUrl).append('\n')
            append("上传加密：").append(if (P.hubUrl.startsWith("https")) "TLS + 证书固定 ✓" else "明文（建议改 https）").append('\n')
            append("通知监听（健康）：").append(if (notifOn) "已授权 ✓" else "未授权").append('\n')
            append("使用情况访问：").append(if (usageOn) "已授权 ✓" else "未授权").append('\n')
            append("日历读取：").append(if (calOn) "已授权 ✓" else "未授权").append('\n')
            append("待补发队列：").append(P.queueSize()).append(" 条").append('\n')
            append("当前采集节奏：").append(if (P.lastIntervalMinutes > 0) "每 ${P.lastIntervalMinutes} 分钟（随使用自动调整）" else "还没开始跑").append('\n')
            append("上次上报：").append(P.lastReport)
        }
    }

    private fun toast(s: String) = Toast.makeText(this, s, Toast.LENGTH_SHORT).show()

    /** 粗判是不是岛课表备份（避免用户选错文件后一头雾水）。 */
    private fun looksLikeBackup(text: String): Boolean =
        text.contains("\"courses\"") && text.contains("\"periods\"")
}
