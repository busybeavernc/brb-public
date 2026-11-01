<p align="center"><img src="assets/logo.png" alt="Doppleganger — A seamless switch." width="420"></p>

**Doppleganger** records a short, seamless loop of you, then lets you switch between that loop and your live webcam in Zoom, Teams or any browser meeting — without anyone noticing. Windows 10/11 and macOS 12+.


**Download: [doppleganger.happyhacks.io](https://doppleganger.happyhacks.io/)** (picks the right file for your Windows PC or Mac), or grab a file from the [latest release](https://github.com/busybeavernc/doppleganger-public/releases/latest).


## Install

1. Download **DopplegangerSetup.exe** from the [latest release](https://github.com/busybeavernc/doppleganger-public/releases/latest) and run it. No admin rights needed. It adds a Start-menu entry, an optional desktop shortcut, and an uninstall entry in *Settings → Apps*.
2. Doppleganger opens with a short **setup guide**:
    1. **Add the Doppleganger camera.** Windows asks for permission once.
    2. **Choose your webcam.**
    3. **Record your first loop.**
    4. **Pick "Doppleganger" as your camera** in Zoom/Teams.

You can reopen the guide any time from the *Setup guide* button. OBS Studio is not needed.

Your loops and settings live in `%APPDATA%\Doppleganger` and are kept if you uninstall. A copy run from its own folder that already contains `config.json` or `loops\` (as earlier versions did) keeps using that folder.

### Mac
1. Download the Mac disk image from the [latest release](https://github.com/busybeavernc/doppleganger-public/releases/latest): **Doppleganger-mac-arm64.dmg** for Apple Silicon (M1 or newer) or **Doppleganger-mac-x86_64.dmg** for Intel Macs. Open it and drag **Doppleganger** onto **Applications**.
2. The first time, macOS says Doppleganger *"can't be opened because Apple cannot check it for malicious software"*, because the app isn't notarized by Apple yet. Click **OK**, go to *System Settings → Privacy & Security*, scroll down and click **Open Anyway**, then open it again and choose **Open**. On macOS 14 or older, **right-click → Open** also works. A Terminal fallback: `xattr -dr com.apple.quarantine /Applications/Doppleganger.app`.
3. Allow the **camera** when macOS asks.
4. **OBS Studio is needed on a Mac.** Doppleganger sends your picture through the free OBS Virtual Camera, because a Mac camera driver of our own needs a paid Apple developer certificate. The setup guide checks whether OBS and its camera are installed. If they aren't, it has a **Download OBS Studio** button ([obsproject.com/download](https://obsproject.com/download)) and walks you through the one-time steps:
    1. Install OBS Studio 30 or newer.
    2. Open OBS, click **Start Virtual Camera**, and allow the camera extension in *System Settings → General → Login Items & Extensions → Camera Extensions*.
    3. Click **Stop Virtual Camera** and quit OBS. You don't need to open it again.
5. In Zoom, Teams or Meet, choose **OBS Virtual Camera**. Keep OBS's own virtual camera stopped while Doppleganger is running.
6. **Allow Doppleganger on your Mac.** The setup guide has a page for this. It shows a ✓ for each permission once it's on, and its buttons open the right Settings page:
    - **Camera** (*Privacy & Security → Camera*): turn on Doppleganger. macOS usually asks the first time it starts.
    - **Input Monitoring** (*Privacy & Security → Input Monitoring*): this lets the hotkeys work while Zoom is in front. Turn on Doppleganger; if it isn't listed, click **+** and pick it from Applications. When macOS offers **Quit & Reopen**, accept, or use the guide's **Reopen Doppleganger** button. On most Mac keyboards the hotkeys are **fn+F9** and **fn+F10**.
    - **OBS camera extension** (*General → Login Items & Extensions → Camera Extensions*): from step 4.

   Doppleganger isn't signed by Apple yet, so macOS can treat an update as a new app and ask again. If the hotkeys stop working after an update, switch Input Monitoring off and on for Doppleganger.

On a Mac, the microphone is silenced by setting the input volume to 0 while the LOOP is on air, then setting it back. Loops and settings live in `~/Library/Application Support/Doppleganger`. Updates install themselves the same way as on Windows, as long as Doppleganger runs from *Applications* and not from the disk image. Hiding from screen sharing uses macOS's "don't share this window" setting; some recent screen-capture methods may still show the window.

### Text size
On a Mac, text follows the system's standard size. *Settings → Text size* (or **Ctrl/Cmd +** and **Ctrl/Cmd −**, with **Ctrl/Cmd 0** to reset) makes it smaller or larger in every window, from 85% to 175%.

### Starting up
Loading the AI model and opening the webcam takes a few seconds, so a splash screen with the logo shows what's happening until the window opens.

### Updates
Each time it opens, Doppleganger checks the releases. If there's a newer version, it shows an "Updating…" window, swaps itself out and reopens. *Settings → Updates → Check now* checks manually. An update never interrupts you while the LOOP is on air or a recording is running.

### Code signing (self-signed)
Release files are signed with a self-signed "Doppleganger" certificate (`doppleganger-publisher.cer` is attached to each release). Windows only trusts it on PCs that choose to: tick *Trust the Doppleganger publisher on this PC* in the installer, and Windows asks you to confirm. Other PCs still see the SmartScreen "unknown publisher" prompt (**More info → Run anyway**). A commercial code-signing certificate would remove that prompt everywhere; the build already supports one, so just swap the secrets.

## The window

| Area | What it does |
|---|---|
| **LIVE / LOOP** | What Zoom sees right now. LIVE is Aqua, LOOP is Violet; GLITCH appears in amber while a fake bad connection is running. |
| **Sync** | How seamless a cut would be right now (Aqua = in sync). |
| **Cut button** | **Tap:** cut immediately (hard cut). **Hold:** Doppleganger waits and cuts by itself the moment you're in sync; let go first to cancel. The button wears the colour of where you're cutting *to*. |
| **Glitch / Record loop** | See below. |
| **Webcam** | Pick which camera is your live feed (virtual cameras are never offered). |
| **Loops** | Your loop library: thumbnails, names (*Rename*), *Use*, *Delete*. A ⚠ appears when a loop's lighting doesn't match your room right now (darker, brighter, different colour). |
| **Mini bar** | A compact always-on-top strip with a live thumbnail, sync meter, Cut and Glitch, to park beside Zoom. Drag it by the logo; *Full view* brings the window back. |
| **Settings** | Privacy, crossfade, guides, overlay strength, the Doppleganger camera, updates. |

**Hidden from screen sharing:** Doppleganger's windows (main, mini bar, setup, settings) are excluded from screen capture. When you share your screen, viewers see what's behind them. This also hides them from your own screenshots. Turn it off in *Settings → Privacy*.

**Silence while on the LOOP:** loops never contain sound, since Doppleganger only handles video. While the LOOP is on air, Doppleganger also **mutes your microphone** at the Windows level, so nothing you say or no background noise goes out, and unmutes it the moment you cut back to live. It only touches microphones that were unmuted, and if the app is ever closed unexpectedly while muted, your mic is restored the next time it starts. Turn it off in *Settings → Privacy*.

## Hotkeys (work while Zoom is focused)

| Key | Action |
|---|---|
| **F9** | **Tap** = cut now. **Hold** = cut when in sync |
| **F10** | Fake a bad connection (glitch) |
| Space | Same as F9 (tap/hold), while the Doppleganger window is focused |
| R | Record a loop; press again (or Esc) to **cancel** a recording in progress. Nothing is saved |
| B / K / C | Glitch / next webcam / calibrate the room (optional) |
| E | End the recording now (final stretch) |
| O | Show or hide the live (aqua) and loop (violet) **outlines** |
| G / M | Show or hide guides / switch to the mini bar |

The tray icon (by the clock) has the same commands, plus a webcam picker and Install/Remove camera.

## Finding you in the picture (no setup)

Doppleganger uses **AI person detection** (Google's MediaPipe selfie segmentation, the kind of model video-call apps use for background blur) to find you in every frame. It needs no setup, works in any room and with any webcam, and copes with lighting changes. The mask is snapped to real edges in the picture (guided filter, plus GrabCut for the start-pose silhouette), so hair and shoulders get a clean outline.

*Settings → Calibrate room* is still there as an optional fallback, and is used automatically only if the AI model can't run on a PC. Lean or step slowly to the side the banner asks for while it learns what the empty room looks like.

## Recording a loop

When you press **Record** (button, **R** or the tray menu), a short *Before you record* card lists what to expect first:
- Sit the way you will in the meeting.
- Hold still for the first 10 seconds.
- Keep movement subtle and natural: blink, breathe, nod slightly, glance at your screen.
- Avoid anything memorable, like big laughs, waves, touching your face or drinking. The loop repeats, so a distinctive moment gets noticed the second time.
- Come back to the start pose for the last 10 seconds.
- Keep the room the same.

Press **Start recording** (or **R** again) to begin. You can turn the card off with *Don't show this again* or in Settings. While a new loop is being stitched, a small progress window appears and cuts are paused for a moment.

| Time (default 90 s) | What you'll see | What to do |
|---|---|---|
| 3 s countdown | "Get in your start pose" | Settle into a comfortable, centered position |
| 0–10 s | **HOLD STILL** + movement meter | No big head or body movement; this becomes the loop's first frame and your silhouette |
| 10–80 s | "Recording – move naturally" | Look at the screen, nod, blink, look like you're listening |
| 80–90 s | "Step back into your silhouette" + first-frame thumbnail + **Match %** | Line up with the silhouette and copy your starting expression; it **glows aqua** when you're back. Hold still |

The app finds the frame in the final seconds that best matches the first, ends the loop there, and adds a few motion-matched bridge frames if needed, so the restart is invisible. The loop starts at its first frame the first time you cut to it, then keeps playing in the background, so later cuts pick it up wherever it is.

## Before you cut

- **Before the first cut**, the loop's start pose shows as a soft violet shape with your aqua outline on top (with outlines on). Line them up ("In start pose – cut is seamless"), then cut, or just *hold* F9 and let Doppleganger cut when you're there. The white silhouette appears only while recording.
- **After that**, the preview shows the off-air feed faded over the on-air one: the loop while you're LIVE, your camera while the LOOP is on air. Line up until the aqua border appears, or hold F9.
- **Outlines** (O, or the checkbox under the sync meter): an aqua line traces you live and a violet line traces the loop, or the frozen picture during a glitch. When the two lines sit on top of each other, the cut is seamless.

## Called on unexpectedly? Fake a bad connection

Press **F10**. Zoom's picture turns jerky and random: frozen frames, uneven bursts, and the occasional sharp in-between frame. It never goes blurry or blocky and never drops out. Your preview keeps the overlay, so you can line up with the frozen picture. Then press (or hold) **F9**: the next jump reveals you live, with a few catch-up steps before it runs smoothly. F10 again also ends it, and it stops by itself after 30 s.

## The Doppleganger camera

A small DirectShow camera filter (64- and 32-bit) installed in `C:\ProgramData\Doppleganger`, built from [softcam](https://github.com/tshino/softcam) (MIT) with its own name and ID, so it never clashes with OBS or other virtual cameras.

Zoom can select it any time. Whenever the Doppleganger app isn't running (not started yet, or quit), the camera shows a neutral dark **"Camera starting…"** picture. It's deliberately generic, because meeting participants can see it, and it's never a frozen frame of you. To make sure the camera always has a real picture, turn on *Settings → Start Doppleganger when Windows starts*; it opens minimized. If it isn't installed, Doppleganger falls back to OBS Virtual Camera (OBS must be installed and closed).

## Settings file

`config.json` in the data folder. Useful fields: `camera_name`, `output_camera` (`auto`/`doppleganger`/`obs`), `width`/`height`/`fps`, `loop_seconds`, `hold_still_seconds`, `return_prompt_seconds`, `crossfade_ms`, `overlay_opacity`, `hotkey`, `glitch_hotkey` (pynput format, e.g. `"<ctrl>+<alt>+l"`), `glitch_max_seconds`, `mute_mic_on_loop`, `hide_from_screen_share`, `auto_update`.
