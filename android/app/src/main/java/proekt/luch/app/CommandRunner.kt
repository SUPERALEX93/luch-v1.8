package proekt.luch.app

import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.media.AudioManager
import android.media.Ringtone
import android.media.RingtoneManager
import android.net.Uri
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import org.json.JSONObject
import java.util.concurrent.atomic.AtomicInteger

class CommandRunner(private val context: Context) {

    private val notifSeq = AtomicInteger(1000)
    private val phone = PhoneControl(context)
    private var ringing: Ringtone? = null

    fun run(name: String, args: JSONObject): Pair<String, String> = try {
        when (name) {
            "notify" -> notify(
                args.optString("title", "ЛУЧ"),
                args.optString("text", args.optString("message", "")))
            "vibrate" -> vibrate(args.optInt("ms", 500))
            "ring" -> ring()
            "open_url" -> openUrl(args.optString("url", ""))
            "battery" -> "ok" to "батарея ${Prefs.battery(context)}%"
            "location" -> "ok" to "координаты отправлены"
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

    private fun notify(title: String, text: String): Pair<String, String> {
        val body = if (text.isNotEmpty()) text else "Пустое уведомление"
        val id = notifSeq.incrementAndGet()
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
        runCatching { am.setStreamVolume(AudioManager.STREAM_ALARM, 100, 0) }
        runCatching { am.setStreamVolume(AudioManager.STREAM_RING, 100, 0) }
        val ringtone = RingtoneManager.getRingtone(context, uri)
        ringing = ringtone
        runCatching { ringtone.play() }
        Thread {
            Thread.sleep(8000)
            runCatching { ringtone.stop() }
            ringing = null
        }.start()
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
