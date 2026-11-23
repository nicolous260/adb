# ADB File Transfer

Small Windows window app to send files and folders from your PC to an Android phone using `adb push`.

- Run from source: `python adb_transfer.py` (Python 3.8+, tkinter included)
- Needs `adb` (Android platform-tools) on PATH, or browse to `adb.exe` in the app
- Phone: enable USB debugging in Developer options
- Optional for running from source: `pip install qrcode` (the .exe already includes it)

## Connect over Wi-Fi with a QR code

Click **Scan QR (Wi-Fi)**, then on the phone (Android 11+) go to Settings > Developer options > Wireless debugging > *Pair device with QR code* and scan the code. The app pairs and connects automatically. Phone and PC must be on the same Wi-Fi.

## Get the .exe

Open the **Actions** tab, pick the latest "Build Windows exe" run, and download the `ADBFileTransfer` artifact.
Pushing a tag like `v1.0` also attaches the exe to a GitHub Release.
