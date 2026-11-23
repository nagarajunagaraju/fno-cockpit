package `in`.raj.fnocockpit

import android.content.Intent
import android.net.Uri
import android.os.PowerManager
import android.provider.Settings
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONObject

@Composable
fun Title(t: String, sub: String? = null) {
    Column(Modifier.padding(top = 12.dp, bottom = 4.dp)) {
        Text(t, fontSize = 20.sp, fontWeight = FontWeight.Bold)
        sub?.let { Text(it, color = Mut, fontSize = 12.sp) }
    }
}

// ============================================================ SIGNAL DETAIL
@Composable
fun SignalDetail(api: Api, id: Long) {
    val (s, err) = poll(5000, id) { api.obj("/api/engine/signals/$id") }
    LazyColumn(Modifier.fillMaxSize().padding(horizontal = 12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        item { ErrorText(err) }
        if (s == null) { item { Text("Loading…", color = Mut) }; return@LazyColumn }
        val p = s.getJSONObject("plan"); val e = s.optJSONObject("explanation") ?: JSONObject()
        val bull = s.optString("bias") == "BULL"
        item {
            Title("${s.optString("symbol")} · ${p.optString("direction")} → BUY ${p.optString("option_type")}",
                "Signal #$id · ${s.optString("status")} · ${s.optString("mode")} data")
        }
        item {
            Card(border = if (bull) Up else Dn) {
                Text(p.optString("tradingsymbol"), fontWeight = FontWeight.Bold, fontSize = 18.sp)
                KV2("Strike", n(p.dbl("strike")), "Expiry", "${p.optString("expiry")} (${n(p.dbl("dte"), 1)}d)")
                val er = p.optJSONArray("entry_range")
                KV2("Entry premium", n(p.dbl("entry_premium")), "Entry range", "${n(er?.optDouble(0))}–${n(er?.optDouble(1))}")
                KV2("Stop-loss", n(p.dbl("sl_premium")), "Target 1", n(p.dbl("t1_premium")), Dn, Up)
                KV2("Target 2", n(p.dbl("t2_premium")), "R:R T1/T2", "${n(p.dbl("rr_t1"))}/${n(p.dbl("rr_t2"))}", Up)
                KV2("Lots", "${p.optInt("lots")} × ${p.optInt("lot_size")} = ${p.optInt("qty")}", "Max loss", rs(p.dbl("max_loss")))
                KV2("Premium outlay", rs(p.dbl("premium_outlay")), "Risk budget", rs(p.dbl("risk_budget")))
                KV2("Spot entry", n(p.dbl("spot_entry")), "Spot SL", n(p.dbl("spot_sl")))
                KV2("Spot T1", n(p.dbl("spot_t1")), "Spot T2", n(p.dbl("spot_t2")))
                KV2("Score", "${n(p.dbl("score"), 1)}/${p.optInt("max_score")}", "Confidence", p.optString("confidence"))
                KV2("IV", "${n(p.dbl("iv"), 1)}%", "Delta", n(p.dbl("delta"), 2))
                KV2("Gamma", n(p.dbl("gamma"), 4), "Theta/day", n(p.dbl("theta")))
                KV2("Vega", n(p.dbl("vega")), "Spread", "${n(p.dbl("spread_pct"), 1)}%")
                KV("Signal time", p.optString("signal_time"))
                KV("Data time", p.optString("data_time"))
                p.optJSONObject("est_charges")?.let { KV("Est. charges (at SL)", rs(it.dbl("total"))) }
            }
        }
        if (s.dbl("fill_price") != null || s.str("outcome") != null) item {
            Card {
                Text("Paper trade", fontWeight = FontWeight.SemiBold)
                KV2("Fill", n(s.dbl("fill_price")), "Last bid", n(s.dbl("last_bid")))
                KV2("Remaining qty", "${s.optInt("remaining_qty")}", "Current SL", n(s.dbl("sl_current")))
                KV2("Charges", rs(s.dbl("charges")), "Net P&L", rs(s.dbl("net_pnl")), c2 = col(s.dbl("net_pnl")))
                s.optJSONArray("exits").objs().forEach { x -> Text("Exit ${x.optInt("qty")} @ ${n(x.dbl("price"))} · ${x.optString("reason")} · ${x.optString("time").drop(11).take(5)}", fontSize = 12.sp, color = Mut) }
                s.str("outcome")?.let { Text(it, color = Mut, fontSize = 12.sp) }
            }
        }
        item { Card { Text("Entry trigger", fontWeight = FontWeight.SemiBold); Text(p.optString("entry_trigger"), fontSize = 13.sp); Text(e.optString("why_entry"), fontSize = 13.sp, color = Mut) } }
        item { Card { Text("Why this stock", fontWeight = FontWeight.SemiBold); Text(e.optString("why_stock"), fontSize = 13.sp) } }
        item { Card { Text("Filters passed", fontWeight = FontWeight.SemiBold, color = Up); Bullets(e.optJSONArray("passed").strs(), mark = "✓") } }
        item {
            Card {
                Text("Filters failed / unavailable", fontWeight = FontWeight.SemiBold, color = Warn)
                val f = e.optJSONArray("failed").strs(); val u = e.optJSONArray("unavailable").strs()
                if (f.isEmpty() && u.isEmpty()) Text("none", color = Mut, fontSize = 13.sp)
                Bullets(f, Dn, "✗"); Bullets(u, Mut, "?")
            }
        }
        item {
            Card {
                Text("Derivatives", fontWeight = FontWeight.SemiBold)
                Text("Futures OI: " + e.optString("futures_oi"), fontSize = 13.sp)
                Text("Option chain: " + e.optString("option_chain"), fontSize = 13.sp)
                Text("Sector: " + e.optString("sector"), fontSize = 13.sp)
            }
        }
        item { Card { Text("Why this strike", fontWeight = FontWeight.SemiBold); Bullets(e.optJSONArray("why_strike").strs()) } }
        item {
            Card {
                Text("Strikes compared", fontWeight = FontWeight.SemiBold)
                s.optJSONObject("selection")?.optJSONArray("candidates").objs().forEach { c ->
                    val ok = c.optBoolean("eligible")
                    Text("${n(c.dbl("strike"))}  ${if (ok) "✓ score ${n(c.dbl("score"), 1)}" else "✗ " + c.optJSONArray("rejected").strs().firstOrNull()}" +
                        "  Δ ${n(c.dbl("delta"), 2)}  spr ${n(c.dbl("spread_pct"), 1)}%", fontSize = 12.sp, color = if (ok) Up else Mut)
                }
            }
        }
        item { Card(border = Dn) { Text("What invalidates the trade", fontWeight = FontWeight.SemiBold, color = Dn); Text(e.optString("invalidation"), fontSize = 13.sp) } }
        item { Card { Text("Main risks", fontWeight = FontWeight.SemiBold, color = Warn); Bullets(e.optJSONArray("risks").strs()) } }
        item { Card { Text("Plan notes", fontWeight = FontWeight.SemiBold); Bullets(p.optJSONArray("plan_notes").strs()) } }
        item { Spacer(Modifier.height(24.dp)) }
    }
}

// ============================================================ STOCK EVALUATION
@Composable
fun StockDetail(api: Api, symbol: String) {
    val (ev, err) = poll(5000, symbol) { api.obj("/api/engine/stock/$symbol") }
    LazyColumn(Modifier.fillMaxSize().padding(horizontal = 12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        item { Title(symbol, "Live evaluation · all four layers"); ErrorText(err) }
        if (ev == null) return@LazyColumn
        val m = ev.optJSONObject("metrics") ?: JSONObject()
        item {
            Card {
                KV2("Status", ev.optString("status"), "Score", "${n(ev.dbl("score"), 1)}/${ev.optInt("max_score")}", statusColor(ev.optString("status")))
                KV2("Price", n(m.dbl("price")), "Change", "${signed(m.dbl("chg_pct"))}%", c2 = col(m.dbl("chg_pct")))
                KV2("VWAP", n(m.dbl("vwap")), "RVOL", "${n(m.dbl("rvol"))}x")
                KV2("Day open", n(m.dbl("day_open")), "Day H/L", "${n(m.dbl("day_high"))}/${n(m.dbl("day_low"))}")
                KV2("PDH", n(m.dbl("pdh")), "PDL", n(m.dbl("pdl")))
                KV2("OR high", n(m.dbl("or_high")), "OR low", n(m.dbl("or_low")))
                KV2("Fut chg", "${signed(m.dbl("fut_chg_pct"))}%", "Fut OI chg", "${signed(m.dbl("fut_oi_chg_pct"))}%")
                KV("Build-up", m.str("buildup") ?: "n/a")
                ev.str("wait_reason")?.let { Text(it, color = Warn, fontSize = 13.sp) }
                ev.optJSONObject("trigger")?.let { Text("Trigger: " + it.optString("text"), fontSize = 12.sp, color = Mut) }
            }
        }
        items(ev.optJSONArray("checks").objs()) { c ->
            val passed = if (c.isNull("passed")) null else c.optBoolean("passed")
            Card(border = when (passed) { true -> Up.copy(alpha = .5f); false -> Dn.copy(alpha = .6f); null -> Line }) {
                Text("${when (passed) { true -> "✓"; false -> "✗"; null -> "?" }} L${c.optInt("layer")} ${c.optString("label")}" +
                    (if (c.optBoolean("mandatory")) "  [mandatory]" else "") +
                    (if (c.optDouble("max_points", 0.0) > 0) "  ${n(c.dbl("points"), 1)}/${n(c.dbl("max_points"), 1)} pts" else ""), fontSize = 13.sp)
                Text("Observed: ${c.optString("value")} · Needs: ${c.optString("threshold")}", fontSize = 12.sp, color = Mut)
                c.str("note")?.takeIf { it.isNotBlank() }?.let { Text(it, fontSize = 12.sp, color = Mut) }
            }
        }
        item { Spacer(Modifier.height(24.dp)) }
    }
}

// ============================================================ SCANNER
@Composable
fun ScannerScreen(api: Api, go: (Screen) -> Unit) {
    val (sc, err) = poll(5000) { api.obj("/api/engine/scanner") }
    LazyColumn(Modifier.fillMaxSize().padding(horizontal = 12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        item { Title("Live scanner", sc?.optString("updated")); ErrorText(err) }
        if (sc == null) return@LazyColumn
        val c = sc.optJSONObject("counts") ?: JSONObject()
        item {
            Card {
                KV2("Universe", "${sc.optInt("universe")}", "Option-checked", "${sc.optInt("option_checked")}")
                KV2("Trigger", "${c.optInt("TRIGGER")}", "Ready", "${c.optInt("READY")}")
                KV2("Watching", "${c.optInt("WATCHING")}", "Rejected", "${c.optInt("REJECTED")}")
                KV2("No setup", "${c.optInt("NO_SETUP")}", "Cycle", "${sc.optInt("cycle_ms")} ms")
            }
        }
        item { SectionTitle("Bullish") }
        items(sc.optJSONArray("bullish").objs(), key = { "b" + it.optString("symbol") }) { SetupRow(it) { go(Screen.Stock(it.optString("symbol"))) } }
        item { SectionTitle("Bearish") }
        items(sc.optJSONArray("bearish").objs(), key = { "s" + it.optString("symbol") }) { SetupRow(it) { go(Screen.Stock(it.optString("symbol"))) } }
        item { SectionTitle("Recent trigger decisions") }
        items(sc.optJSONArray("trigger_log").objs().reversed()) { t ->
            Text("${t.optString("time").drop(11).take(5)} ${t.optString("symbol")} ${t.optString("direction")} → ${t.optString("result")}", fontSize = 12.sp, color = Mut)
        }
        item { SectionTitle("Rejected (mandatory filter failed)") }
        items(sc.optJSONArray("rejected").objs(), key = { "r" + it.optString("symbol") }) { x ->
            Text("${x.optString("symbol")} ${x.optString("direction")}: ${x.optJSONArray("reject_reasons").strs().joinToString(" · ")}", fontSize = 12.sp, color = Mut)
        }
        item { Spacer(Modifier.height(24.dp)) }
    }
}

// ============================================================ PAPER
@Composable
fun PaperScreen(api: Api, go: (Screen) -> Unit) {
    val (p, err) = poll(5000) { api.obj("/api/engine/paper") }
    LazyColumn(Modifier.fillMaxSize().padding(horizontal = 12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        item { Title("Paper trading", "Same signal engine · realistic fills, slippage and charges"); ErrorText(err) }
        if (p == null) return@LazyColumn
        val st = p.optJSONObject("stats_all_time") ?: JSONObject()
        item {
            Card {
                Text("All-time paper results", fontWeight = FontWeight.SemiBold)
                KV2("Trades", "${st.optInt("trades")}", "Win rate", "${n(st.dbl("win_rate"), 1)}%")
                KV2("Avg profit", rs(st.dbl("avg_profit")), "Avg loss", rs(st.dbl("avg_loss")), Up, Dn)
                KV2("Profit factor", n(st.dbl("profit_factor")), "Expectancy", rs(st.dbl("expectancy")))
                KV2("Max drawdown", rs(st.dbl("max_drawdown")), "Net", rs(st.dbl("net")), Dn, col(st.dbl("net")))
                st.optJSONObject("by_side")?.let { bs ->
                    bs.keys().forEach { k -> val g = bs.getJSONObject(k); KV("$k", "${g.optInt("trades")} trades · ${n(g.dbl("win_rate"), 1)}% · ${rs(g.dbl("pnl"))}") }
                }
            }
        }
        val open = p.optJSONArray("open").objs()
        item { SectionTitle("Open (${open.size})") }
        items(open, key = { "o" + it.optLong("id") }) { SignalCard(it) { go(Screen.Signal(it.optLong("id"))) } }
        val closed = p.optJSONArray("closed_today").objs()
        item { SectionTitle("Closed today (${closed.size})") }
        items(closed, key = { "c" + it.optLong("id") }) { SignalCard(it) { go(Screen.Signal(it.optLong("id"))) } }
        item { Spacer(Modifier.height(24.dp)) }
    }
}

// ============================================================ HISTORY
@Composable
fun HistoryScreen(api: Api, go: (Screen) -> Unit) {
    val (xs, err) = poll(30000) { api.arr("/api/engine/signals?limit=200") }
    LazyColumn(Modifier.fillMaxSize().padding(horizontal = 12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        item { Title("Signal history"); ErrorText(err) }
        val list = xs.objs()
        if (xs != null && list.isEmpty()) item { Text("No signals yet.", color = Mut) }
        items(list, key = { it.optLong("id") }) { s ->
            Card(onClick = { go(Screen.Signal(s.optLong("id"))) }) {
                Row {
                    Text("#${s.optLong("id")} ${s.optString("day")} ${s.optString("symbol")}", fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
                    Pill(s.optString("status"), statusColor(s.optString("status")))
                }
                Text("${s.optString("tradingsymbol")} · score ${n(s.dbl("score"), 1)} · net ${rs(s.dbl("net_pnl"))}", fontSize = 12.sp, color = Mut)
                s.str("outcome")?.let { Text(it, fontSize = 12.sp, color = Mut) }
            }
        }
    }
}

// ============================================================ BACKTEST
@Composable
fun BacktestScreen(api: Api) {
    val scope = rememberCoroutineScope()
    var days by remember { mutableStateOf("120") }
    var msg by remember { mutableStateOf<String?>(null) }
    val (b, err) = poll(4000) { api.obj("/api/engine/backtest") }
    LazyColumn(Modifier.fillMaxSize().padding(horizontal = 12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        item { Title("Backtesting", "Same engine replayed on 1-min history · no look-ahead"); ErrorText(err ?: msg) }
        item {
            Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(days, { days = it.filter(Char::isDigit).take(3) }, label = { Text("Days") }, singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number), modifier = Modifier.width(110.dp))
                Button(enabled = b?.optBoolean("running") != true, onClick = {
                    scope.launch {
                        msg = try { api.post("/api/engine/backtest", JSONObject().put("days", days.toIntOrNull() ?: 120)); "Started" } catch (e: Exception) { e.message }
                    }
                }) { Text(if (b?.optBoolean("running") == true) "Running…" else "Run backtest") }
            }
            b?.optJSONObject("progress")?.let { if (b.optBoolean("running")) Text("Progress: ${it.optString("stage")} ${it.optInt("done")}/${it.optInt("total")}", color = Mut, fontSize = 12.sp) }
            b?.str("error")?.let { Text(it, color = Dn) }
            Text("First run downloads 1-min history (can take several minutes for all stocks). Result is cached for the day.", color = Mut, fontSize = 12.sp)
        }
        val r = b?.optJSONObject("last") ?: return@LazyColumn
        val s = r.optJSONObject("stats") ?: JSONObject()
        item {
            Card(border = if (r.optString("sufficiency") == "OK") Up else Warn) {
                Text("${r.optString("source")} · ${r.optJSONArray("tested_days")?.length()} days · ${r.optInt("symbols")} stocks", fontWeight = FontWeight.SemiBold)
                Text(r.optString("sufficiency"), color = if (r.optString("sufficiency") == "OK") Up else Warn, fontSize = 13.sp)
                KV2("Trades", "${s.optInt("trades")}", "Win rate", "${n(s.dbl("win_rate"), 1)}%")
                KV2("Avg win", "${n(s.dbl("avg_profit"))}R", "Avg loss", "${n(s.dbl("avg_loss"))}R", Up, Dn)
                KV2("Profit factor", n(s.dbl("profit_factor")), "Expectancy", "${n(s.dbl("expectancy"))}R")
                KV2("Max DD", "${n(s.dbl("max_drawdown"))}R", "Missed fills", "${s.optInt("missed_fills")}")
                Text("Unit: " + s.optString("unit"), color = Mut, fontSize = 11.sp)
            }
        }
        item { Card { Text("Important notes", fontWeight = FontWeight.SemiBold, color = Warn); Bullets(r.optJSONArray("notes").strs()) } }
        listOf("by_hour" to "By time of day (hour)", "by_score" to "By grade / score", "by_regime" to "By market regime", "by_side" to "CE vs PE", "by_symbol" to "By stock").forEach { (k, t) ->
            val g = s.optJSONObject(k) ?: return@forEach
            item {
                Card {
                    Text(t, fontWeight = FontWeight.SemiBold)
                    g.keys().asSequence().sorted().forEach { key ->
                        val v = g.getJSONObject(key)
                        KV(key, "${v.optInt("trades")} · ${n(v.dbl("win_rate"), 1)}% · ${n(v.dbl("pnl"))}R", col(v.dbl("pnl")))
                    }
                }
            }
        }
        item { Spacer(Modifier.height(24.dp)) }
    }
}

// ============================================================ RISK
private val RISK_FIELDS = listOf(
    "capital" to "Capital (₹)", "risk_per_trade_pct" to "Risk per trade (%)", "max_daily_loss_pct" to "Max daily loss (%)",
    "max_open_positions" to "Max simultaneous positions", "max_exposure_pct" to "Max exposure (% of capital)",
    "max_premium_pct" to "Max premium per trade (%)", "min_score" to "Minimum score (of 10)", "min_rr" to "Minimum reward:risk",
    "max_spread_pct" to "Max bid-ask spread (%)", "max_signals_per_symbol_day" to "Max signals per stock per day",
)

@Composable
fun RiskScreen(api: Api) {
    val scope = rememberCoroutineScope()
    var vals by remember { mutableStateOf<Map<String, String>>(emptyMap()) }
    var msg by remember { mutableStateOf<String?>(null) }
    val (r, err) = poll(5000) { api.obj("/api/engine/risk") }
    LaunchedEffect(Unit) {
        try {
            val c = api.obj("/api/engine/config").getJSONObject("config")
            vals = RISK_FIELDS.associate { (k, _) -> k to c.opt(k).toString() }
        } catch (e: Exception) { msg = e.message }
    }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Title("Risk management", "Paper mode · high score can never bypass these"); ErrorText(err)
        r?.optJSONObject("state")?.let { st ->
            Card {
                KV2("Day P&L", rs(st.dbl("day_pnl")), "Loss limit", rs(st.dbl("max_daily_loss")), col(st.dbl("day_pnl")))
                KV2("Open", "${st.optInt("open_positions")}/${st.optInt("max_open_positions")}", "Exposure", "${rs(st.dbl("exposure"))}/${rs(st.dbl("max_exposure"))}")
                KV("Trading lock", if (st.optBoolean("locked")) "LOCKED for today" else "open", if (st.optBoolean("locked")) Dn else Up)
            }
        }
        RISK_FIELDS.forEach { (k, label) ->
            OutlinedTextField(vals[k] ?: "", { v -> vals = vals + (k to v) }, label = { Text(label) }, singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal), modifier = Modifier.fillMaxWidth())
        }
        Button(onClick = {
            scope.launch {
                msg = try {
                    val body = JSONObject(); vals.forEach { (k, v) -> v.toDoubleOrNull()?.let { body.put(k, it) } }
                    api.post("/api/engine/config", body); "Saved"
                } catch (e: Exception) { e.message }
            }
        }, modifier = Modifier.fillMaxWidth()) { Text("Save risk settings") }
        msg?.let { Text(it, color = if (it == "Saved") Up else Dn) }
        Spacer(Modifier.height(24.dp))
    }
}

// ============================================================ HEALTH
@Composable
fun HealthScreen(api: Api, prefs: Prefs) {
    val ctx = LocalContext.current
    val (h, err) = poll(5000) { api.obj("/api/engine/health") }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Title("Data / API health"); ErrorText(err)
        Card {
            KV("Phone alert watcher", prefs.serviceStatus)
            KV("Server", prefs.serverUrl)
        }
        h?.let {
            val f = it.optJSONObject("feed") ?: JSONObject(); val sc = it.optJSONObject("scanner") ?: JSONObject()
            Card {
                KV("Server version", it.optString("version")); KV("Server time", it.optString("server_time"))
                KV("Execution", it.optString("execution")); KV("Scanner", "${sc.optString("status")} · ${sc.optInt("cycle_ms")} ms")
                KV("Scanner owner", it.optString("singleton"))
                KV("Feed mode", f.optString("mode")); KV("Feed stage", f.optString("stage"))
                KV("Kite login", if (f.optBoolean("logged_in")) "yes" else "LOGIN NEEDED", if (f.optBoolean("logged_in")) Up else Dn)
                KV("WebSocket", f.optString("ticker")); KV("Last tick age", "${f.opt("last_tick_age_s")} s")
                KV("REST fallback", if (f.optBoolean("rest_fallback")) "ON" else "off", if (f.optBoolean("rest_fallback")) Warn else Up)
                KV("Universe / RVOL baselines", "${f.opt("universe")} / ${f.opt("baseline_loaded")}")
                f.optJSONObject("source_progress")?.let { sp -> KV("History load", "${sp.optInt("loaded")}/${sp.optInt("total")} ${sp.optString("message")}") }
                sc.str("error")?.let { e -> Text(e, color = Dn, fontSize = 12.sp) }
            }
            val errs = f.optJSONArray("errors").strs()
            if (errs.isNotEmpty()) Card { Text("Recent feed errors", fontWeight = FontWeight.SemiBold, color = Warn); errs.takeLast(8).forEach { e -> Text(e, fontSize = 11.sp, color = Mut) } }
            if (f.optString("mode") == "LIVE" && !f.optBoolean("logged_in")) {
                Button(onClick = { openUrl(ctx, prefs.serverUrl + "/login") }, modifier = Modifier.fillMaxWidth()) { Text("Login with Kite") }
            }
        }
    }
}

// ============================================================ SETTINGS / SETUP
@Composable
fun SettingsScreen(prefs: Prefs, first: Boolean, done: () -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var url by remember { mutableStateOf(prefs.serverUrl.ifBlank { "https://" }) }
    var tok by remember { mutableStateOf(prefs.token) }
    var alerts by remember { mutableStateOf(prefs.alertsEnabled) }
    var msg by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Title(if (first) "Welcome to F&O Cockpit" else "Settings", if (first) "Connect the app to your own Cockpit server" else null)
        OutlinedTextField(url, { url = it }, label = { Text("Server URL (e.g. https://fno.yourdomain.in)") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri))
        OutlinedTextField(tok, { tok = it }, label = { Text("App token (from server data/app_token.txt)") }, singleLine = true,
            visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth())
        Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
            Switch(alerts, { alerts = it }); Spacer(Modifier.width(8.dp)); Text("Background signal alerts")
        }
        Button(enabled = !busy, modifier = Modifier.fillMaxWidth(), onClick = {
            busy = true
            scope.launch {
                try {
                    val h = Api(url.trim().trimEnd('/'), tok.trim()).obj("/api/engine/health")
                    prefs.serverUrl = url; prefs.token = tok; prefs.alertsEnabled = alerts; prefs.lastEventId = -1
                    if (alerts) AlertService.start(ctx) else AlertService.stop(ctx)
                    msg = "Connected · server ${h.optString("version")} · feed ${h.optJSONObject("feed")?.optString("mode")}"
                    delay(700); done()
                } catch (e: Exception) { msg = "Failed: ${e.message}" }
                busy = false
            }
        }) { Text(if (busy) "Testing…" else "Test connection & save") }
        msg?.let { Text(it, color = if (it.startsWith("Connected")) Up else Dn) }
        if (!first) {
            Divider()
            Text("Daily: Kite login (roju okasari, ~9:00 AM)", fontWeight = FontWeight.SemiBold)
            Button(onClick = { openUrl(ctx, prefs.serverUrl + "/login") }, modifier = Modifier.fillMaxWidth()) { Text("Login with Kite") }
            Text("Alerts reliable ga ravalante battery optimisation off cheyi:", color = Mut, fontSize = 13.sp)
            OutlinedButton(modifier = Modifier.fillMaxWidth(), onClick = {
                val pm = ctx.getSystemService(PowerManager::class.java)
                if (!pm.isIgnoringBatteryOptimizations(ctx.packageName)) {
                    ctx.startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:" + ctx.packageName)))
                } else msg = "Battery optimisation already off"
            }) { Text("Allow background running") }
            OutlinedButton(modifier = Modifier.fillMaxWidth(), onClick = {
                ctx.startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, ctx.packageName))
            }) { Text("Notification settings") }
            OutlinedButton(modifier = Modifier.fillMaxWidth(), onClick = { openUrl(ctx, prefs.serverUrl + "/static/signals.html?token=" + prefs.token) }) {
                Text("Open web dashboard")
            }
        }
        Text("This app never places orders. Signals are research + paper trading; you decide and trade manually in Kite.", color = Mut, fontSize = 12.sp)
    }
}
