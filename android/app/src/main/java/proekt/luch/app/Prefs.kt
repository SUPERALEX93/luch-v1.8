package proekt.luch.app

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.os.Build

class Prefs(context: Context) {

    private val sp = context.getSharedPreferences("luch", Context.MODE_PRIVATE)

    var serverUrl: String
        get() = sp.getString("server", "") ?: ""
        set(v) = sp.edit().putString("server", normalize(v)).apply()

    var clientId: String
        get() = sp.getString("client_id", "") ?: ""
        set(v) = sp.edit().putString("client_id", v).apply()

    var token: String
        get() = sp.getString("token", "") ?: ""
        set(v) = sp.edit().putString("token", v).apply()

    var deviceName: String
        get() = sp.getString("name", "") ?: ""
        set(v) = sp.edit().putString("name", v).apply()

    var registered: Boolean
        get() = sp.getBoolean("registered", false)
        set(v) = sp.edit().putBoolean("registered", v).apply()

    /** Версия приложения, под которой телефон последний раз регистрировался.
     *  После обновления список возможностей меняется, и сервер должен узнать
     *  о новом составе — иначе телефон годами не получит новые команды. */
    var registeredVersion: String
        get() = sp.getString("registered_version", "") ?: ""
        set(v) = sp.edit().putString("registered_version", v).apply()

    var lastSeq: Int
        get() = sp.getInt("seq", 0)
        set(v) = sp.edit().putInt("seq", v).apply()

    var batteryAsked: Boolean
        get() = sp.getBoolean("battery_asked", false)
        set(v) = sp.edit().putBoolean("battery_asked", v).apply()

    val caps: List<String> = listOf(
        "location", "notify", "vibrate", "ring", "open_url", "battery",
        "status", "wake", "torch", "volume", "brightness", "open_app",
        "apps", "clipboard", "dial", "share", "toast", "open_settings",
        "stop_ring")

    fun clear() = sp.edit().clear().apply()

    fun base(): String = serverUrl.trimEnd('/')

    private fun normalize(v: String): String {
        var s = (v ?: "").trim()
        if (s.isEmpty()) return ""
        if (!s.startsWith("http://") && !s.startsWith("https://")) s = "http://$s"
        return s.trimEnd('/')
    }

    fun model(): String = "${Build.MANUFACTURER} ${Build.MODEL}".trim()

    fun os(): String = "Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})"

    companion object {
        fun battery(context: Context): Int {
            val intent: Intent? = context.registerReceiver(
                null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
            val level = intent?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
            val scale = intent?.getIntExtra(BatteryManager.EXTRA_SCALE, -1) ?: -1
            return if (level >= 0 && scale > 0) level * 100 / scale else -1
        }
    }
}
