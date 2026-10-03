package proekt.luch.app

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Build
import android.os.Bundle
import android.os.IBinder
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import org.json.JSONObject

class LocationService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private lateinit var prefs: Prefs
    private var manager: LocationManager? = null
    private var lastSent = 0L

    private val listener = object : LocationListener {
        override fun onLocationChanged(loc: Location) = send(loc)
        override fun onProviderEnabled(p: String) {}
        override fun onProviderDisabled(p: String) {}
        @Deprecated("deprecated in API 29")
        override fun onStatusChanged(p: String?, s: Int, e: Bundle?) {}
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        prefs = Prefs(this)
        startForegroundCompat()
        startUpdates()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_ONCE) sendLastKnown()
        return START_STICKY
    }

    override fun onDestroy() {
        runCatching { manager?.removeUpdates(listener) }
        super.onDestroy()
    }

    private fun startForegroundCompat() {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val notif: Notification = Notification.Builder(this, App.CHANNEL_ID)
            .setContentTitle(getString(R.string.notif_device_name))
            .setContentText(getString(R.string.notif_service_text))
            .setSmallIcon(android.R.drawable.ic_menu_mylocation)
            .setContentIntent(open)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE)
            startForeground(1, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION)
        else startForeground(1, notif)
    }

    private fun startUpdates() {
        val lm = getSystemService(Context.LOCATION_SERVICE) as LocationManager
        manager = lm
        val providers = listOf(
            LocationManager.GPS_PROVIDER,
            LocationManager.NETWORK_PROVIDER,
            LocationManager.PASSIVE_PROVIDER)
        for (p in providers) {
            if (!runCatching { lm.isProviderEnabled(p) }.getOrDefault(false)) continue
            runCatching {
                lm.requestLocationUpdates(p, 20_000L, 20f, listener, android.os.Looper.getMainLooper())
            }
        }
        sendLastKnown()
    }

    private fun sendLastKnown() {
        val lm = manager ?: return
        val best: Location? = runCatching {
            listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER,
                LocationManager.PASSIVE_PROVIDER)
                .mapNotNull { runCatching { lm.getLastKnownLocation(it) }.getOrNull() }
                .maxByOrNull { it.time }
        }.getOrNull()
        best?.let { send(it) }
    }

    private fun send(loc: Location) {
        val now = System.currentTimeMillis()
        if (now - lastSent < 15_000L) return
        lastSent = now
        scope.launch {
            if (prefs.serverUrl.isEmpty()) return@launch
            Net.post(
                prefs.base(), "/api/device/location",
                JSONObject()
                    .put("lat", loc.latitude)
                    .put("lng", loc.longitude)
                    .put("accuracy", if (loc.hasAccuracy()) loc.accuracy.toDouble() else JSONObject.NULL),
                mapOf("client_id" to prefs.clientId, "token" to prefs.token))
        }
    }

    companion object {
        const val ACTION_ONCE = "proekt.luch.app.ONCE"

        fun start(ctx: Context) {
            ctx.startForegroundService(Intent(ctx, LocationService::class.java))
        }

        fun stop(ctx: Context) {
            ctx.stopService(Intent(ctx, LocationService::class.java))
        }
    }
}
