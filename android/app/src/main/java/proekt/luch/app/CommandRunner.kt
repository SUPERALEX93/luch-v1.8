package proekt.luch.app

import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.location.Location
import android.location.LocationManager
import android.media.AudioManager
import android.media.Ringtone
import android.media.RingtoneManager
import android.net.Uri
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import androidx.core.app.NotificationManagerCompat
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import java.util.concurrent.atomic.AtomicInteger

class CommandRunner(private val context: Context) {

    private val notifSeq = AtomicInteger(1000)
    private val notifIds = java.util.concurrent.ConcurrentHashMap<String, Int>()
    private val phone = PhoneControl(context)
    private val prefs = Prefs(context)
    private var ringing: Ringtone? = null

    fun run(name: String, args: JSONObject): Pair<String, String> = try {
        when (name) {
            "notify" -> notify(
                args.optString("title", "ЛУЧ"),
                args.optString("text", args.optString("message", "")))
            "vibrate" -> vibrate(args.optInt("ms", 500))
            "ring" -> ring()
            "open_url" -> openUrl(args.optString("url", ""))
            "battery" -> "ok" to "заряд батареи: ${Prefs.batteryText(context)}"
            "location" -> sendLocation()
            // управление телефоном по командам ИИ
            "status" -> phone.status()
            "wake" -> phone.wake()
            "torch" -> phone.torch(!isFalse(args))
            "volume" -> phone.volume(
                args.optString("action", "up"),
                if (args.has("level")) args.optInt("level") else null,
                args.optString("stream", null))
            "brightness" -> phone.brightness(if (args.has("level")) args.optInt("level") else 50)
            "open_app" -> phone.openApp(args.optString("name", args.optString("app", "")))
            "apps" -> phone.apps(args.optString("filter", null))
            "clipboard" -> phone.clipboard(args.optString("text", null), args.optBoolean("get", false))
            "dial" -> phone.dial(args.optString("number", ""))
            "share" -> phone.share(args.optString("text", ""), args.optString("title", null))
            "toast" -> phone.toast(args.optString("text", ""))
            "open_settings" -> phone.openSettings(args.optString("what", null))
            "stop_ring" -> stopRing()
            else -> "error" to "телефон не умеет команду «$name»"
        }
    } catch (e: Exception) {
        "error" to (e.message ?: e.javaClass.simpleName)
    }

    /**
     * Отправить на сервер последнюю известную геопозицию и честно отчитаться.
     *
     * Раньше команда возвращала «координаты отправлены», ничего не отправляя:
     * сервер считал, что получил позицию, хотя её не было.
     */
    private fun sendLocation(): Pair<String, String> {
        val lm = context.getSystemService(Context.LOCATION_SERVICE) as? LocationManager
            ?: return "error" to "служба геолокации недоступна"
        // Тип не указываем явно: после elvis-оператора Kotlin выведет non-null Location.
        val best = runCatching {
            listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER,
                LocationManager.PASSIVE_PROVIDER)
                .mapNotNull { runCatching { lm.getLastKnownLocation(it) }.getOrNull() }
                .maxByOrNull { it.time }
        }.getOrNull()
            ?: return "error" to "нет известных координат — включи геолокацию и открой карту"
        if (prefs.serverUrl.isEmpty() || prefs.clientId.isEmpty())
            return "error" to "устройство ещё не зарегистрировано на сервере"
        val payload = JSONObject()
            .put("lat", best.latitude)
            .put("lng", best.longitude)
            .put("accuracy",
                if (best.hasAccuracy()) best.accuracy.toDouble() else JSONObject.NULL)
        val (_, code) = runBlocking {
            Net.post(prefs.base(), "/api/device/location", payload,
                mapOf("client_id" to prefs.clientId, "token" to prefs.token))
        }
        return if (code == 200)
            "ok" to "координаты отправлены: %.5f, %.5f".format(best.latitude, best.longitude)
        else "error" to "не удалось отправить координаты (код $code)"
    }

    private fun notify(title: String, text: String): Pair<String, String> {
        // Начиная с Android 13 уведомления можно запретить. Без этой проверки
        // команда рапортовала «ok», хотя пользователь ничего не видел.
        if (!NotificationManagerCompat.from(context).areNotificationsEnabled()) {
            return "error" to "уведомления запрещены — разрешите их ЛУЧ в настройках Android"
        }
        val body = if (text.isNotEmpty()) text else "Пустое уведомление"
        // ID стабилен для одного заголовка: повторные уведомления заменяют друг
        // друга. Раньше ID только рос, и уведомления копились бесконечно.
        val id = notifIds.getOrPut(title) { notifSeq.incrementAndGet() }
        val intent = Intent(context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP)
        val pending = android.app.PendingIntent.getActivity(
            context, id, intent,
            android.app.PendingIntent.FLAG_UPDATE_CURRENT or
                    android.app.PendingIntent.FLAG_IMMUTABLE)
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
            android.app.Notification.Builder(context, App.CHANNEL_ID)
        else android.app.Notification.Builder(context)
        builder.setContentTitle(title.ifEmpty { "ЛУЧ" })
            .setContentText(body)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setAutoCancel(true)
            .setContentIntent(pending)
        context.getSystemService(NotificationManager::class.java).notify(id, builder.build())
        return "ok" to "уведомление показано: «$body»"
    }

    private fun vibrate(ms: Int): Pair<String, String> {
        val dur = ms.coerceIn(50, 10000).toLong()
        val vib = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S)
            context.getSystemService(VibratorManager::class.java).defaultVibrator
        else @Suppress("DEPRECATION")
            context.getSystemService(Vibrator::class.java)
        if (!vib.hasVibrator()) return "error" to "на этом телефоне нет вибрации"
        vib.vibrate(VibrationEffect.createOneShot(dur, VibrationEffect.DEFAULT_AMPLITUDE))
        return "ok" to "вибрация ${dur} мс"
    }

    private fun isFalse(args: JSONObject): Boolean =
        args.has("on") && !args.optBoolean("on", true)

    private fun stopRing(): Pair<String, String> {
        runCatching { ringing?.stop() }
        ringing = null
        return "ok" to "звонок остановлен"
    }

    private fun ring(): Pair<String, String> {
        val uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
            ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
            ?: return "error" to "в системе нет рингтона"
        val am = context.getSystemService(AudioManager::class.java)
        runCatching { am.setStreamVolume(AudioManager.STREAM_ALARM, am.getStreamMaxVolume(AudioManager.STREAM_ALARM), 0) }
        runCatching { am.setStreamVolume(AudioManager.STREAM_RING, am.getStreamMaxVolume(AudioManager.STREAM_RING), 0) }
        val ringtone = RingtoneManager.getRingtone(context, uri)
        // Гасим предыдущий звонок: иначе два рингтона накладываются друг на друга.
        runCatching { ringing?.stop() }
        ringing = ringtone
        runCatching { ringtone.play() }
        Thread {
            Thread.sleep(8000)
            runCatching { ringtone.stop() }
            // Сбрасываем ссылку, только если это всё ещё НАШ звонок. Иначе поток
            // старого звонка обнулял бы ссылку на новый, и stop_ring не смог бы
            // его остановить — телефон продолжал бы звонить.
            synchronized(this) { if (ringing === ringtone) ringing = null }
        }.apply { isDaemon = true }.start()
        return "ok" to "телефон звонит 8 секунд"
    }

    private fun openUrl(url: String): Pair<String, String> {
        var u = url.trim()
        if (u.isEmpty()) return "error" to "не передана ссылка"
        if (!u.startsWith("http://") && !u.startsWith("https://")) u = "https://$u"
        val intent = Intent(Intent.ACTION_VIEW, Uri.parse(u))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        try {
            context.startActivity(intent)
        } catch (e: Exception) {
            return "error" to "нечем открыть ссылку"
        }
        return "ok" to "открыл $u"
    }
}
