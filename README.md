# WindowsAirDrop

AirDrop-style file sharing between a **Windows PC** and **Android phones** on the same Wi-Fi.
Both apps run in the background (PC: system tray; Android: foreground service, also started at boot)
so you can receive files at any time, and send with a couple of taps.

## Get the apps
Push to GitHub and download the artifacts from the **Actions -> Build** run:
* `WindowsAirDrop-android-apk` -> install `app-debug.apk` on the phone (allow "install unknown apps").
* `WindowsAirDrop-windows-exe` -> run `WindowsAirDrop.exe` (no install needed).

Or run from source: `cd windows && pip install -r requirements.txt && python windows_airdrop.py`
(Android: open `android/` in Android Studio.)

## From iPhone / iPad / Mac (Apple "AirDrop" replacement)
True AirDrop cannot work with Windows (Apple-proprietary radio protocol). Instead, open the address shown at
the top of the Windows app (e.g. `http://192.168.1.20:53317`) in Safari on the Apple device, pick files/photos, tap Send.
Tip: Share sheet -> Add to Home Screen for one-tap access later.

## Use
1. Open the app once on both devices (same Wi-Fi). The Windows app minimises to the tray and starts with Windows.
   On first launch, allow it through **Windows Firewall (Private networks)** - needed for TCP 53317 and UDP 53318.
2. **PC -> phone:** pick the phone in the list, click *Send files...*.
3. **Phone -> PC:** tap the PC in the list and choose files, or use the Android **Share** menu -> *Windows AirDrop*.
4. Received files: PC `Downloads\WindowsAirDrop`, phone `Downloads/WindowsAirDrop`.

Optional: set the same **PIN** on both devices so strangers on the network cannot send you files.
Transfers are plain HTTP on the local network (not encrypted) - use trusted networks only.
On some phones, disable battery optimisation for the app so Android keeps it alive.

See [PROTOCOL.md](PROTOCOL.md). Note: this is not Apple's AirDrop and cannot talk to iPhones/Macs.
