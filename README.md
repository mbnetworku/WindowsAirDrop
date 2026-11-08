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

## Real Apple AirDrop (experimental) - iPhone / iPad / Mac <-> Windows PC
Windows Wi-Fi drivers (incl. Intel) cannot speak AirDrop's radio protocol (AWDL), so AirDrop runs in a
small **Linux VM that owns a USB Wi-Fi adapter**, using the open-source research tools
[OWL](https://github.com/seemoo-lab/owl) and [OpenDrop](https://github.com/seemoo-lab/opendrop).
The Windows app talks to that VM ("bridge") and shows Apple devices with a 🍎 in its device list.

**You need:** a USB Wi-Fi adapter that supports *monitor mode + frame injection* on Linux
(the OWL authors used Atheros AR9280; AR9271-based USB adapters are a common choice), and Bluetooth
available inside the VM (pass through the laptop's Bluetooth or use a USB Bluetooth dongle) for waking iPhones.

1. Install **VirtualBox** + Extension Pack, create a **Debian 12 / Ubuntu** VM, network = **Bridged Adapter**.
2. VM settings -> USB -> add filters for the **USB Wi-Fi adapter** and the **Bluetooth** device.
3. In the VM: `git clone` this repo, then `ip link` to find the adapter name, then
   `sudo ./airdrop-bridge/setup.sh <adapter> 6`  (channel 6, 44 or 149).
4. It prints a **bridge address** and **token** - enter them in the Windows app under *Apple AirDrop*, click Save.
   Status should become "Apple AirDrop: ready".
5. On the iPhone/Mac set AirDrop to **Everyone for 10 minutes**.
   * **Apple -> PC:** Share -> AirDrop -> pick the bridge device. The file appears in `Downloads\WindowsAirDrop`.
   * **PC -> Apple:** select the 🍎 device in the Windows app -> *Send files...* -> accept on the iPhone.

Honest limitations: OWL is archived/unmaintained and OpenDrop is experimental, so newer iOS versions may not
work; contacts-only mode is not supported; one file is sent at a time; files from Apple devices are
accepted automatically; while running, the USB adapter cannot also connect to your Wi-Fi network.
Troubleshooting in the VM: `journalctl -u owl -e`, `journalctl -u airdrop-bridge -e`, `ip link show awdl0`.

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
