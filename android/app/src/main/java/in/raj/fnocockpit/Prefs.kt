package `in`.raj.fnocockpit

import android.content.Context

/** App-private settings. The app token never leaves this device except to your own server. */
class Prefs(ctx: Context) {
    private val p = ctx.getSharedPreferences("cockpit", Context.MODE_PRIVATE)

    var serverUrl: String
        get() = p.getString("server", "") ?: ""
        set(v) = p.edit().putString("server", v.trim().trimEnd('/')).apply()
    var token: String
        get() = p.getString("token", "") ?: ""
        set(v) = p.edit().putString("token", v.trim()).apply()
    var lastEventId: Long
        get() = p.getLong("last_event", -1L)
        set(v) = p.edit().putLong("last_event", v).apply()
    var alertsEnabled: Boolean
        get() = p.getBoolean("alerts", true)
        set(v) = p.edit().putBoolean("alerts", v).apply()
    var serviceStatus: String
        get() = p.getString("svc_status", "not started") ?: ""
        set(v) = p.edit().putString("svc_status", v).apply()

    val configured: Boolean get() = serverUrl.startsWith("http") && token.isNotBlank()
}
