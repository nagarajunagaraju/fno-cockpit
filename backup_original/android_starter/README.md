# F&O Predictor Android client (starter)

This is a Kotlin + Jetpack Compose Android client for the local FastAPI backend.

## Open/build
1. Install Android Studio (current stable) and Android SDK Platform 35.
2. Open this `android/` folder in Android Studio and let Gradle sync.
3. Start the backend on your Windows PC using `run_demo.bat` first.
4. Run the Android app on the Android emulator. `10.0.2.2` maps to the host PC.

For a physical phone, change `API_BASE` in `MainActivity.kt` to the Windows PC's LAN IPv4 address, run Uvicorn with `--host 0.0.0.0`, and add authentication/firewall restrictions before exposing the API on a LAN. Do not port-forward this development server to the public internet.

This client includes a local 9:28 AM weekday reminder (inexact Android alarm) and a starter research UI. It is not production-ready: no remote push delivery, secure login, offline cache, exchange-holiday calendar, Play Store signing, or order placement. The reminder asks you to open the app and refresh; it does not claim that a live report has arrived. Build APK from Android Studio after successful Gradle sync. Never place Zerodha API secrets in the Android app.
