package proekt.luch.app

import android.content.Context
import android.os.Build
import android.speech.tts.TextToSpeech
import android.util.Base64
import java.io.ByteArrayOutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.util.Locale
import org.json.JSONObject

/**
 * Отправка записанной фразы на сервер прямо из приложения.
 *
 * Раньше звук уходил через JavaScript страницы, и если страница в WebView
 * оставалась старой (кэш), фраза молча терялась. Теперь запрос делает сам
 * телефон: JS в этом пути не участвует вообще.
 */
class VoiceUploader(
    private val ctx: Context,
    private val baseProvider: () -> String,
    private val onResult: (JSONObject) -> Unit
) {

    private var tts: TextToSpeech? = null

    fun speak(text: String) {
        if (text.isBlank()) return
        if (tts == null) tts = TextToSpeech(ctx) { }
        try {
            tts?.language = Locale("ru", "RU")
            tts?.speak(text, TextToSpeech.QUEUE_FLUSH, null, "luch")
        } catch (e: Exception) {
            // озвучка не критична
        }
    }

    /** Возвращает "" при успехе или текст ошибки. */
    fun upload(wavBase64: String): String {
        val base = baseProvider().trimEnd('/')
        if (base.isEmpty()) return "не задан адрес сервера"
        val bytes = try {
            Base64.decode(wavBase64, Base64.DEFAULT)
        } catch (e: Exception) {
            return "не удалось прочитать запись"
        }
        val boundary = "----luch${System.currentTimeMillis()}"
        val body = ByteArrayOutputStream()
        try {
            body.write(("--$boundary\r\n").toByteArray())
            body.write("Content-Disposition: form-data; name=\"file\"; filename=\"phrase.wav\"\r\n".toByteArray())
            body.write("Content-Type: audio/wav\r\n\r\n".toByteArray())
            body.write(bytes)
            body.write("\r\n--$boundary--\r\n".toByteArray())
        } catch (e: Exception) {
            return "не удалось собрать запрос"
        }

        var conn: HttpURLConnection? = null
        return try {
            conn = URL("$base/api/voice").openConnection() as HttpURLConnection
            conn.requestMethod = "POST"
            conn.doOutput = true
            conn.connectTimeout = 10000
            conn.readTimeout = 90000      // ИИ думает долго
            conn.setChunkedStreamingMode(0)
            conn.setRequestProperty("Content-Type", "multipart/form-data; boundary=$boundary")
            conn.setRequestProperty("Connection", "close")
            conn.outputStream.use { it.write(body.toByteArray()) }

            val code = conn.responseCode
            if (code !in 200..299) {
                val err = conn.errorStream?.bufferedReader()?.use { it.readText() }.orEmpty()
                return "сервер ответил $code" + if (err.length > 160) ": " + err.take(160) else ""
            }
            val text = conn.inputStream.bufferedReader().use { it.readText() }
            val json = try { JSONObject(text) } catch (e: Exception) { return "непонятный ответ сервера" }
            onResult(json)
            ""
        } catch (e: Exception) {
            "не удалось отправить: ${e.message ?: e.javaClass.simpleName}"
        } finally {
            runCatching { conn?.disconnect() }
        }
    }

    fun release() {
        try { tts?.stop(); tts?.shutdown() } catch (e: Exception) { }
        tts = null
    }

    @Suppress("unused")
    private val sdk = Build.VERSION.SDK_INT
}