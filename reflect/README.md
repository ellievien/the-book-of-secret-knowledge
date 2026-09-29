# Reflect

Reflect shows the Claude desktop app from your Windows PC or Mac on your iPhone,
and lets you tap, scroll and type on it. The computer shrinks Claude's window
to phone shape, so it looks like a phone app. Everything stays on your Wi-Fi:
no cloud and no router setup.

Two parts:

* `helper/`: a small program on the computer (Windows or Mac)
* `ios/`: the iPhone app

## 1. Start Reflect on Windows

1. Open the Claude desktop app.
2. In File Explorer, open the `reflect\helper` folder and double-click **Reflect.cmd**.
   (Or in PowerShell: `powershell -ExecutionPolicy Bypass -File .\run.ps1`)
3. The first run installs Python 3.11 and everything else it needs by itself (a few minutes).
4. If Windows asks whether to allow Python on your network, click **Allow** (private networks).
5. A black console window shows a **pairing code**. A Reflect icon (a small phone) also appears in the
   system tray next to the clock. Keep the console window open while you use Reflect.

The tray icon menu has **Start/Stop**, **Phone mode** and **Quit**. Quitting
puts Claude's window back to its normal size.

## 2. Start Reflect on a Mac

1. Open the Claude desktop app.
2. In Terminal: `cd reflect/helper` then `bash run.sh`
3. The first run installs Python 3.11 and the dependencies.
4. macOS asks for two permissions for **Terminal**: **Screen & System Audio Recording** (to see
   Claude's window) and **Accessibility** (to click and type). Turn both on in
   System Settings → Privacy & Security, then run `bash run.sh` again.
5. The pairing code appears in Terminal, and the Reflect icon appears in the menu bar.

Over SSH, run `bash run.sh --capture-test capture.png` to save one picture of
Claude's window. Over SSH the permission belongs to `sshd-keygen-wrapper`,
not Terminal: add `/usr/libexec/sshd-keygen-wrapper` under Screen & System Audio Recording once.

## 3. Install the iPhone app (on the Mac)

You need Xcode on the Mac and your Apple ID signed in to Xcode once
(Xcode → Settings → Accounts → **+**). A free Apple ID is enough.

1. Plug the iPhone into the Mac with a cable, unlock it and tap **Trust**.
2. On the Mac (Terminal or SSH): `cd reflect/ios` then `bash build.sh`
3. The first time only, on the iPhone:
   * Settings → Privacy & Security → **Developer Mode** → On (the iPhone restarts).
   * Settings → General → VPN & Device Management → your Apple ID → **Trust**.
4. Open **Reflect** on the iPhone.

With a free Apple ID the app works for 7 days. When it stops opening, run `bash build.sh` again.
To rebuild after changes, also just run `bash build.sh`.

## 4. Pair the phone (first time only)

1. Make sure the iPhone is on the same Wi-Fi as the computer.
2. Open Reflect on the iPhone and allow **Local Network** access when asked.
3. Tap your computer in the list and type the 6-digit code shown on the computer.

From then on the app connects by itself when it opens. To pair another
phone, choose **Pair a new phone** in the Reflect tray/menu-bar menu for a new code.

## Using it

* **Tap** = click. **Press and hold** = right click. **Drag with one finger** = scroll.
* **Pinch** or drag with **two fingers** zooms and moves the picture on the phone only.
* Bottom bar: **Keyboard** (type, then **Send**; the **return** key also presses Enter),
  **Esc**, **Paste** (types the iPhone clipboard), **Phone mode** on/off, **Disconnect**.
* The corner badge shows frames per second and delay.

## If something is wrong

| You see | Do this |
|---|---|
| "Claude desktop not running" | Open the Claude app on the computer. |
| The computer isn't in the list | Same Wi-Fi? Reflect running? On the phone, tap **Connect by address…** and enter the address printed in the console. |
| "Local Network access" message | iPhone Settings → Privacy & Security → Local Network → turn on Reflect. |
| Black or empty picture on a Mac | Allow Screen & System Audio Recording (see step 2) and restart Reflect. |
| Taps do nothing on a Mac | Allow Accessibility (see step 2) and restart Reflect. |
| Pairing refused | Choose **Pair a new phone** in the Reflect menu and use the new code. |

Check that the computer side works: `.\run.ps1 -CaptureTest` (Windows) or
`bash run.sh --capture-test capture.png` (Mac) saves one picture of Claude's window.

How the phone and computer talk is described in [PROTOCOL.md](PROTOCOL.md).
