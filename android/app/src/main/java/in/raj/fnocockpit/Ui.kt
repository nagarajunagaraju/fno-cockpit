package `in`.raj.fnocockpit

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import org.json.JSONArray
import org.json.JSONObject
import java.text.NumberFormat
import java.util.Locale

val Up = Color(0xFF22C55E)
val Dn = Color(0xFFEF4444)
val Warn = Color(0xFFF59E0B)
val Acc = Color(0xFF60A5FA)
val Mut = Color(0xFF8B93A3)
val CardBg = Color(0xFF171A21)
val Line = Color(0xFF2A2F3A)

val AppColors = darkColorScheme(
    primary = Acc, background = Color(0xFF0F1115), surface = CardBg, onSurface = Color(0xFFE6E8EC),
    onBackground = Color(0xFFE6E8EC), surfaceVariant = Color(0xFF1F2430), onSurfaceVariant = Mut,
)

private val inr = NumberFormat.getNumberInstance(Locale("en", "IN"))

fun JSONObject.dbl(k: String): Double? = if (!has(k) || isNull(k)) null else optDouble(k).takeIf { !it.isNaN() }
fun JSONObject.str(k: String): String? = if (!has(k) || isNull(k)) null else optString(k)
fun JSONArray?.objs(): List<JSONObject> = if (this == null) emptyList() else (0 until length()).mapNotNull { optJSONObject(it) }
fun JSONArray?.strs(): List<String> = if (this == null) emptyList() else (0 until length()).map { optString(it) }

fun n(x: Double?, d: Int = 2): String = if (x == null) "—" else {
    inr.maximumFractionDigits = d; inr.minimumFractionDigits = 0; inr.format(x)
}
fun rs(x: Double?): String = if (x == null) "—" else "₹" + n(x, 0)
fun signed(x: Double?, d: Int = 2): String = if (x == null) "—" else (if (x > 0) "+" else "") + n(x, d)
fun col(x: Double?): Color = when { x == null -> Mut; x > 0 -> Up; x < 0 -> Dn; else -> Mut }

@Composable
fun Card(modifier: Modifier = Modifier, border: Color = Line, onClick: (() -> Unit)? = null, content: @Composable ColumnScope.() -> Unit) {
    Column(
        modifier
            .fillMaxWidth()
            .background(CardBg, RoundedCornerShape(12.dp))
            .border(1.dp, border, RoundedCornerShape(12.dp))
            .let { if (onClick != null) it.clickable { onClick() } else it }
            .padding(12.dp),
        verticalArrangement = Arrangement.spacedBy(4.dp), content = content
    )
}

@Composable
fun Pill(text: String, fg: Color = Mut, bg: Color = Color(0xFF1F2430)) {
    Text(text, color = fg, fontSize = 11.sp, modifier = Modifier.background(bg, RoundedCornerShape(50)).padding(horizontal = 8.dp, vertical = 2.dp))
}

@Composable
fun SectionTitle(t: String) {
    Text(t.uppercase(), color = Mut, fontSize = 12.sp, fontWeight = FontWeight.SemiBold, modifier = Modifier.padding(top = 10.dp, bottom = 2.dp))
}

@Composable
fun KV(k: String, v: String, vc: Color = Color.Unspecified, modifier: Modifier = Modifier) {
    Row(modifier.fillMaxWidth().padding(vertical = 1.dp), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(k, color = Mut, fontSize = 13.sp)
        Text(v, color = vc, fontSize = 13.sp, fontWeight = FontWeight.Medium)
    }
}

@Composable
fun KV2(k1: String, v1: String, k2: String, v2: String, c1: Color = Color.Unspecified, c2: Color = Color.Unspecified) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        KV(k1, v1, c1, Modifier.weight(1f)); KV(k2, v2, c2, Modifier.weight(1f))
    }
}

@Composable
fun Bullets(items: List<String>, color: Color = Color.Unspecified, mark: String = "•") {
    items.forEach { Text("$mark $it", fontSize = 13.sp, color = color, modifier = Modifier.padding(start = 4.dp)) }
}

@Composable
fun ErrorText(e: String?) {
    if (!e.isNullOrBlank()) Text(e, color = Dn, fontSize = 13.sp)
}

fun statusColor(s: String?): Color = when (s) {
    "TRIGGERED", "TRIGGER" -> Acc
    "ACTIVE", "READY" -> Up
    "TARGET_HIT" -> Up
    "STOP_LOSS_HIT", "INVALIDATED", "REJECTED" -> Dn
    "EXPIRED", "WATCHING" -> Warn
    else -> Mut
}

/** The full trade-plan card used on the main screen and in history. */
@Composable
fun SignalCard(s: JSONObject, onClick: () -> Unit) {
    val bull = s.optString("bias") == "BULL"
    Card(border = if (bull) Up.copy(alpha = 0.6f) else Dn.copy(alpha = 0.6f), onClick = onClick) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(s.optString("symbol"), fontWeight = FontWeight.Bold, fontSize = 16.sp, modifier = Modifier.weight(1f))
            val g = s.optString("confidence")
            if (g == "A+" || g == "A" || g == "B") Pill(g, if (g == "B") Warn else Up)
            Spacer(Modifier.width(6.dp))
            Pill("${s.optString("status")} · ${n(s.dbl("score"), 0)}", statusColor(s.optString("status")))
        }
        Text("${if (bull) "BUY CE" else "BUY PE"} · ${s.optString("tradingsymbol")} · Exp ${s.optString("expiry")}",
            color = if (bull) Up else Dn, fontSize = 13.sp)
        val er = s.optJSONArray("entry_range")
        KV2("Entry", "${n(er?.optDouble(0))}–${n(er?.optDouble(1))}", "SL", n(s.dbl("sl_premium")), c2 = Dn)
        KV2("T1", n(s.dbl("t1_premium")), "T2", n(s.dbl("t2_premium")), Up, Up)
        KV2("Lots / Qty", "${s.optInt("lots")} / ${s.optInt("qty")}", "Max loss", rs(s.dbl("max_loss")))
        KV2("R:R (T1)", n(s.dbl("rr_t1")), "Confidence", s.optString("confidence"))
        if (s.dbl("fill_price") != null) {
            KV2("Paper fill", n(s.dbl("fill_price")), "Net P&L", rs(s.dbl("net_pnl")), c2 = col(s.dbl("net_pnl")))
        }
        s.str("outcome")?.let { Text(it, color = Mut, fontSize = 12.sp) }
        Text("Signal ${s.optString("created_at").drop(11).take(5)} · tap for full reasoning", color = Acc, fontSize = 12.sp)
    }
}

/** A watching / ready setup row from the scanner. */
@Composable
fun SetupRow(x: JSONObject, onClick: () -> Unit) {
    val m = x.optJSONObject("metrics") ?: JSONObject()
    Card(onClick = onClick) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(x.optString("symbol"), fontWeight = FontWeight.SemiBold)
                Text(x.optString("sector"), color = Mut, fontSize = 11.sp)
            }
            Column(horizontalAlignment = Alignment.End) {
                Text("${signed(m.dbl("chg_pct"))}%", color = col(m.dbl("chg_pct")), fontWeight = FontWeight.SemiBold)
                val stage = when (x.optString("stage")) { "STAGE2" -> "Stage 2 · pullback"; "STAGE1" -> "Stage 1 · strong"; "STAGE3" -> "Trigger"; else -> x.optString("status") }
                Pill("$stage · ${n(x.dbl("score"), 0)}", statusColor(if (x.optString("stage") == "STAGE2") "READY" else x.optString("status")))
            }
        }
        Text("RVOL ${n(m.dbl("rvol"))}x · ${m.str("buildup") ?: "OI n/a"} · VWAP ${n(m.dbl("vwap"))}", color = Mut, fontSize = 12.sp)
        val note = x.str("wait_reason") ?: x.optJSONArray("missing").strs().firstOrNull() ?: x.optJSONArray("reject_reasons").strs().firstOrNull()
        note?.let { Text(it, color = Mut, fontSize = 12.sp) }
    }
}
