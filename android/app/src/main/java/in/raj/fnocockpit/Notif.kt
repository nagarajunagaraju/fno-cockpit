package `in`.raj.fnocockpit

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.graphics.Color
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import org.json.JSONObject

object Notif {
    const val CH_SIGNAL = "signals_v2"
    const val CH_UPDATE = "updates_v2"
    const val CH_SERVICE = "service_v2"
    const val SERVICE_ID = 1

    fun channels(ctx: Context) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CH_SIGNAL, "New trade signals", NotificationManager.IMPORTANCE_HIGH).apply {
            description = "A setup passed every filter and the entry trigger fired"
            enableVibration(true); enableLights(true); lightColor = Color.GREEN
        })
        nm.createNotificationChannel(NotificationChannel(CH_UPDATE, "Signal updates", NotificationManager.IMPORTANCE_DEFAULT).apply {
            description = "Paper fill, targets, stop-loss, invalidation, risk lock"
        })
        nm.createNotificationChannel(NotificationChannel(CH_SERVICE, "Background watcher", NotificationManager.IMPORTANCE_MIN).apply {
            description = "Keeps the signal watcher running"
        })
    }

    fun openIntent(ctx: Context, signalId: Long): PendingIntent {
        val i = Intent(ctx, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
            putExtra("signal_id", signalId)
        }
        return PendingIntent.getActivity(ctx, signalId.toInt(), i, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
    }

    fun service(ctx: Context, text: String) = NotificationCompat.Builder(ctx, CH_SERVICE)
        .setSmallIcon(R.drawable.ic_stat_signal)
        .setContentTitle("F&O Cockpit watching")
        .setContentText(text)
        .setOngoing(true)
        .setContentIntent(openIntent(ctx, 0))
        .build()

    fun event(ctx: Context, e: JSONObject) {
        val kind = e.optString("kind")
        val sid = e.optLong("signal_id", 0)
        val title = e.optString("title")
        var body = e.optString("body")
        if (kind == "TRIGGERED") {
            val p = e.optJSONObject("payload")?.optJSONObject("plan")
            if (p != null) {
                body = "${p.optString("tradingsymbol")} · Exp ${p.optString("expiry")}\n" +
                    "Entry ${p.optJSONArray("entry_range")?.optDouble(0)}–${p.optJSONArray("entry_range")?.optDouble(1)}" +
                    " | SL ${p.optDouble("sl_premium")} | T1 ${p.optDouble("t1_premium")} | T2 ${p.optDouble("t2_premium")}\n" +
                    "${p.optInt("lots")} lot(s) · max loss ₹${p.optDouble("max_loss").toInt()} · R:R ${p.optDouble("rr_t1")}\n" +
                    "Trigger: ${p.optString("entry_trigger")}"
            }
        }
        val ch = if (kind == "TRIGGERED") CH_SIGNAL else CH_UPDATE
        val n = NotificationCompat.Builder(ctx, ch)
            .setSmallIcon(R.drawable.ic_stat_signal)
            .setContentTitle(title)
            .setContentText(body.lineSequence().firstOrNull() ?: body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setPriority(if (kind == "TRIGGERED") NotificationCompat.PRIORITY_HIGH else NotificationCompat.PRIORITY_DEFAULT)
            .setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .setAutoCancel(true)
            .setContentIntent(openIntent(ctx, sid))
            .setColor(
                when (kind) {
                    "TRIGGERED", "TARGET_HIT", "ACTIVE" -> Color.parseColor("#22C55E")
                    "STOP_LOSS_HIT", "INVALIDATED", "RISK_LOCK" -> Color.parseColor("#EF4444")
                    else -> Color.parseColor("#60A5FA")
                }
            )
            .build()
        try {
            NotificationManagerCompat.from(ctx).notify((100000 + e.optLong("id") % 100000).toInt(), n)
        } catch (_: SecurityException) {
            // notification permission not granted
        }
    }
}
