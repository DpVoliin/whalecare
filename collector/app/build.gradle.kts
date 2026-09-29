import java.util.Properties

// ── 发布签名 ──────────────────────────────────────────────────────────
// 从 collector/keystore.properties 读取（该文件**不入库**，见 .gitignore）。
// 没有它时：release 走未签名构建 —— CI 与外部贡献者不会因为你没放密钥而失败。
// 生成密钥：bash collector/tools/make_release_keystore.sh
val ksProps = Properties().apply {
    val f = rootProject.file("keystore.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}
val releaseStore = ksProps.getProperty("storeFile")
val hasReleaseKey = !releaseStore.isNullOrBlank() && file(releaseStore).exists()
if (!hasReleaseKey) {
    logger.lifecycle("[signing] 未找到 collector/keystore.properties → release 产物将是未签名版（本地调试不受影响）")
}

plugins {
    // ★ AGP 9 起**内置 Kotlin 支持**，不再需要（而且不允许）再 apply kotlin.android 插件。
    //   CI 里的原文报错：
    //     The 'org.jetbrains.kotlin.android' plugin is no longer required for Kotlin support
    //     since AGP 9.0. Solution: Remove it from app/build.gradle.kts.
    alias(libs.plugins.android.application)
}

android {
    namespace = "dev.dpvoliin.whalecollector"
    compileSdk = 37

    defaultConfig {
        applicationId = "dev.dpvoliin.whalecollector"
        minSdk = 26
        targetSdk = 37
        versionCode = 901
        versionName = "0.9.1"
    }

    // 与岛课表同一个调试签名：以后升级能直接覆盖安装，不用卸载
    // ★ 但这个文件**不入库**（见 .gitignore）→ CI / 外部贡献者机器上没有它。
    //   以前无条件引用它，AGP 在校验签名时会直接失败（本仓库 CI 12/12 全挂的原因之一）。
    //   所以：有文件才启用；没有就退回 AGP 自带的 debug 签名（CI 照样能出包）。
    val stableDebugKs = file("../keystore/debug.keystore")
    val hasStableDebug = stableDebugKs.exists()
    if (!hasStableDebug) {
        logger.lifecycle("[signing] 没有 collector/keystore/debug.keystore → 用 AGP 默认 debug 签名")
    }
    signingConfigs {
        if (hasReleaseKey) {
            create("release") {
                storeFile = file(releaseStore!!)
                storePassword = ksProps.getProperty("storePassword")
                keyAlias = ksProps.getProperty("keyAlias")
                keyPassword = ksProps.getProperty("keyPassword")
            }
        }
        // 自用调试签名（与岛课表同一个）：升级能覆盖安装，不用卸载
        if (hasStableDebug) {
            create("stableDebug") {
                storeFile = stableDebugKs
                storePassword = "android"
                keyAlias = "androiddebugkey"
                keyPassword = "android"
            }
        }
    }
    buildTypes {
        debug {
            if (hasStableDebug) {
                signingConfig = signingConfigs.getByName("stableDebug")
            }
        }
        release {
            // 采集器逻辑不复杂，**刻意不混淆**：用户/审计者能直接反编译核对"到底采了什么"。
            // 这里不再显式写 isMinifyEnabled/isShrinkResources —— 它们的默认值本来就是 false，
            // 而 AGP 9 的 Kotlin DSL 对这两个属性的命名动过，写死了反而会因名字变化而构建失败。
            // 真要开混淆，就在 AGP 9 的文档确认写法后再加回来。
            if (hasReleaseKey) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    // Kotlin 的 jvmTarget 不再手写：AGP 9 的内置 Kotlin 会跟随上面的 compileOptions（17）。
    // 少一个手写旋钮 = 少一处"两边版本不一致"的失败点；真需要单独指定时再按 AGP 9 文档加回。
}

dependencies {
    implementation(libs.androidx.core)
    implementation(libs.androidx.appcompat)
}
