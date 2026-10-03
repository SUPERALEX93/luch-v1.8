package proekt.luch.app

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.BufferedReader
import java.net.ConnectException
import java.net.HttpURLConnection
import java.net.SocketTimeoutException
import java.net.URL
import java.net.URLEncoder
import javax.net.ssl.SSLException

object Net {

    /** Человеческое объяснение последней сетевой ошибки вместо молчаливого «код 0». */
    @Volatile var lastError: String = ""
        private set

    private fun describe(e: Exception): String = when (e) {
        is SocketTimeoutException -> "сервер не отвечает дольше 6 секунд"
        is ConnectException ->
            "соединение отклонено: неверный адрес или порт, либо сервер не запущен"
        is SSLException -> "ошибка HTTPS: ${e.message ?: "сертификат не подходит"}"
        else -> "${e.javaClass.simpleName}: ${e.message ?: "сетевая ошибка"}"
    }

    private fun readBody(conn: HttpURLConnection): String {
        val code = conn.responseCode
        val stream = if (code in 200..299) conn.inputStream else conn.errorStream
        if (stream == null) return ""
        return stream.bufferedReader().use(BufferedReader::readText)
    }

    private fun open(url: String, method: String, body: String?, timeout: Int = 6000):
            Pair<JSONObject, Int> {
        val conn = URL(url).openConnection() as HttpURLConnection
        var code = 0
        val json: JSONObject
        try {
            conn.requestMethod = method
            conn.connectTimeout = timeout
            conn.readTimeout = timeout
            conn.useCaches = false
            if (body != null) {
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            }
            if (body != null) conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
            code = conn.responseCode
            val text = readBody(conn)
            json = try { if (text.isBlank()) JSONObject() else JSONObject(text) }
            catch (e: Exception) { JSONObject() }
        } finally {
            conn.disconnect()
        }
        return json to code
    }

    private fun query(params: Map<String, String>): String {
        val body = params.entries
            .filter { it.value.isNotEmpty() }
            .joinToString("&") {
                URLEncoder.encode(it.key, "UTF-8") + "=" + URLEncoder.encode(it.value, "UTF-8")
            }
        return if (body.isEmpty()) "" else "?" + body
    }

    suspend fun get(base: String, path: String, params: Map<String, String> = emptyMap(),
                    timeout: Int = 6000): Pair<JSONObject, Int> = withContext(Dispatchers.IO) {
        try { open(base + path + query(params), "GET", null, timeout).also { lastError = "" } }
        catch (e: Exception) { lastError = describe(e); JSONObject() to 0 }
    }

    suspend fun post(base: String, path: String, body: JSONObject, params: Map<String, String> = emptyMap(),
                     timeout: Int = 6000): Pair<JSONObject, Int> = withContext(Dispatchers.IO) {
        try { open(base + path + query(params), "POST", body.toString(), timeout).also { lastError = "" } }
        catch (e: Exception) { lastError = describe(e); JSONObject() to 0 }
    }
}
