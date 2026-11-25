package `in`.raj.fnocockpit

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

class ApiException(msg: String, val code: Int = 0) : Exception(msg)

/** Tiny JSON client for the Cockpit server (no third-party libraries). */
class Api(private val base: String, private val token: String) {

    private fun call(path: String, method: String = "GET", body: String? = null): String {
        val c = (URL(base + path).openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = 8000
            readTimeout = 20000
            setRequestProperty("X-App-Token", token)
            setRequestProperty("Accept", "application/json")
            if (body != null) {
                doOutput = true
                setRequestProperty("Content-Type", "application/json")
                outputStream.use { it.write(body.toByteArray()) }
            }
        }
        try {
            val code = c.responseCode
            val stream = if (code in 200..299) c.inputStream else c.errorStream
            val text = stream?.bufferedReader()?.use { it.readText() } ?: ""
            if (code !in 200..299) {
                val detail = runCatching { JSONObject(text).optString("detail") }.getOrNull()
                throw ApiException(
                    when (code) {
                        401 -> "Wrong app token (check Settings)"
                        else -> "Server $code: ${detail?.ifBlank { null } ?: text.take(160)}"
                    }, code
                )
            }
            return text
        } finally {
            c.disconnect()
        }
    }

    suspend fun obj(path: String): JSONObject = withContext(Dispatchers.IO) { JSONObject(call(path)) }
    suspend fun arr(path: String): JSONArray = withContext(Dispatchers.IO) { JSONArray(call(path)) }
    suspend fun post(path: String, body: JSONObject): JSONObject =
        withContext(Dispatchers.IO) { JSONObject(call(path, "POST", body.toString())) }

    /** Raw bytes (Excel download). */
    suspend fun bytes(path: String): ByteArray = withContext(Dispatchers.IO) {
        val c = (URL(base + path).openConnection() as HttpURLConnection).apply {
            connectTimeout = 8000; readTimeout = 30000; setRequestProperty("X-App-Token", token)
        }
        try {
            if (c.responseCode !in 200..299) throw ApiException("Server ${c.responseCode}", c.responseCode)
            c.inputStream.use { it.readBytes() }
        } finally { c.disconnect() }
    }

    /** Blocking variant for the background service thread. */
    fun eventsBlocking(after: Long): JSONArray = JSONArray(call("/api/engine/events?after=$after&limit=50"))
    fun lastEventIdBlocking(): Long = JSONObject(call("/api/engine/events/last")).optLong("last_id", 0)
}
