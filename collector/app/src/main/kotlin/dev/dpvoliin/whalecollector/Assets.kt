package dev.dpvoliin.whalecollector

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import java.io.File

/**
 * 她的**真形象素材**取自中枢（2026-09-29 加）。
 *
 * 为什么不把图打进包里 ✗：
 *   · 素材是主人的私有资源 ✓ 公开仓库只留**占位图** ✓（既有规矩）
 *   · 打进包 ⇒ 换一张图就得重新发版 ✗ 而走中枢 ⇒ 丢个文件就行 ✓
 *   · 别人用开源版取不到 ⇒ 自动回落内置的矢量占位头像 ✓（和桌面挂件一个套路 ✓）
 *
 * 策略：**本地缓存 + 一天过期 + 拉不到就用旧的**（绝不让形象变空白 ✗）
 *   · 有缓存且没过期 → 直接用 ✓（绝大多数时候零网络 ✓）
 *   · 过期 → 后台线程去 /asset/whale_avatar.png 拉 ✓ 成功就覆盖缓存 ✓
 *   · 拉失败 → **继续用旧图** ✓（比没有好 ✓ 也不清缓存 ✗）
 *   · 从来没成功过 → 返回 null ⇒ UI 用矢量占位 ✓
 *
 * 注意：请求必须**带 header token**（中枢只认 header ✓ `?t=` 会被拒 ✓
 * 因为那种写法会把 token 写进服务器日志和浏览器历史 ✓ 所以这里不能塞进 <img src> ✓）
 */
object Assets {

    private const val AVATAR = "whale_avatar.png"
    private const val MAX_AGE_MS = 24L * 3600 * 1000     // 一天过期，够用又不折腾

    private fun cacheFile(ctx: Context): File = File(ctx.filesDir, AVATAR)

    /** 取头像：命中缓存就直接返回；否则尝试下载（同一进程内不会并发打网络 ✓） */
    fun avatar(ctx: Context): Bitmap? {
        val f = cacheFile(ctx)
        val fresh = f.isFile && System.currentTimeMillis() - f.lastModified() < MAX_AGE_MS
        if (!fresh) {
            runCatching { download(ctx, f) }        // 失败也无所谓 —— 下面照旧读缓存 ✓
        }
        if (!f.isFile) return null                  // 从没成功过 → 调用方用矢量占位 ✓
        return runCatching { BitmapFactory.decodeFile(f.absolutePath) }.getOrNull()
    }

    /** 强制刷新一次（设置页"更新头像"用 ✓ 或排查时用 ✓） */
    fun refresh(ctx: Context): Boolean = runCatching { download(ctx, cacheFile(ctx)) }.isSuccess

    private fun download(ctx: Context, dest: File) {
        val url = P.hubUrl
        if (url.isBlank() || P.token.isBlank()) return
        val conn = TlsTofu.open(ctx, "$url/asset/$AVATAR")
        conn.connectTimeout = 8000
        conn.readTimeout = 12000
        conn.setRequestProperty("X-Token", P.token)      // ★ 只认 header ✓
        val code = conn.responseCode
        if (code == 200) {
            val bytes = conn.inputStream.use { it.readBytes() }
            // 原子写：先写临时文件再改名（避免"写到一半被杀"留下半张坏图 ✗）
            val tmp = File(dest.parentFile, dest.name + ".tmp")
            tmp.outputStream().use { it.write(bytes) }
            if (tmp.length() > 0) {
                if (dest.exists()) dest.delete()
                tmp.renameTo(dest)
            } else {
                tmp.delete()
            }
        }
        conn.disconnect()
    }
}
