package proekt.luch.app

import android.content.Context
import org.json.JSONObject
import android.webkit.JavascriptInterface
import java.util.concurrent.Semaphore

class LuchBridge(private val ctx: Context) {

    private val prefs = Prefs(ctx)
    private val recorder = AudioRecorder(ctx)
    private var voice: VoiceListener? = null
    private var uploader: VoiceUploader? = null
    @Volatile private var uploading = false
    private val voiceSem = Semaphore(1)


    /** Отправка одной фразы; пока ИИ думает, новые фразы ждут в очереди. */
    fun uploadPhrase(b64: String) {
        val u = uploader ?: return
        voiceSem.acquire()
        uploading = true
        try {
            val err = u.upload(b64)
            if (err.isNotEmpty())
                CommandRunner(ctx).run("notify", JSONObject()
                    .put("title", "ЛУЧ: микрофон").put("text", err))
        } finally {
            uploading = false; voiceSem.release()
        }
    }

    @JavascriptInterface
    fun getDeviceName(): String = prefs.deviceName.ifEmpty { "Телефон" }

    /** Версия сборки — чтобы было видно, какое приложение реально стоит. */
    @JavascriptInterface
    fun getAppVersion(): String =
        "${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})"

    // ---- Микрофон: постоянное слушание, фразы выделяются сами ----

    fun attachVoice(v: VoiceListener, u: VoiceUploader) { voice = v; uploader = u }

    /** Возвращает пустую строку при успехе или текст ошибки. */
    @JavascriptInterface
    fun startListening(): String {
        val v = voice ?: return "слушатель не создан"
        if (v.isRunning()) return ""
        if (!hasMicPermission()) return "нет разрешения на микрофон"
        v.start()
        return if (v.isRunning()) "" else "микрофон занят другим приложением"
    }

    @JavascriptInterface
    fun stopListening() {
        voice?.stop()
    }

    /** Страница сообщает свою версию — так видно рассинхрон кэша и APK. */
    @JavascriptInterface
    fun reportPageVersion(v: String) {
        val app = BuildConfig.VERSION_NAME
        if (v.isBlank() || v == app) return
        CommandRunner(ctx).run("notify", JSONObject()
            .put("title", "ЛУЧ: обновите страницу")
            .put("text", "Страница: $v, приложение: $app. Закройте и снова откройте приложение."))
    }

    @JavascriptInterface
    fun isVoiceBusy(): Boolean = uploading

    /** Отправить фразу из страницы: работает и с нативным слушателем, и с записью на самой странице. */
    @JavascriptInterface
    fun sendAudioBase64(b64: String): String {
        if (b64.isBlank()) return "пустая запись"
        uploadPhrase(b64)
        return ""
    }

    @JavascriptInterface
    fun isListening(): Boolean = voice?.isRunning() ?: false


    @JavascriptInterface
    fun startRecording(): String = recorder.start()

    /** Возвращает JSON с готовым звуком в base64. */
    @JavascriptInterface
    fun stopRecording(): String = recorder.stopAndRead()

    @JavascriptInterface
    fun isRecording(): Boolean = recorder.isRecording()

    @JavascriptInterface
    fun hasMicPermission(): Boolean =
        ctx.checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) ==
            android.content.pm.PackageManager.PERMISSION_GRANTED

    @JavascriptInterface
    fun getBattery(): Int = Prefs.battery(ctx)

    @JavascriptInterface
    fun getServerUrl(): String = prefs.base()

    @JavascriptInterface
    fun getClientId(): String = prefs.clientId

    @JavascriptInterface
    fun isPhoneApp(): Boolean = true

    @JavascriptInterface
    fun showNotification(title: String, text: String): String {
        val (status, out) = CommandRunner(ctx).run(
            "notify", JSONObject().put("title", title).put("text", text))
        return if (status == "ok") out else "error: $out"
    }

    @JavascriptInterface
    fun vibrate(ms: Int): String {
        val (status, out) = CommandRunner(ctx).run("vibrate", JSONObject().put("ms", ms))
        return if (status == "ok") out else "error: $out"
    }
}
