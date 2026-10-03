package proekt.luch.app

import android.Manifest
import android.annotation.SuppressLint
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.view.View
import android.webkit.GeolocationPermissions
import android.webkit.PermissionRequest
import android.webkit.SslErrorHandler
import android.net.http.SslError
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import org.json.JSONObject

class MainActivity : AppCompatActivity() {

    private lateinit var web: WebView
    private lateinit var prefs: Prefs
    private lateinit var poller: DevicePoller
    private lateinit var uploader: VoiceUploader
    private var pendingAudioPermission: PermissionRequest? = null
    private var pendingAudioGrant: Array<String>? = null
    private var statusView: View? = null
    private var statusWatcher: Runnable? = null

    companion object {
        // нужна командам ИИ: яркость меняется через окно активности
        @JvmStatic var instance = java.lang.ref.WeakReference<MainActivity>(null)

        const val REQ_LOC = 1001
        const val REQ_NOTIF = 1002
        const val REQ_AUDIO = 1003
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        instance = java.lang.ref.WeakReference(this)
        prefs = Prefs(this)
        // Получатель Context валиден только после super.onCreate
        poller = DevicePoller(applicationContext)
        // poller мог найти другой рабочий адрес (https вместо http и т.п.) —
        // тогда сайт надо открыть на нём, иначе страница останется старой.
        // Опрос идёт в фоновом потоке, а любой метод WebView допустим только
        // из главного потока — иначе WebView бросает исключение про потоки.
        poller.onBaseChanged = { runOnUiThread { loadSite() } }

        web = WebView(this)
        // Старая страница из кэша — главная причина «ничего не изменилось»:
        // оставался прежний app.js без новых функций. Кэш отключаем полностью.
        web.settings.cacheMode = android.webkit.WebSettings.LOAD_NO_CACHE
        web.settings.allowFileAccess = false
        runCatching { web.clearCache(true) }

        uploader = VoiceUploader(
            this,
            { prefs.base() },
            { json ->
                // Ответ ИИ показываем в странице, а если страница почему-то старая —
                // озвучиваем и показываем уведомлением, чтобы ответ не потерялся
                runOnUiThread {
                    val ok = runCatching {
                        web.evaluateJavascript(
                            "window.__luchVoiceResult(${JSONObject.quote(json.toString())})", null)
                    }.isSuccess
                    if (!ok) {
                        val txt = json.optString("response").ifBlank {
                            json.optString("user_text").ifBlank { "ответ ИИ" }
                        }
                        uploader.speak(txt)
                        CommandRunner(applicationContext).run(
                            "notify", JSONObject().put("title", "ЛУЧ").put("text", txt))
                    }
                }
            })

        val bridge = LuchBridge(this)
        // Микрофон открыт постоянно, фразы уходят на сервер сами из телефона
        bridge.attachVoice(VoiceListener { b64 -> bridge.uploadPhrase(b64) }, uploader)
        web.addJavascriptInterface(bridge, "LuchAndroid")
        web.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView, url: String) {
                view.visibility = View.VISIBLE
                injectCss(view)
                // страница должна сообщить свою версию, чтобы заметить рассинхрон с APK
                runCatching { view.evaluateJavascript("window.__luchReportVersion && window.__luchReportVersion()", null) }
            }

            // Сервер в домашней сети работает с самоподписанным сертификатом.
            // HTTPS обязателен: микрофон в WebView доступен только в защищённом
            // контексте, а на http://IP адресе navigator.mediaDevices не существует.
            override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
                handler.proceed()
            }
        }
        web.webChromeClient = object : WebChromeClient() {
            override fun onGeolocationPermissionsShowPrompt(origin: String, cb: GeolocationPermissions.Callback?) {
                cb?.invoke(origin, true, false)
            }

            // Без этого WebView молча запрещает доступ к микрофону со страницы,
            // даже когда разрешение RECORD_AUDIO уже выдано системой.
            override fun onPermissionRequest(request: PermissionRequest) {
                val req = request
                val wanted = req.resources.filter { it == PermissionRequest.RESOURCE_AUDIO_CAPTURE }
                if (wanted.isEmpty()) { req.deny(); return }
                if (hasPerm(Manifest.permission.RECORD_AUDIO)) {
                    req.grant(wanted.toTypedArray())
                } else {
                    pendingAudioPermission = req
                    pendingAudioGrant = wanted.toTypedArray()
                    ActivityCompat.requestPermissions(this@MainActivity, arrayOf(Manifest.permission.RECORD_AUDIO), REQ_AUDIO)
                }
            }
        }
        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            loadsImagesAutomatically = true
            useWideViewPort = true
            loadWithOverviewMode = true
            builtInZoomControls = true
            displayZoomControls = false
            allowFileAccess = false
            allowContentAccess = false
            mediaPlaybackRequiresUserGesture = false
            cacheMode = WebSettings.LOAD_DEFAULT
        }
        web.setBackgroundColor(0xFF0B0F14.toInt())
        val root = android.widget.FrameLayout(this)
        root.addView(web, android.widget.FrameLayout.LayoutParams(
            android.widget.FrameLayout.LayoutParams.MATCH_PARENT,
            android.widget.FrameLayout.LayoutParams.MATCH_PARENT))
        addServerButton(root)
        setContentView(root)

        askBatteryOptOut()
        askPermissions()
        poller.start()
        if (prefs.serverUrl.isEmpty()) showSetup() else loadSite()
    }

    /** Кнопка смены сервера: подключиться к любому адресу в любой момент,
     *  не выходя из приложения и не переустанавливая его. */
    private fun addServerButton(root: android.widget.FrameLayout) {
        val pad = (12 * resources.displayMetrics.density).toInt()
        val btn = TextView(this)
        btn.id = View.generateViewId()
        btn.text = "⚙ СЕРВЕР"
        btn.setTextColor(0xFF0B0F14.toInt())
        btn.textSize = 11f
        val padH = (10 * resources.displayMetrics.density).toInt()
        btn.setPadding(padH, padH / 2, padH, padH / 2)
        btn.background = android.graphics.drawable.GradientDrawable().apply {
            cornerRadius = (10 * resources.displayMetrics.density).toFloat()
            setColor(0xE6222C3A.toInt())
            setStroke((1 * resources.displayMetrics.density).toInt(), 0xFF4A5B70.toInt())
        }
        btn.setOnClickListener { showSetup() }
        val lp = android.widget.FrameLayout.LayoutParams(
            android.widget.FrameLayout.LayoutParams.WRAP_CONTENT,
            android.widget.FrameLayout.LayoutParams.WRAP_CONTENT,
            android.view.Gravity.TOP or android.view.Gravity.END)
        lp.setMargins(pad, pad, pad, 0)
        root.addView(btn, lp)
    }

    private fun injectCss(view: WebView) {
        val css = """
            <style>
              html{ -webkit-tap-highlight-color: rgba(0,0,0,0); }
              body{ overscroll-behavior-y: none; }
              *{ -webkit-user-select: text; }
              button, .btn, [role=button]{ touch-action: manipulation; }
            </style>
        """.trimIndent()
        view.evaluateJavascript(
            "try{var s=document.createElement('style');s.innerHTML=${org.json.JSONObject.quote(css)};" +
                "document.head.appendChild(s);}catch(e){}", null)
    }

    private fun loadSite() {
        web.loadUrl(prefs.base() + "/")
    }

    private fun showSetup() {
        val pad = (18 * resources.displayMetrics.density).toInt()
        val box = LinearLayout(this)
        box.orientation = LinearLayout.VERTICAL
        box.setPadding(pad, pad, pad, 0)

        val hint = TextView(this)
        hint.text = "Адрес сервера ЛУЧ в локальной сети, например 192.168.1.163:1337\n" +
            "Можно указать https:…:1338 — тогда и сайт, и микрофон пойдут по защищённому адресу."
        hint.setPadding(0, 0, 0, pad / 2)
        box.addView(hint)

        val url = EditText(this)
        url.hint = "192.168.1.163:1337"
        url.setText(prefs.serverUrl)
        box.addView(url)

        val name = EditText(this)
        name.hint = "Как называть этот телефон"
        name.setText(prefs.deviceName)
        box.addView(name)

        val ver = TextView(this)
        ver.text = "Сборка приложения: ${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})\n" +
            "Текущий адрес: ${prefs.base().ifEmpty { "не задан" }}"
        ver.textSize = 11f
        ver.setPadding(0, 0, 0, pad / 2)
        box.addView(ver)

        AlertDialog.Builder(this)
            .setTitle("Подключение к ЛУЧ")
            .setView(box)
            .setCancelable(false)
            .setPositiveButton("Подключить") { _, _ ->
                val typed = url.text.toString().trim()
                if (typed.isEmpty()) {
                    Toast.makeText(this, "Нуж��н адрес сервера", Toast.LENGTH_LONG).show()
                    showSetup()
                    return@setPositiveButton
                }
                prefs.serverUrl = typed
                prefs.deviceName = name.text.toString().trim()
                prefs.registered = false
                // Перезапускаем опрос: он переберёт варианты (в том числе
                // защищённый) и сам откроет сайт на том адресе, который ответит.
                // Сайт не грузим здесь — загрузится после успешного подключения.
                poller.stop()
                poller.start()
                Toast.makeText(this, "Подключаюсь…", Toast.LENGTH_SHORT).show()
            }
            .setNegativeButton("Позже") { _, _ -> }
            .show()
    }

    private fun askPermissions() {
        val need = mutableListOf<String>()
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
            != PackageManager.PERMISSION_GRANTED)
            need.add(Manifest.permission.ACCESS_FINE_LOCATION)
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_COARSE_LOCATION)
            != PackageManager.PERMISSION_GRANTED)
            need.add(Manifest.permission.ACCESS_COARSE_LOCATION)
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED)
            need.add(Manifest.permission.POST_NOTIFICATIONS)
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED)
            need.add(Manifest.permission.RECORD_AUDIO)

        if (need.isEmpty()) startLocation() else
            ActivityCompat.requestPermissions(this, need.toTypedArray(), REQ_LOC)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grant: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grant)
        if (requestCode == REQ_LOC) startLocation()
        if (requestCode == REQ_AUDIO) {
            val req = pendingAudioPermission
            val res = pendingAudioGrant
            pendingAudioPermission = null
            pendingAudioGrant = null
            if (req != null && res != null) {
                val ok = grant.isNotEmpty() &&
                    grant[0] == PackageManager.PERMISSION_GRANTED
                if (ok) req.grant(res) else req.deny()
            }
        }
    }

    private fun hasPerm(p: String): Boolean =
        ContextCompat.checkSelfPermission(this, p) == PackageManager.PERMISSION_GRANTED

    private fun startLocation() {
        val fine = hasPerm(Manifest.permission.ACCESS_FINE_LOCATION)
        val coarse = hasPerm(Manifest.permission.ACCESS_COARSE_LOCATION)
        if (!fine && !coarse) {
            Toast.makeText(this, "Без геолокации ИИ не узнает, где ты", Toast.LENGTH_LONG).show()
            return
        }
        try {
            LocationService.start(this)
        } catch (e: Exception) {
            Toast.makeText(this, "Службу геолокации не удалось запустить", Toast.LENGTH_LONG).show()
        }
    }

    private fun askBatteryOptOut() {
        val pm = getSystemService(PowerManager::class.java)
        if (pm.isIgnoringBatteryOptimizations(packageName)) return
        // Спрашиваем один раз: каждый запуск этого диалога раздражает.
        if (prefs.batteryAsked) return
        prefs.batteryAsked = true
        AlertDialog.Builder(this)
            .setTitle("ЛУЧ должен работать в фоне")
            .setMessage("Android может отключить ЛУЧ, чтобы экономить батарею, и тогда ИИ не сможет " +
                "присылать команды. Разрешить ЛУЧ работать в фоне?")
            .setPositiveButton("Разрешить") { _, _ ->
                runCatching {
                    startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                        Uri.parse("package:$packageName")))
                }
            }
            .setNegativeButton("Позже") { _, _ -> }
            .show()
    }

    override fun onResume() {
        super.onResume()
        poller.start()
        watchStatus()
    }

    /** Показывает баннер со статусом связи с сервером, если что-то пошло не так. */
    private fun watchStatus() {
        if (statusWatcher != null) return
        val runner = object : Runnable {
            override fun run() {
                val err = poller.lastError
                // Микрофон записывается нативно, поэтому http ни при чём —
                // предупреждения про HTTPS больше не нужны.
                val shown = err
                if (shown.isNotEmpty() && !poller.online) {
                    if (statusView == null) statusView = buildStatus(shown)
                    statusView?.visibility = View.VISIBLE
                    statusView?.findViewById<TextView>(android.R.id.text1)?.text = shown
                } else {
                    statusView?.visibility = View.GONE
                }
                android.os.Handler(mainLooper).postDelayed(this, 2000)
            }
        }
        statusWatcher = runner
        android.os.Handler(mainLooper).post(runner)
    }

    private fun buildStatus(text: String): View {
        val tv = android.widget.TextView(this)
        tv.id = android.R.id.text1
        tv.text = text
        tv.textSize = 13f
        tv.setTextColor(0xFF111111.toInt())
        tv.setBackgroundColor(0xFFFFC107.toInt())
        tv.setPadding(24, 18, 24, 18)
        tv.setOnClickListener { showSetup() }
        (web.parent as? android.widget.FrameLayout)?.addView(
            tv, android.widget.FrameLayout.LayoutParams(
                android.widget.FrameLayout.LayoutParams.MATCH_PARENT,
                android.widget.FrameLayout.LayoutParams.WRAP_CONTENT
            ).apply { gravity = android.view.Gravity.TOP })
        return tv
    }

    override fun onDestroy() {
        statusWatcher?.let { android.os.Handler(mainLooper).removeCallbacks(it) }
        poller.stop()
        LocationService.stop(this)
        web.destroy()
        super.onDestroy()
    }

    @Deprecated("для старых Android")
    override fun onBackPressed() {
        if (web.canGoBack()) {
            web.goBack()
        } else {
            super.onBackPressed()
        }
    }
}
