package proekt.luch.app

import android.content.Context
import android.media.MediaRecorder
import android.os.Build
import android.util.Base64
import org.json.JSONObject
import java.io.File

/**
 * Запись голоса средствами Android, без браузерных API.
 *
 * Нужна потому, что в WebView доступ к микрофону возможен только на
 * «защищённом» адресе: https с доверенным сертификатом либо localhost.
 * Обычный http в локальной сети для микрофона не годится в принципе,
 * поэтому запись идёт нативно, а в страницу попадает уже готовый файл.
 */
class AudioRecorder(private val ctx: Context) {

    private var rec: MediaRecorder? = null
    private var outFile: File? = null

    @Suppress("DEPRECATION")
    private fun newRecorder(): MediaRecorder =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) MediaRecorder(ctx) else MediaRecorder()

    /** Возвращает пустую строку при успехе или текст ошибки. */
    fun start(): String {
        if (rec != null) return "запись уже идёт"
        var m: MediaRecorder? = null
        var f: File? = null
        return try {
            val file = File(ctx.cacheDir, "luch_voice_" + System.currentTimeMillis() + ".m4a")
            f = file
            val recorder = newRecorder()
            m = recorder
            recorder.setAudioSource(MediaRecorder.AudioSource.MIC)
            recorder.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            recorder.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            // Параметры, которые лучше всего понимает распознавание речи
            recorder.setAudioEncodingBitRate(64000)
            recorder.setAudioSamplingRate(16000)
            recorder.setOutputFile(file.absolutePath)
            recorder.prepare()
            recorder.start()
            rec = recorder
            outFile = file
            ""
        } catch (e: Exception) {
            // Освобождаем настоящий рекордер (не rec — он ещё null) и убираем файл.
            runCatching { m?.reset() }
            runCatching { m?.release() }
            f?.delete()
            rec = null
            outFile = null
            "не удалось начать запись: ${e.message ?: e.javaClass.simpleName}. " +
                "Проверьте, выдано ли приложению разрешение на микрофон."
        }
    }

    /** Возвращает JSON: {"ok":true,"b64":"...","mime":"audio/mp4"} либо {"ok":false,"error":"..."} */
    fun stopAndRead(): String {
        val m = rec ?: return JSONObject().put("ok", false).put("error", "запись не идёт").toString()
        val f = outFile
        rec = null
        outFile = null
        try {
            // stop() бросает исключение, если запись длилась меньше ~1 секунды
            m.stop()
        } catch (e: Exception) {
            m.reset()
            m.release()
            f?.delete()
            return JSONObject().put("ok", false)
                .put("error", "запись слишком короткая, подержите кнопку дольше").toString()
        }
        try {
            m.reset(); m.release()
        } catch (e: Exception) {
            // уже освобождён — ничего страшного
        }
        if (f == null || !f.exists() || f.length() == 0L) {
            return JSONObject().put("ok", false).put("error", "файл записи пустой").toString()
        }
        // Чтение файла тоже может упасть (нет места, файл пропал) — из метода
        // моста JavaScript исключение вылетать не должно.
        val b64 = try {
            Base64.encodeToString(f.readBytes(), Base64.NO_WRAP)
        } catch (e: Exception) {
            f.delete()
            return JSONObject().put("ok", false)
                .put("error", "не удалось прочитать запись: ${e.message ?: e.javaClass.simpleName}").toString()
        }
        f.delete()
        return JSONObject().put("ok", true).put("b64", b64).put("mime", "audio/mp4").toString()
    }

    fun isRecording(): Boolean = rec != null

    fun cancel() {
        val m = rec
        rec = null
        outFile?.delete()
        outFile = null
        try {
            m?.stop()
        } catch (e: Exception) {
            // запись не успела стартовать — просто освобождаем
        }
        try {
            m?.reset(); m?.release()
        } catch (e: Exception) {
            // уже освобождён
        }
    }
}