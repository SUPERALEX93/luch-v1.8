package proekt.luch.app

import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ResolveInfo
import android.hardware.camera2.CameraManager
import android.media.AudioManager
import android.net.Uri
import android.os.BatteryManager
import android.os.Build
import android.provider.Settings
import android.view.WindowManager
import android.widget.Toast
import org.json.JSONObject

/**
 * Управление телефоном по командам от ИИ.
 *
 * Здесь только то, что Android разрешает без прав администратора: включить
 * экран, фонарик, звук, яркость, запустить приложение, открыть настройки.
 * Звонок не совершается — только открывается звонилка с номером.
 */
class PhoneControl(private val ctx: Context) {

    private var torchOn: String? = null

    fun status(): Pair<String, String> {
        val pm = ctx.getSystemService(BatteryManager::class.java)
        val level = Prefs.battery(ctx)
        val bat = pm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
        val statusCode = pm.getIntProperty(BatteryManager.BATTERY_PROPERTY_STATUS)
        val charge = when (statusCode) {
            BatteryManager.BATTERY_STATUS_CHARGING -> "заряжается"
            BatteryManager.BATTERY_STATUS_FULL -> "заряд полный"
            else -> "на батарее"
        }
        val am = ctx.getSystemService(AudioManager::class.java)
        val vol = am.getStreamVolume(AudioManager.STREAM_MUSIC)
        val max = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
        return "ok" to ("модель ${Build.MODEL}, Android ${Build.VERSION.RELEASE}, " +
            "батарея ${if (bat > 0) "$bat" else "$level"}% ($charge), " +
            "громкость $vol из $max, ${if (torchOn != null) "фонарик включён" else "фонарик выключен"}")
    }

    fun wake(): Pair<String, String> {
        val act = MainActivity.instance?.get()
        if (act != null) act.runOnUiThread {
            runCatching {
                act.window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON or
                        WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON or
                        WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED)
                act.window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            }
        }
        val i = Intent(ctx, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP or
                    Intent.FLAG_ACTIVITY_REORDER_TO_FRONT)
        return try {
            ctx.startActivity(i)
            "ok" to "экран включён, приложение открыто"
        } catch (e: Exception) {
            "error" to "не смог разбудить экран: ${e.message ?: e.javaClass.simpleName}"
        }
    }

    fun torch(on: Boolean): Pair<String, String> {
        return try {
            val cm = ctx.getSystemService(CameraManager::class.java)
            var id = torchOn
            if (id == null || !on) {
                id = cm.cameraIdList.firstOrNull { info ->
                    runCatching { cm.getCameraCharacteristics(info)
                        .get(android.hardware.camera2.CameraCharacteristics.FLASH_INFO_AVAILABLE) == true }
                        .getOrDefault(false)
                }
            }
            if (id == null) return "error" to "на этом телефоне нет фонарика"
            cm.setTorchMode(id, on)
            torchOn = if (on) id else null
            "ok" to if (on) "фонарик включён" else "фонарик выключен"
        } catch (e: Exception) {
            "error" to "фонарик недоступен: ${e.message ?: e.javaClass.simpleName}"
        }
    }

    fun volume(action: String, level: Int?, streamName: String?): Pair<String, String> {
        val am = ctx.getSystemService(AudioManager::class.java)
        val stream = when (streamName?.lowercase()) {
            "alarm" -> AudioManager.STREAM_ALARM
            "ring", "звонок" -> AudioManager.STREAM_RING
            "system", "системный" -> AudioManager.STREAM_SYSTEM
            "voice", "разговор" -> AudioManager.STREAM_VOICE_CALL
            else -> AudioManager.STREAM_MUSIC
        }
        val max = am.getStreamMaxVolume(stream)
        return try {
            when (action.lowercase()) {
                "up", "громче" -> am.adjustStreamVolume(stream, AudioManager.ADJUST_RAISE, 0)
                "down", "тише" -> am.adjustStreamVolume(stream, AudioManager.ADJUST_LOWER, 0)
                "mute", "выключить" -> am.setStreamVolume(stream, 0, 0)
                "set" -> {
                    val v = ((level ?: 50).coerceIn(0, 100)) * max / 100
                    am.setStreamVolume(stream, v, 0)
                }
                else -> return "error" to "не понял действие с громкостью: $action"
            }
            val now = am.getStreamVolume(stream)
            "ok" to "громкость $now из $max"
        } catch (e: Exception) {
            "error" to "не смог изменить громкость: ${e.message ?: e.javaClass.simpleName}"
        }
    }

    fun brightness(level: Int?): Pair<String, String> {
        val act = MainActivity.instance?.get()
            ?: return "error" to "приложение свёрнуто — откройте его, чтобы менять яркость"
        val l = (level ?: 50).coerceIn(0, 100)
        act.runOnUiThread {
            runCatching {
                val attrs = act.window.attributes
                attrs.screenBrightness = l / 100f
                act.window.attributes = attrs
            }
        }
        return "ok" to "яркость экрана $l%"
    }

    /** Русские названия, которыми люди обычно просят открыть приложение. */
    private val appAliases = mapOf(
        "камера" to "camera", "фотоаппарат" to "camera", "камера2" to "camera2",
        "настройки" to "settings", "параметры" to "settings", "телефон" to "phone",
        "звонки" to "dialer", "сообщения" to "messages", "смс" to "messaging",
        "часы" to "clock", "будильник" to "clock", "календарь" to "calendar",
        "контакты" to "contacts", "телефонная книга" to "contacts",
        "галерея" to "photos", "фото" to "photos", "альбом" to "gallery",
        "браузер" to "chrome", "интернет" to "chrome", "почта" to "gmail",
        "карты" to "maps", "диск" to "drive", "файлы" to "files",
        "калькулятор" to "calculator", "заметки" to "notes", "музыка" to "music",
        "ютуб" to "youtube", "play" to "play", "вк" to "vk", "телеграм" to "telegram",
        "вотсап" to "whatsapp", "установки" to "settings", "корзина" to "files")

    /** Подобрать запускаемое приложение по названию: своё, русский синоним или пакет. */
    private fun findApp(name: String): Pair<String, String>? {
        val pm = ctx.packageManager
        val want = appAliases[name.trim().lowercase()] ?: name.trim()
        // список «имя -> пакет» из запускаемых приложений
        val launchable = runCatching {
            pm.queryIntentActivities(
                Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER),
                PackageManager.MATCH_ALL
            ).mapNotNull { ri ->
                val pkg = ri.activityInfo?.packageName ?: return@mapNotNull null
                val ai = ri.activityInfo?.applicationInfo ?: return@mapNotNull null
                val label = runCatching { pm.getApplicationLabel(ai).toString() }.getOrDefault(pkg)
                pkg to label
            }
        }.getOrDefault(emptyList())
        // если ничего не нашлось, берём любые установленные — вдруг это сервис
        val all = launchable.ifEmpty {
            runCatching {
                pm.getInstalledApplications(0).map { it.packageName to it.loadLabel(pm).toString() }
            }.getOrDefault(emptyList())
        }
        val hit = all.firstOrNull { (pkg, label) ->
            label.equals(want, true) || label.contains(want, true) ||
                    pkg.equals(want, true) || pkg.contains(want, true)
        } ?: return null
        return hit.first to hit.second
    }

    fun openApp(name: String): Pair<String, String> {
        if (name.isBlank()) return "error" to "не указано имя приложения"
        val pm = ctx.packageManager
        val (pkg, label) = findApp(name)
            ?: return "error" to "приложение «$name» не найдено"
        return try {
            val launch = ctx.packageManager.getLaunchIntentForPackage(pkg)
                ?: Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
                    .setPackage(pkg).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            ctx.startActivity(launch)
            "ok" to "открыл «$label»"
        } catch (e: Exception) {
            "error" to "не удалось открыть: ${e.message ?: e.javaClass.simpleName}"
        }
    }

    fun apps(filter: String?): Pair<String, String> {
        val pm: PackageManager = ctx.packageManager
        val query = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        val found: List<ResolveInfo> = pm.queryIntentActivities(query, 0)
        val named = found.mapNotNull { ri ->
            val info = ri.activityInfo ?: return@mapNotNull null
            val pkg = info.packageName
            val label = runCatching {
                pm.getApplicationLabel(info.applicationInfo).toString()
            }.getOrDefault(pkg)
            label to pkg
        }.distinctBy { it.second }
        val f = filter?.trim()?.lowercase().orEmpty()
        val list = if (f.isEmpty()) named else named.filter { it.first.lowercase().contains(f) }
        if (list.isEmpty()) {
            return "ok" to if (f.isEmpty()) "приложений не найдено" else "ничего не подходит под «$filter»"
        }
        val shown = list.take(30).joinToString(", ") { it.first }
        val more = if (list.size > 30) " ... ещё ${list.size - 30}" else ""
        return "ok" to "найдено ${list.size}: $shown$more"
    }

    fun clipboard(text: String?, get: Boolean): Pair<String, String> {
        val cm = ctx.getSystemService(ClipboardManager::class.java)
        return try {
            if (get) {
                val cur = cm.primaryClip?.getItemAt(0)?.coerceToText(ctx)?.toString().orEmpty()
                if (cur.isEmpty()) "ok" to "буфер обмена пуст"
                else "ok" to "в буфере: $cur"
            } else {
                if (text.isNullOrBlank()) return "error" to "не передал текст для буфера"
                cm.setPrimaryClip(ClipData.newPlainText("ЛУЧ", text))
                "ok" to "скопировал в буфер: $text"
            }
        } catch (e: Exception) {
            "error" to "Android не дал доступ к буферу: ${e.message ?: e.javaClass.simpleName}"
        }
    }

    fun dial(number: String): Pair<String, String> {
        val n = number.filter { it.isDigit() || it == '+' }
        if (n.isBlank()) return "error" to "не передан номер"
        val i = Intent(Intent.ACTION_DIAL, Uri.parse("tel:$n"))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return try {
            ctx.startActivity(i)
            "ok" to "открыл звонилку с номером $n (звонок не совершён)"
        } catch (e: Exception) {
            "error" to "нечем открыть звонилку"
        }
    }

    fun share(text: String, title: String?): Pair<String, String> {
        if (text.isBlank()) return "error" to "не передан текст"
        val i = Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_TEXT, text)
            if (!title.isNullOrBlank()) putExtra(Intent.EXTRA_SUBJECT, title)
        }
        val chooser = Intent.createChooser(i, title?.ifBlank { "Отправить" } ?: "Отправить")
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return try {
            ctx.startActivity(chooser)
            "ok" to "предложил отправить текст"
        } catch (e: Exception) {
            "error" to "нечем поделиться текстом"
        }
    }

    fun toast(text: String): Pair<String, String> {
        if (text.isBlank()) return "error" to "пустой текст"
        android.os.Handler(ctx.mainLooper).post {
            Toast.makeText(ctx, text, Toast.LENGTH_LONG).show()
        }
        return "ok" to "показал всплывающее сообщение"
    }

    fun openSettings(what: String?): Pair<String, String> {
        val target = when (what?.lowercase()) {
            "wifi", "wi-fi", "сеть" -> Settings.ACTION_WIFI_SETTINGS
            "bluetooth", "блютуз" -> Settings.ACTION_BLUETOOTH_SETTINGS
            "apps", "приложения" -> Settings.ACTION_MANAGE_APPLICATIONS_SETTINGS
            "display", "яркость", "экран" -> Settings.ACTION_DISPLAY_SETTINGS
            "sound", "звук", "громкость" -> Settings.ACTION_SOUND_SETTINGS
            "home", "домой" -> Settings.ACTION_HOME_SETTINGS
            "battery" -> Settings.ACTION_BATTERY_SAVER_SETTINGS
            "storage", "память" -> Settings.ACTION_INTERNAL_STORAGE_SETTINGS
            else -> Settings.ACTION_SETTINGS
        }
        val i = Intent(target).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return try {
            ctx.startActivity(i)
            "ok" to "открыл настройки${if (what.isNullOrBlank()) "" else ": $what"}"
        } catch (e: Exception) {
            "error" to "не смог открыть настройки"
        }
    }

    fun args(j: JSONObject): JSONObject = j

    @Suppress("unused")
    private fun keepActivityApi(act: Activity) = act
}