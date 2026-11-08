package com.windowsairdrop

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder

/** Foreground service: keeps the receiver + discovery alive while the app is in the background. */
class AirDropService : Service() {
    companion object {
        @Volatile var node: Node? = null
        @Volatile var lastEvent: String = "Listening for incoming files..."
        private const val CH = "airdrop"

        fun start(ctx: Context) {
            ctx.startForegroundService(Intent(ctx, AirDropService::class.java))
        }
    }

    private var lock: WifiManager.MulticastLock? = null

    override fun onBind(i: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CH, "AirDrop receiver", NotificationManager.IMPORTANCE_LOW))
        startForeground(1, ongoing("Ready to receive files from your PC"))

        if (node == null) {
            val prefs = getSharedPreferences("prefs", MODE_PRIVATE)
            val wifi = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            lock = wifi.createMulticastLock("airdrop").apply { setReferenceCounted(false); acquire() }
            node = Node(applicationContext, prefs.getString("name", Build.MODEL) ?: Build.MODEL,
                prefs.getString("pin", "") ?: "") { file, sender ->
                lastEvent = "Received $file from $sender"
                val n = Notification.Builder(this, CH).setSmallIcon(android.R.drawable.stat_sys_download_done)
                    .setContentTitle("File received").setContentText("$file from $sender")
                    .setContentIntent(openApp()).setAutoCancel(true).build()
                nm.notify(System.currentTimeMillis().toInt(), n)
            }.also { it.start() }
        }
        return START_STICKY
    }

    private fun openApp() = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
        PendingIntent.FLAG_IMMUTABLE)

    private fun ongoing(text: String) = Notification.Builder(this, CH)
        .setSmallIcon(android.R.drawable.stat_sys_upload).setContentTitle("Windows AirDrop")
        .setContentText(text).setContentIntent(openApp()).setOngoing(true).build()

    override fun onDestroy() {
        node?.stop(); node = null
        lock?.release()
        super.onDestroy()
    }
}
