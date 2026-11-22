# F&O Cockpit – Android app (source)

Kotlin + Jetpack Compose, no third-party network libraries, no Firebase. Package `in.raj.fnocockpit`
(different from your SMN app, so both install side by side).

Screens: Signals (main: market status, risk strip, active signals with full trade plan, bullish/bearish setups),
Signal detail (contract, entry/SL/targets, Greeks, paper fill, reasoning, strikes compared, invalidation, risks),
Stock evaluation (every check with observed value vs threshold), Scanner, Paper trading, Signal history,
Backtesting, Risk management (edit limits), Data/API health, Settings (server URL, app token, alerts, Kite login).

Background alerts: `AlertService` (foreground service, type specialUse) polls `/api/engine/events` on your
server every 10 s in market hours (09:00–15:40 IST, Mon–Fri) and every 5 min otherwise; restarts after reboot.

## Build
* Android Studio (or command line): `./gradlew assembleRelease` (Windows: `gradlew.bat assembleRelease`).
* Needs Android SDK 35 (`local.properties` -> `sdk.dir=...`).
* Release signing uses `keystore/fnocockpit-release.jks` + `keystore/keystore.properties`.
  Keep these private and backed up: an update must be signed with the same key or Android refuses to install it.
* Output: `app/build/outputs/apk/release/app-release.apk`.
