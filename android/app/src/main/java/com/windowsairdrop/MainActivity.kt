package com.windowsairdrop

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.ListView
import android.widget.TextView
import android.widget.Toast
import kotlin.concurrent.thread

class MainActivity : Activity() {
    private val ui = Handler(Looper.getMainLooper())
    private var peers = listOf<Peer>()
    private var pending: List<Uri> = emptyList()   // files shared into the app via the share sheet
    private var target: Peer? = null
    private lateinit var status: TextView
    private lateinit var adapter: ArrayAdapter<String>

    override fun onCreate(b: Bundle?) {
        super.onCreate(b)
        setContentView(R.layout.activity_main)
        if (Build.VERSION.SDK_INT >= 33) requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        AirDropService.start(this)

        val prefs = getSharedPreferences("prefs", MODE_PRIVATE)
        val name = prefs.getString("name", Build.MODEL) ?: Build.MODEL
        findViewById<TextView>(R.id.me).text = "This phone: $name"
        status = findViewById(R.id.status)
        val pin = findViewById<EditText>(R.id.pin).apply { setText(prefs.getString("pin", "")) }
        findViewById<Button>(R.id.savePin).setOnClickListener {
            val p = pin.text.toString().trim()
            prefs.edit().putString("pin", p).apply()
            AirDropService.node?.pin = p
            Toast.makeText(this, if (p.isEmpty()) "PIN cleared" else "PIN saved", Toast.LENGTH_SHORT).show()
        }

        adapter = ArrayAdapter(this, android.R.layout.simple_list_item_1, mutableListOf())
        findViewById<ListView>(R.id.list).apply {
            this.adapter = this@MainActivity.adapter
            setOnItemClickListener { _, _, pos, _ -> choose(peers[pos]) }
        }
        handleShare(intent)
        tick()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleShare(intent)
    }

    @Suppress("DEPRECATION")
    private fun handleShare(i: Intent) {
        pending = when (i.action) {
            Intent.ACTION_SEND -> listOfNotNull(i.getParcelableExtra<Uri>(Intent.EXTRA_STREAM))
            Intent.ACTION_SEND_MULTIPLE -> i.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM) ?: emptyList()
            else -> pending
        }
        if (pending.isNotEmpty()) status.text = "${pending.size} file(s) ready - tap a PC to send"
    }

    private fun tick() {
        peers = AirDropService.node?.peers() ?: emptyList()
        adapter.clear()
        adapter.addAll(peers.map { "${it.name}  (${it.ip})${if (it.pin) "  🔒" else ""}" })
        if (pending.isEmpty() && !sending) status.text = AirDropService.lastEvent
        ui.postDelayed({ tick() }, 2000)
    }

    private fun choose(p: Peer) {
        target = p
        if (pending.isNotEmpty()) {
            send(p, pending); pending = emptyList()
        } else {
            startActivityForResult(Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
                addCategory(Intent.CATEGORY_OPENABLE); type = "*/*"
                putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
            }, 7)
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(req: Int, res: Int, data: Intent?) {
        super.onActivityResult(req, res, data)
        if (req != 7 || res != RESULT_OK || data == null) return
        val uris = mutableListOf<Uri>()
        data.clipData?.let { c -> for (i in 0 until c.itemCount) uris += c.getItemAt(i).uri }
        data.data?.let { if (uris.isEmpty()) uris += it }
        target?.let { send(it, uris) }
    }

    @Volatile private var sending = false

    private fun send(p: Peer, uris: List<Uri>) {
        val pin = getSharedPreferences("prefs", MODE_PRIVATE).getString("pin", "") ?: ""
        val node = AirDropService.node ?: return
        sending = true
        thread {
            try {
                uris.forEachIndexed { i, u ->
                    node.send(p, u, pin) { s, t ->
                        ui.post { status.text = "Sending ${i + 1}/${uris.size} to ${p.name}: ${100 * s / maxOf(t, 1)}%" }
                    }
                }
                ui.post { status.text = "Sent ${uris.size} file(s) to ${p.name}" }
            } catch (e: Exception) {
                ui.post { status.text = "Send failed: ${e.message}" }
            } finally {
                sending = false
            }
        }
    }
}
