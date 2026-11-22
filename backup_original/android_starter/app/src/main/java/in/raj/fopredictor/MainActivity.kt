package in.raj.fopredictor

import android.os.Bundle
import android.os.Build
import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

private const val API_BASE = "http://10.0.2.2:8000" // Emulator -> Windows host. For a phone, use PC LAN IP.

class MainActivity : ComponentActivity() {
    private val notificationPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        ReportReminder.schedule(this)
        if (Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
        setContent { PredictorApp() }
    }
}

@Composable
private fun PredictorApp() {
    var dark by remember { mutableStateOf(true) }
    var loading by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf("") }
    var report by remember { mutableStateOf<JSONObject?>(null) }
    val colors = if (dark) darkColorScheme() else lightColorScheme()

    MaterialTheme(colorScheme = colors) {
        Scaffold { padding ->
            LazyColumn(Modifier.fillMaxSize().padding(padding).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                item {
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                        Text("F&O Predictor", style = MaterialTheme.typography.headlineMedium)
                        Row { Text("Dark"); Switch(checked = dark, onCheckedChange = { dark = it }) }
                    }
                    Text("Research signals · Manual trading only", style = MaterialTheme.typography.bodyMedium)
                    Spacer(Modifier.height(8.dp))
                    Button(enabled = !loading, onClick = {
                        loading = true; error = ""
                    }) { Text(if (loading) "Loading…" else "Refresh predictions") }
                }
                if (loading) {
                    item {
                        LaunchedEffect(loading) {
                            try { report = fetchReport(); error = "" }
                            catch (e: Exception) { error = e.message ?: "Unable to connect to backend" }
                            finally { loading = false }
                        }
                        LinearProgressIndicator(Modifier.fillMaxWidth())
                    }
                }
                if (error.isNotBlank()) item { Text(error, color = MaterialTheme.colorScheme.error) }
                report?.let { r ->
                    item { Text("Universe: ${r.optInt("universe_count")} · No trade: ${r.optInt("no_trade_count")}") }
                    item { Text("TOP BULLISH", style = MaterialTheme.typography.titleLarge) }
                    items(readArray(r, "top_bullish"), key = { it.optString("symbol") }) { SignalCard(it) }
                    item { Text("TOP BEARISH", style = MaterialTheme.typography.titleLarge) }
                    items(readArray(r, "top_bearish"), key = { it.optString("symbol") }) { SignalCard(it) }
                    item { Text(r.optString("disclaimer"), style = MaterialTheme.typography.bodySmall) }
                }
            }
        }
    }
}

@Composable
private fun SignalCard(item: JSONObject) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text("${item.optString("symbol")}  ·  ${item.optString("signal")}", style = MaterialTheme.typography.titleMedium)
            Text("Score ${item.optInt("score")} · Strength ${item.optInt("confidence_estimate_pct")}%")
            Text("Spot reference ₹${item.optDouble("spot_ltp")}")
            Text("Entry ${item.optString("entry_reference")} · SL ${item.optString("stop_reference")}")
            Text(item.optJSONArray("reasons")?.let { a -> (0 until a.length()).joinToString(" · ") { a.optString(it) } } ?: "")
        }
    }
}

private fun readArray(root: JSONObject, key: String): List<JSONObject> {
    val a = root.optJSONArray(key) ?: return emptyList()
    return (0 until a.length()).mapNotNull { a.optJSONObject(it) }
}

private suspend fun fetchReport(): JSONObject = withContext(Dispatchers.IO) {
    val connection = (URL("$API_BASE/api/predictions").openConnection() as HttpURLConnection).apply {
        requestMethod = "GET"; connectTimeout = 8000; readTimeout = 15000
    }
    try {
        val stream = if (connection.responseCode in 200..299) connection.inputStream else connection.errorStream
        val body = stream.bufferedReader().use { it.readText() }
        if (connection.responseCode !in 200..299) throw IllegalStateException("Backend ${connection.responseCode}: $body")
        JSONObject(body)
    } finally { connection.disconnect() }
}
