package `in`.raj.fnocockpit

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch
import org.json.JSONObject

private fun kindColor(k: String): Color = when (k) {
    "SIGNAL", "PLAN" -> Acc
    "T1", "EXIT" -> Up
    "SETUP" -> Color(0xFF7FB3FF)
    "WAIT" -> Warn
    "LEVEL" -> Color(0xFFC792EA)
    else -> Mut
}

@Composable
fun SensexScreen(api: Api, go: (Screen) -> Unit) {
    val (d, err) = poll(5000) {
        JSONObject().put("live", api.obj("/api/sensex/live"))
            .put("sig", runCatching { api.arr("/api/engine/signals?limit=20") }.getOrNull() ?: org.json.JSONArray())
    }
    var showLevels by remember { mutableStateOf(true) }
    val v = d?.optJSONObject("live")
    val live = v?.optJSONObject("live")
    val br = v?.optJSONObject("brain")
    val st = v?.optJSONObject("status")
    LazyColumn(Modifier.fillMaxSize().padding(horizontal = 12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        item {
            Row(Modifier.fillMaxWidth().padding(top = 12.dp), verticalAlignment = Alignment.CenterVertically) {
                Text("SENSEX LIVE", fontSize = 20.sp, fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
                val b = br?.optString("bias") ?: "—"
                Pill(b, when (b) { "BULLISH" -> Up; "BEARISH" -> Dn; "SIDEWAYS" -> Warn; else -> Mut })
            }
            ErrorText(err)
        }
        item {
            Card(border = Acc) {
                Row(verticalAlignment = Alignment.Bottom) {
                    Text(n(live?.dbl("ltp"), 2), fontSize = 30.sp, fontWeight = FontWeight.Bold)
                    Spacer(Modifier.width(8.dp))
                    Text("${signed(live?.dbl("chg_pct"))}%", fontSize = 18.sp, color = col(live?.dbl("chg_pct")))
                }
                KV2("NIFTY", "${signed(live?.dbl("nifty_chg"))}%", "VIX", "${n(live?.dbl("vix"))} (${signed(live?.dbl("vix_chg"))}%)",
                    col(live?.dbl("nifty_chg")), if ((live?.dbl("vix_chg") ?: 0.0) > 5) Dn else Color.Unspecified)
                KV2("Day high", n(br?.dbl("day_high"), 0), "Day low", n(br?.dbl("day_low"), 0))
                KV2("Opening range", if (br?.dbl("orh") != null) "${n(br.dbl("orl"), 0)} – ${n(br.dbl("orh"), 0)}" else "forming", "EMA20 (5m)", n(br?.dbl("ema20_5m"), 0))
                KV2("Trades today", "${br?.optInt("trades_today") ?: 0} / 2", "Option expiry", v?.str("expiry") ?: "—")
                Text("Status: ${st?.optString("stage") ?: "…"}" + (st?.str("error")?.let { " · $it" } ?: ""), color = Mut, fontSize = 11.sp)
            }
        }
        // active Sensex trade (paper tracked)
        val today = v?.str("day")
        d?.optJSONArray("sig").objs().filter { it.optString("symbol") == "SENSEX" && it.optString("day") == today &&
                it.optString("status") in listOf("TRIGGERED", "ACTIVE") }.forEach { s ->
            item { SignalCard(s) { go(Screen.Signal(s.optLong("id"))) } }
        }
        item {
            Card {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("Support / Resistance", fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
                    TextButton(onClick = { showLevels = !showLevels }) { Text(if (showLevels) "Hide" else "Show") }
                }
                val oi = v?.optJSONObject("oi")
                Text("Option OI: resistance ${n(oi?.dbl("res"), 0)} (max CE OI) · support ${n(oi?.dbl("sup"), 0)} (max PE OI)", fontSize = 12.sp, color = Acc)
                if (showLevels) {
                    val px = live?.dbl("ltp") ?: br?.dbl("last")
                    var placed = false
                    br?.optJSONArray("levels").objs().forEach { l ->
                        val lv = l.dbl("value") ?: 0.0
                        if (!placed && px != null && px > lv) {
                            placed = true
                            PriceRow(px)
                        }
                        val above = px != null && lv > px
                        KV(l.optString("name"), n(lv, 0), if (above) Dn else Up)
                    }
                    if (!placed && px != null) PriceRow(px)
                    Text("Paina unnavi = resistance (red), kinda unnavi = support (green)", color = Mut, fontSize = 11.sp)
                }
            }
        }
        item { SectionTitle("LIVE COMMENTARY") }
        val comm = v?.optJSONArray("commentary").objs().reversed()
        if (comm.isEmpty()) item {
            Text("Market time lo (09:15 – 15:30, Kite login tarvata) prati 5 min ki commentary vastundi.", color = Mut, fontSize = 13.sp)
        }
        items(comm) { c ->
            val k = c.optString("kind")
            Row(Modifier.fillMaxWidth().padding(vertical = 2.dp)) {
                Text(c.optString("t"), color = Mut, fontSize = 12.sp, modifier = Modifier.width(44.dp))
                Column(Modifier.weight(1f)
                    .background(if (k == "SIGNAL" || k == "PLAN") Acc.copy(alpha = 0.12f) else Color.Transparent, RoundedCornerShape(6.dp))
                    .padding(horizontal = 6.dp, vertical = 2.dp)) {
                    Text(k, color = kindColor(k), fontSize = 10.sp, fontWeight = FontWeight.Bold)
                    Text(c.optString("text"), fontSize = 14.sp,
                        fontWeight = if (c.optBoolean("important")) FontWeight.SemiBold else FontWeight.Normal,
                        color = if (c.optBoolean("replayed")) Mut else Color.Unspecified)
                    if (c.has("signal_id") && !c.isNull("signal_id")) {
                        TextButton(onClick = { go(Screen.Signal(c.optLong("signal_id"))) }, contentPadding = PaddingValues(0.dp)) { Text("Trade plan open →") }
                    }
                }
            }
        }
        item { SensexBacktestCard(api) }
        item { StrategyLabCard(api) }
        item {
            Text("Idi rules based commentary, 100% guarantee kaadu. Prati trade ki SL compulsory. App orders pettadu - Kite lo nuvve pettali.",
                color = Mut, fontSize = 11.sp)
            Spacer(Modifier.height(24.dp))
        }
    }
}

@Composable
private fun PriceRow(px: Double) {
    Row(Modifier.fillMaxWidth().background(Acc.copy(alpha = 0.18f), RoundedCornerShape(6.dp)).padding(horizontal = 6.dp, vertical = 3.dp)) {
        Text("▶ Ippudu Sensex", fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
        Text(n(px, 0), fontWeight = FontWeight.Bold, color = Acc)
    }
}

@Composable
fun SensexBacktestCard(api: Api) {
    val scope = rememberCoroutineScope()
    var msg by remember { mutableStateOf<String?>(null) }
    val (b, err) = poll(5000) { api.obj("/api/sensex/backtest") }
    val job = b?.optJSONObject("job")
    val running = job?.optBoolean("running") == true
    Card {
        Text("Rules backtest (gata 120 rojulu)", fontWeight = FontWeight.SemiBold)
        ErrorText(err ?: msg ?: job?.str("error"))
        Button(enabled = !running, onClick = { scope.launch { msg = try { api.post("/api/sensex/backtest", JSONObject().put("days", 120)); "Started" } catch (e: Exception) { e.message } } }) {
            Text(if (running) "Running: ${job?.optString("stage")}" else "Run backtest")
        }
        val r = b?.optJSONObject("result") ?: return@Card
        if (!r.optBoolean("ok")) { Text(r.optString("error"), color = Warn); return@Card }
        val s = r.optJSONObject("stats") ?: JSONObject()
        Text("${r.optInt("days")} days · ${r.optString("from")} → ${r.optString("to")} · no-trade days ${r.optInt("no_trade_days")}", color = Mut, fontSize = 12.sp)
        KV2("Trades", "${s.optInt("trades")}", "Win rate", "${n(s.dbl("win_pct"), 1)}%")
        KV2("Avg / trade", "${signed(s.dbl("avg_r"))}R", "Total", "${signed(s.dbl("total_r"))}R", col(s.dbl("avg_r")), col(s.dbl("total_r")))
        KV2("Avg win", "${n(s.dbl("avg_win_r"))}R", "Avg loss", "${n(s.dbl("avg_loss_r"))}R", Up, Dn)
        KV2("Profit factor", n(s.dbl("profit_factor")), "Max drawdown", "${n(s.dbl("max_dd_r"))}R")
        Text("Month wise", fontWeight = FontWeight.Medium, fontSize = 13.sp)
        r.optJSONArray("by_month").objs().forEach { m ->
            KV(m.optString("month"), "${m.optInt("trades")} trades · ${n(m.dbl("win_pct"), 0)}% · ${signed(m.dbl("total_r"))}R", col(m.dbl("total_r")))
        }
        Bullets(r.optJSONArray("notes").strs(), color = Mut)
    }
}

private fun rulesText(r: JSONObject?): String {
    if (r == null) return "—"
    val full = (r.dbl("t1_exit_frac") ?: 0.5) >= 1.0
    return "SL x${n(r.dbl("sl_mult"), 1)} · target ${n(r.dbl("t1_r"), 2)}R" + (if (full) " full exit" else " half, T2 ${n(r.dbl("t2_r"), 1)}R") +
        " · ${r.optInt("max_trades")}/day" + (if (r.optInt("time_stop_min") > 0) " · time stop ${r.optInt("time_stop_min")}m" else "") +
        (if (r.optBoolean("require_pd_break")) " · ninnati H/L break" else "")
}

@Composable
private fun LabRow(title: String, row: JSONObject?, highlight: Color = Color.Unspecified) {
    if (row == null) return
    val a = row.optJSONObject("train") ?: JSONObject()
    val b = row.optJSONObject("test") ?: JSONObject()
    Column(Modifier.padding(vertical = 4.dp)) {
        Text(title, fontWeight = FontWeight.SemiBold, color = highlight, fontSize = 13.sp)
        Text(rulesText(row.optJSONObject("rules")), color = Mut, fontSize = 11.sp)
        KV2("Train win · avg", "${n(a.dbl("win_pct"), 0)}% · ${signed(a.dbl("avg_r"))}R", "Test win · avg", "${n(b.dbl("win_pct"), 0)}% · ${signed(b.dbl("avg_r"))}R",
            col(a.dbl("avg_r")), col(b.dbl("avg_r")))
        Text("trades: train ${a.optInt("trades")}, test ${b.optInt("trades")}", color = Mut, fontSize = 11.sp)
    }
}

@Composable
fun StrategyLabCard(api: Api) {
    val scope = rememberCoroutineScope()
    var msg by remember { mutableStateOf<String?>(null) }
    val (d, err) = poll(5000) { api.obj("/api/sensex/lab") }
    val job = d?.optJSONObject("job")
    val running = job?.optBoolean("running") == true
    Card(border = Acc) {
        Text("Strategy Lab (high win rate + profit)", fontWeight = FontWeight.SemiBold)
        Text("192 rule combinations: first 80 days lo select, last 40 days lo verify. Charges ~0.07R/trade teesesi.", color = Mut, fontSize = 11.sp)
        ErrorText(err ?: msg ?: job?.str("error"))
        Button(enabled = !running, onClick = { scope.launch { msg = try { api.post("/api/sensex/lab", JSONObject()); "Started (2-5 min)" } catch (e: Exception) { e.message } } }) {
            Text(if (running) "Running: ${job?.optString("stage")}" else "Run Strategy Lab")
        }
        Text("Live rules ippudu: " + (d?.optJSONObject("applied_rules")?.let { rulesText(it) } ?: "original"), fontSize = 12.sp)
        val r = d?.optJSONObject("result") ?: return@Card
        if (!r.optBoolean("ok")) { Text(r.optString("error"), color = Warn); return@Card }
        val v = r.optJSONObject("verdict") ?: JSONObject()
        val found = v.optString("label") == "FOUND"
        Pill(v.optString("label"), if (found) Up else Dn)
        Text(v.optString("text"), fontSize = 13.sp)
        Text("Test period from ${r.optString("test_from")}", color = Mut, fontSize = 11.sp)
        LabRow("✅ Pass ayina best rule", r.optJSONObject("winner"), Up)
        LabRow("Ippati rules (original)", r.optJSONObject("current_rules"))
        LabRow("Low SL + High target", r.optJSONObject("low_sl_high_target"))
        LabRow("Highest win rate (profit kaadu choodu)", r.optJSONObject("highest_win_rate"), Warn)
        r.optJSONObject("winner")?.optJSONObject("rules")?.let { w ->
            Button(onClick = { scope.launch { msg = try { api.post("/api/sensex/rules", JSONObject().put("rules", w)); "Applied - repu morning nunchi live" } catch (e: Exception) { e.message } } }) {
                Text("Ee rule live lo pettu")
            }
        }
        if (d.optJSONObject("applied_rules") != null) {
            TextButton(onClick = { scope.launch { msg = try { api.post("/api/sensex/rules", JSONObject().put("rules", JSONObject.NULL)); "Original rules ki marchanu" } catch (e: Exception) { e.message } } }) {
                Text("Original rules ki marchu")
            }
        }
        Text(r.optString("criteria"), color = Mut, fontSize = 11.sp)
    }
}
