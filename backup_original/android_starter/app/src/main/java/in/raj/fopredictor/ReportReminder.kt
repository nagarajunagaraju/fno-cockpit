package in.raj.fopredictor

import android.app.AlarmManager
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import java.util.Calendar

object ReportReminder {
    private const val CHANNEL_ID = "daily_report"
    private const val REQUEST_CODE = 928

    fun schedule(context: Context) {
        val alarm = context.getSystemService(Context.ALARM_SERVICE) as AlarmManager
        val intent = Intent(context, ReportReminderReceiver::class.java)
        val pending = PendingIntent.getBroadcast(context, REQUEST_CODE, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val next = Calendar.getInstance().apply {
            set(Calendar.HOUR_OF_DAY, 9); set(Calendar.MINUTE, 28); set(Calendar.SECOND, 0); set(Calendar.MILLISECOND, 0)
            if (timeInMillis <= System.currentTimeMillis()) add(Calendar.DAY_OF_YEAR, 1)
        }
        alarm.setInexactRepeating(AlarmManager.RTC_WAKEUP, next.timeInMillis, AlarmManager.INTERVAL_DAY, pending)
    }

    fun show(context: Context) {
        val today = Calendar.getInstance().get(Calendar.DAY_OF_WEEK)
        if (today == Calendar.SATURDAY || today == Calendar.SUNDAY) return
        val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= 26) manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Daily market report", NotificationManager.IMPORTANCE_DEFAULT))
        val open = PendingIntent.getActivity(context, 929, Intent(context, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle("F&O Predictor · 9:28 AM")
            .setContentText("Refresh today's research outlook. Verify live market data before trading.")
            .setContentIntent(open).setAutoCancel(true).build()
        manager.notify(928, notification)
    }
}

class ReportReminderReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) = ReportReminder.show(context)
}
