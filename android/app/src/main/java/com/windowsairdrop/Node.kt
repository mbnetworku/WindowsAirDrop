package com.windowsairdrop

import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.Environment
import android.provider.MediaStore
import android.provider.OpenableColumns
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.InputStream
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.HttpURLConnection
import java.net.InetAddress
import java.net.NetworkInterface
import java.net.ServerSocket
import java.net.Socket
import java.net.URL
import java.net.URLDecoder
import java.net.URLEncoder
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import kotlin.concurrent.thread

const val HTTP_PORT = 53317
const val DISC_PORT = 53318
private const val PEER_TTL_MS = 12_000L

data class Peer(val id: String, val name: String, val ip: String, val port: Int,
                val os: String, val pin: Boolean, val seen: Long)

/** Same protocol as windows/airdrop_core.py - see PROTOCOL.md. */
class Node(private val ctx: Context, @Volatile var name: String, @Volatile var pin: String,
           private val onReceive: (fileName: String, sender: String) -> Unit) {

    private val id = UUID.randomUUID().toString().replace("-", "").take(12)
    private val peers = ConcurrentHashMap<String, Peer>()
    @Volatile private var running = false
    private var server: ServerSocket? = null
    private var listener: DatagramSocket? = null

    fun start() {
        running = true
        thread(isDaemon = true) { runServer() }
        thread(isDaemon = true) { runBeacon() }
        thread(isDaemon = true) { runListener() }
    }

    fun stop() {
        running = false
        runCatching { server?.close() }
        runCatching { listener?.close() }
    }

    fun peers(): List<Peer> {
        val now = System.currentTimeMillis()
        peers.values.removeIf { now - it.seen > PEER_TTL_MS }
        return peers.values.sortedBy { it.name.lowercase() }
    }

    // ---------- discovery ----------
    private fun beacon() = JSONObject().put("app", "wad").put("id", id).put("name", name)
        .put("port", HTTP_PORT).put("os", "android").put("pin", pin.isNotEmpty())
        .toString().toByteArray()

    private fun broadcastAddresses(): Set<InetAddress> {
        val out = mutableSetOf<InetAddress>(InetAddress.getByName("255.255.255.255"))
        runCatching {
            for (ni in NetworkInterface.getNetworkInterfaces()) {
                if (!ni.isUp || ni.isLoopback) continue
                ni.interfaceAddresses.mapNotNullTo(out) { it.broadcast }
            }
        }
        return out
    }

    private fun runBeacon() {
        val s = DatagramSocket().apply { broadcast = true }
        while (running) {
            val data = beacon()
            for (addr in broadcastAddresses()) {
                runCatching { s.send(DatagramPacket(data, data.size, addr, DISC_PORT)) }
            }
            Thread.sleep(3000)
        }
        s.close()
    }

    private fun runListener() {
        val s = DatagramSocket(null).apply { reuseAddress = true; bind(java.net.InetSocketAddress(DISC_PORT)) }
        listener = s
        val buf = ByteArray(2048)
        while (running) {
            try {
                val p = DatagramPacket(buf, buf.size)
                s.receive(p)
                val j = JSONObject(String(p.data, 0, p.length))
                if (j.optString("app") != "wad" || j.optString("id") == id) continue
                val pid = j.getString("id")
                peers[pid] = Peer(pid, j.optString("name", "?"), p.address.hostAddress ?: continue,
                    j.optInt("port", HTTP_PORT), j.optString("os", "?"),
                    j.optBoolean("pin"), System.currentTimeMillis())
            } catch (e: Exception) {
                if (!running) break
            }
        }
    }

    // ---------- receiving ----------
    private fun runServer() {
        val ss = ServerSocket(HTTP_PORT)
        server = ss
        while (running) {
            val c = try { ss.accept() } catch (e: Exception) { break }
            thread(isDaemon = true) { runCatching { handle(c) }; runCatching { c.close() } }
        }
    }

    private fun readLine(i: InputStream): String? {
        val sb = StringBuilder()
        while (true) {
            val b = i.read()
            if (b < 0) return if (sb.isEmpty()) null else sb.toString()
            if (b == '\n'.code) return sb.toString().trimEnd('\r')
            sb.append(b.toChar())
        }
    }

    private fun reply(c: Socket, code: Int, text: String) {
        val body = text.toByteArray()
        val head = "HTTP/1.1 $code ${if (code == 200) "OK" else "Error"}\r\n" +
            "Content-Length: ${body.size}\r\nConnection: close\r\n\r\n"
        c.getOutputStream().apply { write(head.toByteArray()); write(body); flush() }
    }

    private fun handle(c: Socket) {
        c.soTimeout = 30_000
        val input = BufferedInputStream(c.getInputStream())
        val reqLine = readLine(input) ?: return
        val parts = reqLine.split(" ")
        val headers = mutableMapOf<String, String>()
        while (true) {
            val l = readLine(input) ?: return
            if (l.isEmpty()) break
            l.indexOf(':').takeIf { it > 0 }?.let { headers[l.substring(0, it).trim().lowercase()] = l.substring(it + 1).trim() }
        }
        if (parts.size < 2 || parts[0] != "PUT" || !parts[1].startsWith("/send")) return reply(c, 404, "not found")
        if (pin.isNotEmpty() && headers["x-pin"] != pin) return reply(c, 403, "bad pin")
        val length = headers["content-length"]?.toLongOrNull() ?: return reply(c, 411, "length required")
        val rawName = parts[1].substringAfter("name=", "file").substringBefore('&')
        val name = sanitize(URLDecoder.decode(rawName, "UTF-8"))
        val sender = URLDecoder.decode(headers["x-device"] ?: "unknown", "UTF-8")

        val resolver = ctx.contentResolver
        val values = ContentValues().apply {
            put(MediaStore.Downloads.DISPLAY_NAME, name)
            put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/WindowsAirDrop")
            put(MediaStore.Downloads.IS_PENDING, 1)
        }
        val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
            ?: return reply(c, 500, "cannot create file")
        try {
            resolver.openOutputStream(uri)!!.use { out ->
                val buf = ByteArray(256 * 1024)
                var left = length
                while (left > 0) {
                    val n = input.read(buf, 0, minOf(buf.size.toLong(), left).toInt())
                    if (n < 0) throw java.io.IOException("truncated upload")
                    out.write(buf, 0, n)
                    left -= n
                }
            }
            resolver.update(uri, ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) }, null, null)
        } catch (e: Exception) {
            resolver.delete(uri, null, null)
            return
        }
        reply(c, 200, "ok")
        onReceive(name, sender)
    }

    private fun sanitize(n: String): String {
        val base = n.replace('\\', '/').substringAfterLast('/')
        return base.replace(Regex("[<>:\"|?*\\u0000-\\u001f]"), "_").trim(' ', '.').ifEmpty { "file" }
    }

    // ---------- sending ----------
    fun send(peer: Peer, uri: Uri, pinToSend: String, progress: (Long, Long) -> Unit) {
        val resolver = ctx.contentResolver
        var fileName = "file"
        var size = -1L
        resolver.query(uri, null, null, null, null)?.use {
            if (it.moveToFirst()) {
                it.getColumnIndex(OpenableColumns.DISPLAY_NAME).takeIf { i -> i >= 0 }?.let { i -> fileName = it.getString(i) }
                it.getColumnIndex(OpenableColumns.SIZE).takeIf { i -> i >= 0 }?.let { i -> size = it.getLong(i) }
            }
        }
        require(size >= 0) { "Unknown file size" }
        val url = URL("http://${peer.ip}:${peer.port}/send?name=" + URLEncoder.encode(fileName, "UTF-8").replace("+", "%20"))
        val conn = (url.openConnection() as HttpURLConnection).apply {
            requestMethod = "PUT"
            doOutput = true
            connectTimeout = 10_000
            readTimeout = 30_000
            setFixedLengthStreamingMode(size)
            setRequestProperty("X-Device", URLEncoder.encode(name, "UTF-8").replace("+", "%20"))
            if (pinToSend.isNotEmpty()) setRequestProperty("X-Pin", pinToSend)
        }
        try {
            try {
                resolver.openInputStream(uri)!!.use { src ->
                    conn.outputStream.use { out ->
                        val buf = ByteArray(256 * 1024)
                        var sent = 0L
                        while (true) {
                            val n = src.read(buf)
                            if (n < 0) break
                            out.write(buf, 0, n)
                            sent += n
                            progress(sent, size)
                        }
                    }
                }
            } catch (e: java.io.IOException) {
                // receiver may have rejected early (e.g. wrong PIN); check its answer below
            }
            val code = conn.responseCode
            if (code != 200) throw RuntimeException(
                if (code == 403) "Wrong PIN" else "Receiver answered $code")
        } finally {
            conn.disconnect()
        }
    }
}
