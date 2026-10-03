package proekt.luch.app

import android.content.Context
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.json.JSONObject

class DevicePoller(private val context: Context) {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val prefs = Prefs(context)
    private val runner = CommandRunner(context)
    private var job: Job? = null
    private var doneSeq = 0
    /** Вызывается, если пришлось сменить адрес сервера: нужно перезагрузить сайт. */
    var onBaseChanged: ((String) -> Unit)? = null
    @Volatile var lastError: String = ""
    @Volatile var online: Boolean = false

    private fun note(msg: String) {
        lastError = msg
        android.util.Log.i("LuchDevice", msg)
    }

    private suspend fun submit(id: String, status: String, output: String) {
        // cmd_id/status/output сервер ждёт в теле JSON, а не в адресе
        val (res, code) = Net.post(
            prefs.base(), "/api/device/result",
            JSONObject()
                .put("cmd_id", id)
                .put("status", status)
                .put("output", output),
            mapOf("client_id" to prefs.clientId, "token" to prefs.token))
        if (code != 200) note("не удалось отправить результат: ${Net.lastError.ifEmpty { "код $code" }}")
    }

    fun start() {
        if (job?.isActive == true) return
        job = scope.launch {
            var since = prefs.lastSeq
            // Микрофон теперь работает и по обычному http, поэтому подключение
            // не считаем поломкой. Но после обновления приложения состав
            // возможностей меняется — тогда регистрируемся заново, иначе сервер
            // до конца не узнает, что телефон теперь умеет что-то новое.
            val build = BuildConfig.VERSION_NAME
            if (prefs.registered && prefs.registeredVersion != build) {
                prefs.registered = false
            }
            while (isActive) {
                try {
                    if (prefs.serverUrl.isEmpty()) {
                        note("сервер не задан")
                        delay(2000); continue
                    }
                    if (!prefs.registered) {
                        if (!register()) { delay(4000); continue }
                        since = prefs.lastSeq
                    }
                    val (pack, code) = Net.get(
                        prefs.base(), "/api/device/queue",
                        mapOf(
                            "client_id" to prefs.clientId,
                            "token" to prefs.token,
                            "since" to since.toString(),
                            "battery" to Prefs.battery(context).toString()))

                    if (code == 401 || code == 404) {
                        prefs.registered = false
                        note("сервер не узнал телефон (код $code), регистрирую заново")
                        delay(1500); continue
                    }
                    if (code == 200) {
                        if (!online) note("на связи с сервером ${prefs.base()}")
                        online = true
                        if (pack.optBoolean("reset", false)) {
                            // Сервер потерял состояние и его счётчик seq откатился.
                            // Сохранив старый since, мы бы отбрасывали новые команды
                            // и вечно отвечали бы «уже выполнялось раньше».
                            doneSeq = 0
                            since = 0
                            prefs.lastSeq = 0
                            note("сервер сбросил очередь, продолжаю с нуля")
                        }
                        val arr = pack.optJSONArray("commands")
                        if (arr != null && arr.length() > 0) {
                            for (i in 0 until arr.length()) {
                                val item = arr.optJSONObject(i) ?: continue
                                val id = item.optString("id")
                                val name = item.optString("command")
                                val args = item.optJSONObject("args") ?: JSONObject()
                                val seq = item.optInt("seq", 0)
                                // Сервер может прислать команду повторно, если не дождался
                                // нашего ответа. Повторно выполнять её нельзя.
                                if (seq > 0 && seq <= doneSeq) {
                                    submit(id, "ok", "уже выполнялось раньше")
                                    continue
                                }
                                if (seq > 0) doneSeq = seq
                                val (status, output) = runner.run(name, args)
                                submit(id, status, output)
                            }
                        }
                        since = pack.optInt("seq", since)
                        prefs.lastSeq = since
                    } else if (code == 0) {
                        online = false
                        note("сервер недоступен по адресу ${prefs.base()}")
                    } else {
                        online = false
                        note("сервер ответил кодом $code")
                    }
                } catch (e: Exception) {
                    online = false
                    note("ошибка связи: ${e.message ?: e.javaClass.simpleName}")
                }
                delay(2500)
            }
        }
    }

    fun stop() {
        job?.cancel()
        job = null
    }

    /** Адрес может отличаться от рабочего схемой или портом: сервер отдаёт ещё и
     *  HTTPS на соседнем порту (без него не работает микрофон). Перебираем варианты,
     *  чтобы пользователю не приходилось угадывать. */
    private fun candidateBases(): List<String> {
        val raw = prefs.serverUrl.trim().trimEnd('/')
        if (raw.isEmpty()) return emptyList()
        val withScheme = if (raw.startsWith("http://") || raw.startsWith("https://")) raw else "http://$raw"
        val hostPort = withScheme.substringAfter("://").substringBefore('/')
        val host = hostPort.substringBefore(':')
        val port = hostPort.substringAfter(':', "").toIntOrNull()
        val ok = { p: Int -> p in 1..65535 }
        val out = LinkedHashSet<String>()
        if (port == null) {
            out.add(withScheme)
            out.add("https://$host")
            out.add("http://$host")
        } else {
            // localhost — единственный http-адрес, который браузер считает
            // защищённым. Работает через "adb reverse tcp:%d tcp:%d" и не требует
            // никакого сертификата, поэтому пробуем его первым.
            if (host != "localhost" && host != "127.0.0.1") {
                out.add("http://localhost:$port")
                if (ok(port + 1)) out.add("https://localhost:${port + 1}")
            }
            if (withScheme.startsWith("http://")) out.add("https://$host:${port + 1}")
            out.add(withScheme)
            out.add("https://$host:$port")
            out.add("http://$host:${port + 1}")
            if (ok(port - 1)) out.add("http://$host:${port - 1}")
        }
        return out.toList()
    }

    private suspend fun register(): Boolean {
        val cands = candidateBases()
        if (cands.isEmpty()) { note("сервер не задан"); return false }
        val body = JSONObject()
            .put("name", prefs.deviceName.ifEmpty { defaultName() })
            .put("kind", "android")
            .put("token", prefs.token)
            .put("client_id", prefs.clientId)
            .put("caps", org.json.JSONArray(prefs.caps))
            .put("model", prefs.model())
            .put("os", prefs.os())

        var why = ""
        for (base in cands) {
            val (res, code) = Net.post(base, "/api/device/register", body, emptyMap(), 4000)
            if (code != 200) {
                why = if (Net.lastError.isNotEmpty()) Net.lastError else "код $code"
                note("не отвечает $base — $why")
                continue
            }
            // нашли живой адрес — запоминаем и открываем сайт именно на нём,
            // даже если он совпал с введённым: он мог смениться при переподключении
            prefs.serverUrl = base
            onBaseChanged?.invoke(base)
            prefs.clientId = res.optString("client_id")
            prefs.token = res.optString("token")
            prefs.deviceName = res.optString("name", prefs.deviceName)
            prefs.registered = true
            note("зарегистрирован как ${prefs.clientId}")
            return true
        }
        note("регистрация не прошла: $why")
        return false
    }

    /** Защищённый адрес: https либо localhost (localhost браузер считает
     *  защищённым даже по http, и сертификат не нужен). */
    private fun isSecureBase(b: String): Boolean =
        b.startsWith("https://") || b.contains("localhost") || b.contains("127.0.0.1")

    private fun defaultName(): String = "${prefs.model().substringBefore(' ')} (телефон)"
}
