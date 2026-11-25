package `in`.raj.fnocockpit

import android.app.AlarmManager
import android.app.PendingIntent
import android.app.Service
import android.os.PowerManager
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
 * Every 10 s during 09:00–15:40 IST, every 5 min 18:45–21:45 (evening watchlist), asleep otherwise. No Firebase needed.
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
        } else if (intent?.action == ACTION_WAKE) {
            worker?.interrupt()     // alarm fired: stop sleeping, check the window now
        }
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        worker?.interrupt()
        releaseWake()
        super.onDestroy()
    }

    // ---- staying awake: phone deep sleep pauses Thread.sleep, so
    // (1) during market hours hold a partial wake lock (CPU only, screen stays off),
    // (2) for long sleeps set an AlarmManager alarm that wakes the phone and restarts polling.
    private var wake: PowerManager.WakeLock? = null

    private fun holdWake(ms: Long) {
        if (wake?.isHeld == true) return
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "fnocockpit:market").apply {
            setReferenceCounted(false); acquire(ms)
        }
    }

    private fun releaseWake() {
        try { if (wake?.isHeld == true) wake?.release() } catch (_: Exception) { }
        wake = null
    }

    private fun scheduleWake(ms: Long) {
        val am = getSystemService(Context.ALARM_SERVICE) as AlarmManager
        val pi = PendingIntent.getForegroundService(this, 7, Intent(this, AlertService::class.java).setAction(ACTION_WAKE),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val at = System.currentTimeMillis() + ms
        if (Build.VERSION.SDK_INT < 31 || am.canScheduleExactAlarms()) {
            am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pi)
        } else {
            am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pi)   // inexact: may be a few minutes late
        }
    }

    /** Sleeps by the wall clock in <=60 s steps (Thread.sleep alone stops counting while the phone sleeps). */
    private fun sleepUntil(wallMs: Long) {
        while (running) {
            val left = wallMs - System.currentTimeMillis()
            if (left <= 0) return
            try { Thread.sleep(minOf(left, 60_000L)) } catch (_: InterruptedException) { return }
        }
    }

    private fun marketWindow(): Boolean {
        val now = ZonedDateTime.now(ZoneId.of("Asia/Kolkata"))
        if (now.dayOfWeek == DayOfWeek.SATURDAY || now.dayOfWeek == DayOfWeek.SUNDAY) return false
        val t = now.toLocalTime()
        return t.isAfter(LocalTime.of(9, 0)) && t.isBefore(LocalTime.of(15, 40))
    }

    /** 18:45–21:45 IST weekdays: the evening FII watchlist arrives here (polled every 5 min). */
    private fun eveningWindow(): Boolean {
        val now = ZonedDateTime.now(ZoneId.of("Asia/Kolkata"))
        if (now.dayOfWeek == DayOfWeek.SATURDAY || now.dayOfWeek == DayOfWeek.SUNDAY) return false
        val t = now.toLocalTime()
        return t.isAfter(LocalTime.of(18, 45)) && t.isBefore(LocalTime.of(21, 45))
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
            if (inWindow) holdWake(7 * 60 * 60 * 1000L) else releaseWake()
            val sleepMs = when {
                failures > 0 && inWindow -> minOf(60_000L, 10_000L * failures)
                inWindow -> 10_000L
                eveningWindow() -> 300_000L
                else -> {   // closed: sleep until the next window (09:00 market or 18:45 watchlist), no polling
                    val (ms, label) = msUntilNextWindow()
                    updateStatus(prefs, "Sleeping until $label")
                    ms
                }
            }
            if (sleepMs > 120_000L) scheduleWake(sleepMs)
            sleepUntil(System.currentTimeMillis() + sleepMs)
        }
    }

    private fun nextAt(now: ZonedDateTime, h: Int, m: Int): ZonedDateTime {
        var next = now.withHour(h).withMinute(m).withSecond(5).withNano(0)
        if (!now.isBefore(next)) next = next.plusDays(1)
        while (next.dayOfWeek == DayOfWeek.SATURDAY || next.dayOfWeek == DayOfWeek.SUNDAY) next = next.plusDays(1)
        return next
    }

    private fun msUntilNextWindow(): Pair<Long, String> {
        val now = ZonedDateTime.now(ZoneId.of("Asia/Kolkata"))
        val open = nextAt(now, 9, 0)
        val eve = nextAt(now, 18, 46)
        val (next, label) = if (eve.isBefore(open)) eve to "evening watchlist 18:45" else open to "next market day 09:00"
        return java.time.Duration.between(now, next).toMillis().coerceAtLeast(60_000L) to label
    }

    private fun updateStatus(prefs: Prefs, s: String) {
        prefs.serviceStatus = s
        try {
            androidx.core.app.NotificationManagerCompat.from(this).notify(Notif.SERVICE_ID, Notif.service(this, s))
        } catch (_: SecurityException) { }
    }

    companion object {
        const val ACTION_WAKE = "in.raj.fnocockpit.WAKE"

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
