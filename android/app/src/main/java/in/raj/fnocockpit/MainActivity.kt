package `in`.raj.fnocockpit

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import kotlinx.coroutines.delay
import org.json.JSONObject

sealed class Screen {
    data object Setup : Screen()
    data object Home : Screen()
    data object Scanner : Screen()
    data object Paper : Screen()
    data object More : Screen()
    data class Signal(val id: Long) : Screen()
    data class Stock(val symbol: String) : Screen()
    data object History : Screen()
    data object Backtest : Screen()
    data object Risk : Screen()
    data object Health : Screen()
    data object Settings : Screen()
}

class MainActivity : ComponentActivity() {
    private val openSignal = mutableStateOf(0L)
    private val notifPerm = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Notif.channels(this)
        openSignal.value = intent?.getLongExtra("signal_id", 0L) ?: 0L
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            notifPerm.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
        AlertService.start(this)
        setContent { MaterialTheme(colorScheme = AppColors) { Surface(Modifier.fillMaxSize()) { App(Prefs(this), openSignal) } } }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        openSignal.value = intent.getLongExtra("signal_id", 0L)
    }
}

@Composable
fun App(prefs: Prefs, openSignal: MutableState<Long>) {
    var screen by remember { mutableStateOf<Screen>(if (prefs.configured) Screen.Home else Screen.Setup) }
    val stack = remember { mutableStateListOf<Screen>() }
    fun go(s: Screen) { stack.add(screen); screen = s }
    fun tab(s: Screen) { stack.clear(); screen = s }
    LaunchedEffect(openSignal.value) {
        if (openSignal.value > 0 && prefs.configured) { go(Screen.Signal(openSignal.value)); openSignal.value = 0 }
    }
    BackHandler(enabled = stack.isNotEmpty()) { screen = stack.removeAt(stack.lastIndex) }
    val api = remember(prefs.serverUrl, prefs.token, screen) { Api(prefs.serverUrl, prefs.token) }
    val isTab = screen in listOf(Screen.Home, Screen.Scanner, Screen.Paper, Screen.More)

    Scaffold(
        bottomBar = {
            if (isTab) NavigationBar {
                listOf(Triple(Screen.Home, "Signals", Icons.Filled.Notifications), Triple(Screen.Scanner, "Scanner", Icons.Filled.Search),
                    Triple(Screen.Paper, "Paper", Icons.Filled.ShoppingCart), Triple(Screen.More, "More", Icons.Filled.Menu)).forEach { (s, l, i) ->
                    NavigationBarItem(selected = screen == s, onClick = { tab(s) }, icon = { Icon(i, l) }, label = { Text(l) })
                }
            }
        }
    ) { pad ->
        Box(Modifier.padding(pad)) {
            when (val s = screen) {
                Screen.Setup -> SettingsScreen(prefs, first = true) { tab(Screen.Home) }
                Screen.Home -> HomeScreen(api, prefs, ::go)
                Screen.Scanner -> ScannerScreen(api, ::go)
                Screen.Paper -> PaperScreen(api, ::go)
                Screen.More -> MoreScreen(::go)
                is Screen.Signal -> SignalDetail(api, s.id)
                is Screen.Stock -> StockDetail(api, s.symbol)
                Screen.History -> HistoryScreen(api, ::go)
                Screen.Backtest -> BacktestScreen(api)
                Screen.Risk -> RiskScreen(api)
                Screen.Health -> HealthScreen(api, prefs)
                Screen.Settings -> SettingsScreen(prefs, first = false) { tab(Screen.Home) }
            }
        }
    }
}

/** Polls a loader every [ms] while the composable is on screen. */
@Composable
fun <T> poll(ms: Long, key: Any? = Unit, load: suspend () -> T): Pair<T?, String?> {
    var data by remember(key) { mutableStateOf<T?>(null) }
    var err by remember(key) { mutableStateOf<String?>(null) }
    LaunchedEffect(key) {
        while (true) {
            try { data = load(); err = null } catch (e: Exception) { err = e.message ?: e.toString() }
            delay(ms)
        }
    }
    return data to err
}

fun openUrl(ctx: android.content.Context, url: String) {
    ctx.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
}

// ============================================================ HOME (main screen)
@Composable
fun HomeScreen(api: Api, prefs: Prefs, go: (Screen) -> Unit) {
    val ctx = androidx.compose.ui.platform.LocalContext.current
    val (d, err) = poll(5000) {
        JSONObject().put("health", api.obj("/api/engine/health")).put("scanner", api.obj("/api/engine/scanner"))
            .put("paper", api.obj("/api/engine/paper")).put("risk", api.obj("/api/engine/risk"))
            .put("wl", runCatching { api.obj("/api/engine/watchlist") }.getOrNull() ?: JSONObject())
    }
    LazyColumn(Modifier.fillMaxSize().padding(horizontal = 12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        item {
            Row(Modifier.fillMaxWidth().padding(top = 12.dp), verticalAlignment = Alignment.CenterVertically) {
                Text("F&O Cockpit", fontSize = 22.sp, fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
                val feed = d?.optJSONObject("health")?.optJSONObject("feed")
                Pill("${feed?.optString("mode") ?: "…"} · PAPER", if (feed?.optString("mode") == "LIVE") Up else Warn)
            }
            ErrorText(err)
        }
        if (d == null) { item { Text("Connecting to ${prefs.serverUrl}…", color = Mut) }; return@LazyColumn }
        val h = d.getJSONObject("health"); val feed = h.optJSONObject("feed") ?: JSONObject()
        val sc = d.getJSONObject("scanner"); val paper = d.getJSONObject("paper"); val risk = d.getJSONObject("risk").getJSONObject("state")
        val mk = sc.optJSONObject("market") ?: JSONObject()
        item {
            Card {
                fun pc(o: JSONObject?) = o?.let { if (it.dbl("prev_close") != null && it.dbl("ltp") != null) (it.getDouble("ltp") / it.getDouble("prev_close") - 1) * 100 else null }
                val nf = pc(mk.optJSONObject("nifty")); val bn = pc(mk.optJSONObject("banknifty"))
                KV2("NIFTY 50", "${signed(nf)}%", "BANK NIFTY", "${signed(bn)}%", col(nf), col(bn))
                val br = mk.optJSONObject("breadth")
                KV2("Breadth", br?.let { "${it.optInt("adv")}↑ ${it.optInt("dec")}↓" } ?: "—", "Updated", sc.optString("updated").drop(11).take(8))
                val rg = sc.optJSONObject("regime")
                KV2("Regime", rg?.optString("regime")?.replace("_", " ") ?: "—", "Alerts today", "${sc.optInt("alerts_today")} / ${sc.optInt("max_alerts")}",
                    when (rg?.optString("allows")) { "CE" -> Up; "PE" -> Dn; else -> Warn })
                KV2("Day P&L", rs(risk.dbl("day_pnl")), "Open", "${risk.optInt("open_positions")}/${risk.optInt("max_open_positions")}", col(risk.dbl("day_pnl")))
                if (risk.optBoolean("locked")) Text("DAILY LOSS LOCK ACTIVE — no new signals today", color = Dn, fontWeight = FontWeight.Bold)
                if (feed.optString("mode") == "LIVE" && !feed.optBoolean("logged_in")) {
                    Text("Kite login needed today", color = Dn)
                    Button(onClick = { openUrl(ctx, prefs.serverUrl + "/login") }) { Text("Login with Kite") }
                } else if (sc.optString("status") != "running") {
                    Text("Data: ${feed.optString("stage")} ${feed.optJSONObject("source_progress")?.optString("message") ?: ""}", color = Warn, fontSize = 12.sp)
                }
            }
        }
        val rep = d.optJSONObject("wl")?.optJSONObject("report")
        item { WatchlistCard(rep, d.optJSONObject("wl")?.optString("today") ?: "", go) }
        val counts = sc.optJSONObject("counts") ?: JSONObject()
        val bull = sc.optJSONArray("bullish").objs(); val bear = sc.optJSONArray("bearish").objs()
        item {
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                Pill("Paiki ${bull.size}", Up); Pill("Kindiki ${bear.size}", Dn)
                Pill("Ready ${counts.optInt("READY")}", Acc); Pill("Rejected ${counts.optInt("REJECTED")}")
            }
        }
        val open = paper.optJSONArray("open").objs()
        val closed = paper.optJSONArray("closed_today").objs()
        item { SectionTitle("Active signals (${open.size})") }
        if (open.isEmpty()) item {
            Text("No active signal. Strong stock VWAP ki pullback ayyi malli turn ayinappude alert (roju max 3). NO TRADE kuda valid answer.", color = Mut, fontSize = 13.sp)
        }
        items(open, key = { "o" + it.optLong("id") }) { SignalCard(it) { go(Screen.Signal(it.optLong("id"))) } }
        item { SectionTitle("Paiki velle setups (bullish) · Stage 1/2") }
        if (bull.isEmpty()) item { Text("None right now", color = Mut, fontSize = 13.sp) }
        items(bull.take(8), key = { "b" + it.optString("symbol") }) { SetupRow(it) { go(Screen.Stock(it.optString("symbol"))) } }
        item { SectionTitle("Kindiki padipoye setups (bearish) · Stage 1/2") }
        if (bear.isEmpty()) item { Text("None right now", color = Mut, fontSize = 13.sp) }
        items(bear.take(8), key = { "s" + it.optString("symbol") }) { SetupRow(it) { go(Screen.Stock(it.optString("symbol"))) } }
        if (closed.isNotEmpty()) {
            item { SectionTitle("Today's closed signals (${closed.size})") }
            items(closed, key = { "c" + it.optLong("id") }) { SignalCard(it) { go(Screen.Signal(it.optLong("id"))) } }
        }
        item { Spacer(Modifier.height(24.dp)) }
    }
}

@Composable
fun MoreScreen(go: (Screen) -> Unit) {
    Column(Modifier.fillMaxSize().padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("More", fontSize = 22.sp, fontWeight = FontWeight.Bold)
        listOf("Signal history" to Screen.History, "Backtesting" to Screen.Backtest, "Risk management" to Screen.Risk,
            "Data / API health" to Screen.Health, "Settings (server, token, alerts)" to Screen.Settings).forEach { (t, s) ->
            Card(onClick = { go(s) }) { Text(t, fontSize = 16.sp) }
        }
        Text("Paper trading mode only. App never places orders. Trade manually in Kite after your own check.", color = Mut, fontSize = 12.sp)
    }
}

@Composable
fun WatchlistCard(rep: JSONObject?, today: String, go: (Screen) -> Unit) {
    Card(border = Acc) {
        if (rep == null) {
            Text("FII watchlist", fontWeight = FontWeight.SemiBold)
            Text("Prati roju evening ~7 PM tarvata vastundi (aa roju Kite login undali).", color = Mut, fontSize = 12.sp)
            return@Card
        }
        val fii = rep.optJSONObject("fii") ?: JSONObject()
        val mood = fii.str("mood") ?: "UNKNOWN"
        val forDate = rep.optString("for_date")
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(if (forDate == today) "Today's FII watchlist" else "Watchlist for $forDate", fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
            Pill("FII $mood", when (mood) { "BULLISH" -> Up; "BEARISH" -> Dn; else -> Warn })
        }
        fii.str("reason")?.let { Text(it, color = Mut, fontSize = 11.sp) }
        val picks = rep.optJSONArray("picks").objs()
        if (picks.isEmpty()) Text("Evvi pass avvaledu (build-up + trend + ban/results). NO TRADE bias.", color = Mut, fontSize = 13.sp)
        picks.forEach { p ->
            val ce = p.optString("side") == "CE"
            Row(Modifier.fillMaxWidth().padding(top = 4.dp), verticalAlignment = Alignment.CenterVertically) {
                Pill(p.optString("side"), if (ce) Up else Dn)
                Spacer(Modifier.width(6.dp))
                Column(Modifier.weight(1f)) {
                    TextButton(onClick = { go(Screen.Stock(p.optString("symbol"))) }, contentPadding = PaddingValues(0.dp)) {
                        Text(p.optString("symbol"), fontWeight = FontWeight.Bold)
                    }
                    Text("${signed(p.dbl("price_pct"))}% · OI ${signed(p.dbl("oi_pct"), 1)}% · ${p.optString("buildup")}", fontSize = 12.sp, color = if (ce) Up else Dn)
                    Text(p.optString("plan"), fontSize = 11.sp, color = Mut)
                }
            }
        }
        rep.optJSONArray("notes")?.let { if (it.length() > 0) Text("Note: " + it.optString(0), color = Warn, fontSize = 11.sp) }
        Text("Engine ee stocks ki priority istundi; entry malli live pullback confirm ayyakane.", color = Mut, fontSize = 11.sp)
    }
}
