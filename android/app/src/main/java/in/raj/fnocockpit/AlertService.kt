package `in`.raj.fnocockpit

import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import java.time.DayOfWeek
import java.time.LocalTime
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import kotlin.concurrent.thread

/**
 * Foreground service that polls YOUR server for new signal events and shows them as notifications.
 * Every 10 s during 09:00–15:40 IST on weekdays, every 5 min otherwise. No Firebase needed.
 */
class AlertService : Service() {
    @Volatile private var running = false
    private var worker: Thread? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Notif.channels(this)
        val n = Notif.service(this, "Starting…")
        if (Build.VERSION.SDK_INT >= 34) {
            ServiceCompat.startForeground(this, Notif.SERVICE_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(Notif.SERVICE_ID, n)
        }
        if (!running) {
            running = true
            worker = thread(name = "cockpit-alerts", isDaemon = true) { loop() }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        worker?.interrupt()
        super.onDestroy()
    }

    private fun marketWindow(): Boolean {
        val now = ZonedDateTime.now(ZoneId.of("Asia/Kolkata"))
        if (now.dayOfWeek == DayOfWeek.SATURDAY || now.dayOfWeek == DayOfWeek.SUNDAY) return false
        val t = now.toLocalTime()
        return t.isAfter(LocalTime.of(9, 0)) && t.isBefore(LocalTime.of(15, 40))
    }

    private fun loop() {
        val prefs = Prefs(this)
        val fmt = DateTimeFormatter.ofPattern("HH:mm:ss")
        var failures = 0
        while (running) {
            val inWindow = marketWindow()
            try {
                if (!prefs.configured || !prefs.alertsEnabled) {
                    updateStatus(prefs, "Not configured / alerts off")
                } else {
                    val api = Api(prefs.serverUrl, prefs.token)
                    if (prefs.lastEventId < 0) {
                        // first run: start from the newest event, don't replay history
                        prefs.lastEventId = api.lastEventIdBlocking()
                    }
                    var after = prefs.lastEventId
                    while (true) {
                        val evs = api.eventsBlocking(after)
                        for (i in 0 until evs.length()) {
                            val e = evs.getJSONObject(i)
                            Notif.event(this, e)
                            after = e.optLong("id")
                            prefs.lastEventId = after
                        }
                        if (evs.length() < 50) break
                    }
                    failures = 0
                    updateStatus(prefs, (if (inWindow) "Market hours · checked " else "Off-hours · checked ") +
                        ZonedDateTime.now(ZoneId.of("Asia/Kolkata")).format(fmt))
                }
            } catch (e: Exception) {
                failures++
                updateStatus(prefs, "Server not reachable (${e.message?.take(60)}) · retry")
            }
            val sleepMs = when {
                failures > 0 && inWindow -> minOf(60_000L, 10_000L * failures)
                inWindow -> 10_000L
                else -> {   // market closed: sleep until next weekday 09:00 IST (no polling, no battery use)
                    val untilOpen = msUntilNextOpen()
                    updateStatus(prefs, "Sleeping until next market day 09:00")
                    untilOpen
                }
            }
            try { Thread.sleep(sleepMs) } catch (_: InterruptedException) { }
        }
    }

    private fun msUntilNextOpen(): Long {
        val zone = ZoneId.of("Asia/Kolkata")
        val now = ZonedDateTime.now(zone)
        var next = now.withHour(9).withMinute(0).withSecond(5).withNano(0)
        if (!now.isBefore(next)) next = next.plusDays(1)
        while (next.dayOfWeek == DayOfWeek.SATURDAY || next.dayOfWeek == DayOfWeek.SUNDAY) next = next.plusDays(1)
        return java.time.Duration.between(now, next).toMillis().coerceAtLeast(60_000L)
    }

    private fun updateStatus(prefs: Prefs, s: String) {
        prefs.serviceStatus = s
        try {
            androidx.core.app.NotificationManagerCompat.from(this).notify(Notif.SERVICE_ID, Notif.service(this, s))
        } catch (_: SecurityException) { }
    }

    companion object {
        fun start(ctx: Context) {
            val p = Prefs(ctx)
            if (!p.configured || !p.alertsEnabled) return
            try {
                ContextCompat.startForegroundService(ctx, Intent(ctx, AlertService::class.java))
            } catch (_: Exception) { }
        }

        fun stop(ctx: Context) {
            ctx.stopService(Intent(ctx, AlertService::class.java))
        }
    }
}
