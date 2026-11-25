package `in`.raj.fnocockpit

import android.content.ContentValues
import android.content.Context
import android.os.Build
import android.provider.MediaStore
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

private val TABS = listOf("F&O ↑", "F&O ↓", "Invest", "Watch", "Paper", "Backtest", "Data")
private val LIST_KEY = mapOf("F&O ↑" to "FNO_BULL", "F&O ↓" to "FNO_BEAR", "Invest" to "INV_BULL")

@Composable
fun SmfHomeCard(rep: JSONObject?, go: (Screen) -> Unit) {
    Card(onClick = { go(Screen.Smf) }) {
        Text("Smart Money (SMF)", fontWeight = FontWeight.SemiBold)
        if (rep == null || !rep.optBoolean("ok")) {
            Text("Evening ~7 PM list (NSE delivery + OI). Tap → Data tab to start the first download.", color = Mut, fontSize = 12.sp)
            return@Card
        }
        val l = rep.optJSONObject("lists") ?: JSONObject()
        Text("List of ${rep.optString("date")} · score ≥ ${n(rep.dbl("min_score"), 1)}", color = Mut, fontSize = 12.sp)
        listOf("FNO_BULL" to "F&O ↑", "FNO_BEAR" to "F&O ↓", "INV_BULL" to "Invest").forEach { (k, label) ->
            val syms = l.optJSONArray(k).objs().take(4).joinToString(", ") { it.optString("symbol") }
            Text("$label: ${syms.ifBlank { "none" }}", fontSize = 13.sp, color = if (k == "FNO_BEAR") Dn else Up)
        }
    }
}

@Composable
fun SmfScreen(api: Api, go: (Screen) -> Unit) {
    var tab by remember { mutableStateOf(TABS[0]) }
    Column(Modifier.fillMaxSize().padding(horizontal = 12.dp)) {
        Title("Smart Money Footprint", "Big money quiet ga konutunna / ammutunna stocks · EOD · research only")
        Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            TABS.forEach { t -> FilterChip(selected = tab == t, onClick = { tab = t }, label = { Text(t) }) }
        }
        Spacer(Modifier.height(6.dp))
        when (tab) {
            in LIST_KEY -> SmfList(api, LIST_KEY.getValue(tab), go)
            "Watch" -> SmfWatch(api, go)
            "Paper" -> SmfPaper(api)
            "Backtest" -> SmfBacktest(api)
            else -> SmfData(api)
        }
    }
}

@Composable
private fun SmfList(api: Api, key: String, go: (Screen) -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var msg by remember { mutableStateOf<String?>(null) }
    val (d, err) = poll(30000) { api.obj("/api/smf/today") }
    val rep = d?.optJSONObject("report")
    LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        item { ErrorText(err ?: msg) }
        if (rep == null || !rep.optBoolean("ok")) {
            item { Card { Text("Inka list ledu. 'Data' tab lo first download start chey. Tarvata prati weekday ~6:30–8 PM ki auto ga vastundi.", color = Mut) } }
            return@LazyColumn
        }
        item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("${rep.optString("date")} · score ≥ ${n(rep.dbl("min_score"), 1)}", color = Mut, fontSize = 12.sp, modifier = Modifier.weight(1f))
                TextButton(onClick = { scope.launch { msg = saveExcel(ctx, api, rep.optString("date")) } }) { Text("Excel ⬇") }
            }
        }
        val rows = rep.optJSONObject("lists")?.optJSONArray(key).objs()
        if (rows.isEmpty()) item { Card { Text("Ee roju ee list lo stock ledu (score ≥ threshold ki evvaru raaledu). No trade kuda correct answer.", color = Mut) } }
        rows.forEach { r -> item { SmfCard(r, key, go) } }
        item { Text("Entry = next day signal-day high/low cross ayithe matrame. SL = base low / 1.5×ATR (daggara unnadi). Qty = 2% risk. Research tool, not advice.", color = Mut, fontSize = 11.sp) }
        item { Spacer(Modifier.height(16.dp)) }
    }
}

@Composable
private fun SmfCard(r: JSONObject, key: String, go: (Screen) -> Unit) {
    val bull = key != "FNO_BEAR"
    val c = if (bull) Up else Dn
    Card(border = c, onClick = { if (r.optBoolean("is_fno")) go(Screen.Stock(r.optString("symbol"))) }) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(r.optString("symbol"), fontWeight = FontWeight.Bold, fontSize = 17.sp, modifier = Modifier.weight(1f))
            Pill(r.optString("phase"), when (r.optString("phase")) { "ACCUMULATION" -> Acc; "MARKUP" -> Up; "DISTRIBUTION", "MARKDOWN" -> Dn; else -> Mut })
            Spacer(Modifier.width(6.dp))
            Pill("${n(r.dbl("score"), 1)}/10", c)
        }
        Text(r.optString("reason"), fontSize = 12.sp)
        KV2("Close", n(r.dbl("close")), "Entry ${if (bull) "above" else "below"}", n(r.dbl("entry")), Color.Unspecified, c)
        KV2("Stop loss", n(r.dbl("sl")), "Risk", "${n(r.dbl("risk_pct"))}%", Dn)
        KV2("Target 1 (1.5R)", n(r.dbl("t1")), "Target 2 (2.5R)", n(r.dbl("t2")), Up, Up)
        KV2("Cash qty (2% risk)", "${r.optInt("cash_qty")} (${rs(r.dbl("cash_value"))})", "Delivery", "${n(r.dbl("deliv_per"), 0)}%")
        if (r.optBoolean("is_fno")) {
            KV2("Lot", "${r.optInt("lot")}", "Futures risk/lot", rs(r.dbl("fut_risk_per_lot")))
            KV2("OI build-up", r.str("oi_buildup") ?: "—", "OI 5 days", "${signed(r.dbl("oi_chg5_pct"), 1)}%")
            r.str("option_hint")?.let { Text("Options: $it", color = Acc, fontSize = 12.sp) }
        }
        Text("${r.optString("sector")} · turnover ₹${n(r.dbl("turnover_cr"), 0)} Cr/day", color = Mut, fontSize = 11.sp)
    }
}

@Composable
private fun SmfWatch(api: Api, go: (Screen) -> Unit) {
    val (d, err) = poll(30000) { api.obj("/api/smf/watch") }
    LazyColumn(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        item { ErrorText(err); Text("ACCUMULATION lo unna stocks ni breakout / fail ayye varaku track chestundi. Breakout roju notification vastundi.", color = Mut, fontSize = 12.sp) }
        d?.optJSONArray("watch").objs().forEach { w ->
            item {
                Card {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(w.optString("symbol"), fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
                        Pill(w.optString("status"), when (w.optString("status")) { "BROKE_OUT" -> Up; "FAILED" -> Dn; "WATCHING" -> Acc; else -> Mut })
                    }
                    Text("Since ${w.optString("start_d")} · base ${n(w.dbl("base_low"))} – ${n(w.dbl("base_high"))} · ${w.optString("list")}", fontSize = 12.sp, color = Mut)
                    w.str("note")?.let { Text(it, fontSize = 12.sp) }
                }
            }
        }
    }
}

@Composable
private fun SmfPaper(api: Api) {
    val (d, err) = poll(30000) { api.obj("/api/smf/paper") }
    LazyColumn(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        item { ErrorText(err); Text("Prati list signal auto ga paper log avutundi. 1 month positive aithe ne real money.", color = Mut, fontSize = 12.sp) }
        if (d == null) return@LazyColumn
        listOf("all_time" to "All time", "last_7_days" to "Last 7 days").forEach { (k, t) ->
            val s = d.optJSONObject(k) ?: JSONObject()
            item {
                Card {
                    Text(t, fontWeight = FontWeight.SemiBold)
                    KV2("Trades", "${s.optInt("trades")}", "Win", "${n(s.dbl("win_pct"), 1)}%")
                    KV2("Avg R", n(s.dbl("avg_r")), "Total R", n(s.dbl("total_r")), col(s.dbl("avg_r")), col(s.dbl("total_r")))
                    if (s.optInt("trades") > 0) KV2("Best", s.optString("best"), "Worst", s.optString("worst"), Up, Dn)
                }
            }
        }
        item {
            Card {
                Text("By list", fontWeight = FontWeight.SemiBold)
                d.optJSONObject("by_list")?.let { bl -> bl.keys().forEach { k -> val s = bl.getJSONObject(k); KV(k, "${s.optInt("trades")} trades · ${n(s.dbl("win_pct"), 0)}% · ${n(s.dbl("avg_r"))}R") } }
                Text("By sector", fontWeight = FontWeight.SemiBold)
                d.optJSONObject("by_sector")?.let { bs -> bs.keys().forEach { k -> val s = bs.getJSONObject(k); KV(k, "${s.optInt("trades")} · ${n(s.dbl("avg_r"))}R") } }
            }
        }
        item { SectionTitle("Open (${d.optJSONArray("open")?.length() ?: 0})") }
        d.optJSONArray("open").objs().forEach { p -> item { KV("${p.optString("symbol")} ${p.optString("side")} ${p.optString("signal_d")}", p.optString("status")) } }
        item { SectionTitle("Recent closed") }
        d.optJSONArray("recent_closed").objs().forEach { p ->
            item { KV("${p.optString("symbol")} ${p.optString("side")} ${p.optString("exit_d")} ${p.optString("note")}", "${signed(p.dbl("r"))}R", col(p.dbl("r"))) }
        }
        item { Spacer(Modifier.height(16.dp)) }
    }
}

@Composable
private fun SmfBacktest(api: Api) {
    val scope = rememberCoroutineScope()
    var msg by remember { mutableStateOf<String?>(null) }
    val (d, err) = poll(5000) { api.obj("/api/smf/backtest") }
    val job = d?.optJSONObject("job")
    val running = job?.optBoolean("running") == true
    LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        item {
            ErrorText(err ?: msg)
            Button(enabled = !running, onClick = { scope.launch { msg = try { api.post("/api/smf/run/backtest", JSONObject()); "Started" } catch (e: Exception) { e.message } } }) {
                Text(if (running) "Running: ${job?.optString("stage")}" else "Run backtest (400 days)")
            }
            job?.str("error")?.let { Text(it, color = Dn, fontSize = 12.sp) }
        }
        val r = d?.optJSONObject("result") ?: return@LazyColumn
        if (!r.optBoolean("ok")) { item { Text(r.optString("error"), color = Warn) }; return@LazyColumn }
        item {
            Card(border = Acc) {
                Text("${r.optInt("days")} days · ${r.optString("from")} → ${r.optString("to")}", fontWeight = FontWeight.SemiBold)
                Text("Test period from ${r.optString("test_from")} (verdict test period nunche)", color = Mut, fontSize = 12.sp)
                Text("Recommended min score: ${r.str("recommended_min_score") ?: "none (no list passed)"}", color = Acc)
            }
        }
        val lists = r.optJSONObject("lists") ?: JSONObject()
        lists.keys().forEach { k ->
            val L = lists.getJSONObject(k)
            val v = L.optJSONObject("verdict") ?: JSONObject()
            val vc = when (v.optString("label")) { "EDGE LOOKS REAL" -> Up; "NO EDGE" -> Dn; else -> Warn }
            item {
                Card(border = vc) {
                    Row { Text(k.replace("_", " "), fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f)); Pill(v.optString("label"), vc) }
                    v.str("text")?.let { Text(it, fontSize = 12.sp) }
                    if (v.has("events")) {
                        KV2("Signals (${v.optString("threshold")})", "${v.optInt("events")}", "10d excess", "${signed(v.dbl("excess_10d"))}%", Color.Unspecified, col(v.dbl("excess_10d")))
                        KV2("10d hit", "${n(v.dbl("hit_10d"), 1)}%", "Baseline hit", "${n(v.dbl("baseline_hit_10d"), 1)}%")
                        KV2("Trade rule avg", "${signed(v.dbl("trade_expectancy_r"))}R", "Months top>mid", "${v.str("months_top_beats_mid_pct") ?: "—"}%", col(v.dbl("trade_expectancy_r")))
                    }
                    Text("Score buckets (TEST, 10-day)", fontWeight = FontWeight.Medium, fontSize = 13.sp)
                    val b = L.optJSONObject("test")?.optJSONObject("buckets_stock_days") ?: JSONObject()
                    listOf("baseline_all", "0-3", "4-5", "6-7", "8-10").forEach { bk ->
                        val s = b.optJSONObject(bk) ?: return@forEach
                        val t = s.optJSONObject("10d")
                        KV(if (bk == "baseline_all") "All (baseline)" else "Score $bk", "n ${s.optInt("n")} · hit ${n(t?.dbl("hit"), 0)}% · avg ${signed(t?.dbl("avg"))}% · excess ${signed(t?.dbl("excess"))}%", col(t?.dbl("excess")))
                    }
                    Text("Trade rule (TEST)", fontWeight = FontWeight.Medium, fontSize = 13.sp)
                    val tr = L.optJSONObject("test")?.optJSONObject("trade_rule") ?: JSONObject()
                    tr.keys().forEach { tk -> val s = tr.getJSONObject(tk); KV("Score $tk", "${s.optInt("trades")} trades · win ${n(s.dbl("win_pct"), 0)}% · ${signed(s.dbl("avg_r"))}R · DD ${n(s.dbl("max_dd_r"))}R", col(s.dbl("avg_r"))) }
                    Text("Weights check (TEST, score 8+)", fontWeight = FontWeight.Medium, fontSize = 13.sp)
                    val sn = L.optJSONObject("sensitivity_test") ?: JSONObject()
                    sn.keys().forEach { sk -> val s = sn.getJSONObject(sk); KV(sk, "n ${s.optInt("events_8plus")} · excess ${signed(s.dbl("excess_10d"))}%", col(s.dbl("excess_10d"))) }
                }
            }
        }
        item { Card { Text("Notes", fontWeight = FontWeight.SemiBold, color = Warn); Bullets(r.optJSONArray("notes").strs()) } }
        item { Spacer(Modifier.height(16.dp)) }
    }
}

@Composable
private fun SmfData(api: Api) {
    val scope = rememberCoroutineScope()
    var msg by remember { mutableStateOf<String?>(null) }
    var minScore by remember { mutableStateOf("") }
    var capital by remember { mutableStateOf("") }
    var tgToken by remember { mutableStateOf("") }
    var tgChat by remember { mutableStateOf("") }
    val (d, err) = poll(5000) { api.obj("/api/smf/status") }
    val data = d?.optJSONObject("data")
    val jobs = d?.optJSONObject("jobs")
    val anyRunning = jobs?.let { j -> j.keys().asSequence().any { j.optJSONObject(it)?.optBoolean("running") == true } } == true
    LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        item { ErrorText(err ?: msg) }
        item {
            Card {
                Text("NSE data", fontWeight = FontWeight.SemiBold)
                KV2("Trading days", "${data?.optInt("trading_days") ?: 0}", "Holidays skipped", "${data?.optInt("holidays_skipped") ?: 0}")
                KV2("From", data?.str("from") ?: "—", "To", data?.str("to") ?: "—")
                KV2("Equity rows", "${data?.optInt("equity_rows") ?: 0}", "F&O rows", "${data?.optInt("fo_rows") ?: 0}")
                KV2("Nifty days", "${data?.optInt("nifty_days") ?: 0}", "Nifty 500 list", "${data?.optInt("n500") ?: 0}")
                Button(enabled = !anyRunning, onClick = { scope.launch { msg = try { api.post("/api/smf/run/all", JSONObject()); "Started: download → list → backtest (first time 30–60 min)" } catch (e: Exception) { e.message } } }) {
                    Text(if (anyRunning) "Running…" else "Download / update + list + backtest")
                }
                Text("First time ~400 days download avutundi (30–60 min). Tarvata prati evening auto ga kotha roju matrame.", color = Mut, fontSize = 11.sp)
            }
        }
        jobs?.keys()?.forEach { k ->
            val j = jobs.optJSONObject(k) ?: return@forEach
            item {
                Card(border = if (j.optBoolean("running")) Acc else if (j.str("error") != null) Dn else Line) {
                    Text("Job: $k", fontWeight = FontWeight.SemiBold)
                    KV2("Stage", j.optString("stage"), "Started", j.optString("started").takeLast(14))
                    j.str("error")?.let { Text(it, color = Dn, fontSize = 12.sp) }
                    j.optJSONObject("result")?.optJSONObject("update")?.let { u ->
                        Text("Downloaded ${u.optJSONArray("fetched")?.length() ?: 0} days · latest ${u.str("latest") ?: "—"} · ${u.optInt("requests")} requests", fontSize = 12.sp)
                        u.optJSONArray("errors").strs().take(5).forEach { Text(it, color = Dn, fontSize = 11.sp) }
                    }
                }
            }
        }
        item {
            Card {
                Text("Last 15 days (rows per date)", fontWeight = FontWeight.SemiBold)
                data?.optJSONArray("recent").objs().forEach { r ->
                    KV("${r.optString("d")} ${r.optString("status")}", "EQ ${r.optInt("equity_rows")} · F&O ${r.optInt("fo_rows")} ${r.optString("note")}",
                        if (r.optString("status") == "OK") Color.Unspecified else Warn)
                }
            }
        }
        item {
            Card {
                Text("Settings", fontWeight = FontWeight.SemiBold)
                val cfg = d?.optJSONObject("config")
                Text("Now: min score ${cfg?.optJSONObject("lists")?.optDouble("min_score")} · capital ₹${cfg?.optJSONObject("trade")?.optDouble("capital")?.toLong()} · risk ${cfg?.optJSONObject("trade")?.optDouble("risk_pct")}%", color = Mut, fontSize = 12.sp)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedTextField(minScore, { minScore = it.take(4) }, label = { Text("Min score") }, singleLine = true, modifier = Modifier.weight(1f),
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal))
                    OutlinedTextField(capital, { capital = it.filter(Char::isDigit).take(9) }, label = { Text("Capital ₹") }, singleLine = true, modifier = Modifier.weight(1f),
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number))
                }
                Button(onClick = { scope.launch {
                    val b = JSONObject()
                    minScore.toDoubleOrNull()?.let { b.put("min_score", it) }
                    capital.toDoubleOrNull()?.let { b.put("capital", it) }
                    msg = try { api.post("/api/smf/settings", b); "Saved (next list uses it)" } catch (e: Exception) { e.message }
                } }) { Text("Save") }
            }
        }
        item {
            Card {
                Text("Telegram (optional)", fontWeight = FontWeight.SemiBold)
                Text(if (d?.optBoolean("telegram_configured") == true) "Connected ✓" else "BotFather lo bot create chesi token + nee chat id ikkada paste chey. Token server lo matrame untundi.", color = Mut, fontSize = 12.sp)
                OutlinedTextField(tgToken, { tgToken = it.trim() }, label = { Text("Bot token") }, singleLine = true, visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth())
                OutlinedTextField(tgChat, { tgChat = it.trim() }, label = { Text("Chat id") }, singleLine = true, modifier = Modifier.fillMaxWidth())
                Button(enabled = tgToken.isNotBlank() && tgChat.isNotBlank(), onClick = { scope.launch {
                    msg = try { api.post("/api/smf/telegram", JSONObject().put("bot_token", tgToken).put("chat_id", tgChat)); tgToken = ""; "Telegram connected: test message pampanu" } catch (e: Exception) { e.message }
                } }) { Text("Connect + test") }
            }
        }
        item { Spacer(Modifier.height(16.dp)) }
    }
}

private suspend fun saveExcel(ctx: Context, api: Api, date: String): String = try {
    val bytes = api.bytes("/api/smf/excel")
    val name = "SMF_$date.xlsx"
    withContext(Dispatchers.IO) {
        if (Build.VERSION.SDK_INT >= 29) {
            val v = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, name)
                put(MediaStore.Downloads.MIME_TYPE, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            }
            val uri = ctx.contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v) ?: error("cannot create file")
            ctx.contentResolver.openOutputStream(uri)!!.use { it.write(bytes) }
        } else {
            java.io.File(ctx.getExternalFilesDir(null), name).writeBytes(bytes)
        }
    }
    "Saved to Downloads: $name"
} catch (e: Exception) { "Excel: ${e.message}" }
