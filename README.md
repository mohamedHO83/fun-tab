# Fun Tab

A GTA-style radial **Alt+Tab** for Windows. Hold Alt, tap Tab, flick the mouse at the app you want, let go of Alt.

It replaces the Windows switcher while it runs, and it is built to be faster than the thing it replaces: a warm open paints in about **12 ms**, and transitions run at the full refresh rate of the monitor.

```bat
pip install -r requirements.txt
run.bat
```

A tray icon appears. Quit from there when you're done.

## Controls

Everything below works while Alt is held. Release Alt to switch.

| Input | Action |
|---|---|
| **Alt+Tab** | Open the wheel with the previous window selected |
| **Alt+Shift+Tab** | Open going backwards (least recently used first) |
| **Ctrl+Alt+Tab** | Open in sticky mode: the wheel stays up after Alt is released |
| **Alt+`** | Open showing only the current app's windows |
| **Flick the mouse** | Aim by direction from wherever the cursor already is - no need to drag it to the wheel first |
| **Point at a slice** | With the cursor actually on the ring, it picks whatever it's over |
| **Needle in the hub** | Shows the direction you're aiming, whenever the mouse has control |
| **Tab / Shift+Tab** | Next / previous |
| **Arrows** | Next / previous |
| **Mouse wheel** | Next / previous |
| **1**-**9**, **0** | Jump straight to that slice and switch immediately |
| **Type any letters** | Filter the wheel by window title, app name or executable |
| **Backspace** | Edit the filter |
| **Home / End** | First / last window |
| **`** | Cycle between windows of the selected app |
| **Enter**, **Space**, **left click** | Switch to the selection (a click anywhere counts) |
| **Delete**, **Ctrl+W**, **Ctrl+Q** | Close the selected window, keep the wheel open |
| **Ctrl+M** | Minimise the selected window |
| **Esc** | Clear the filter, or cancel if there is no filter |

Windows are ordered most-recently-used first, so Alt+Tab always lands on the app you came from and Alt+Tab+Tab lands on the one before it.

### Mouse and keyboard don't fight

Aiming is measured from wherever the cursor was when it last had control, not from the middle of the screen, so a flick works the same with the pointer parked in a corner. Measuring from the wheel's centre instead meant the corner your pointer happened to be in had already chosen a slice for you, and reaching the others meant dragging the cursor across the display.

The pointer also only has control while it's actually moving. Anything discrete - Tab, an arrow, a digit, the scroll wheel, typing a filter - takes the wheel back and re-anchors on the cursor, so a stationary mouse can't undo your keypress and a knock of the desk can't either. Move it deliberately and it takes over again.

Because the aim comes from a direction rather than a position, a **needle** in the hub shows where you're pointing. It only appears while the pointer has control, so it never disagrees with the highlight, and it sits in the gap between the hub and the icons - a full-length ray would strike through the icon of the very slice you're choosing. Repainting it writes only that band back into the layered surface, so tracking the cursor costs **1.9 ms** a frame against 0.9 ms for sitting still; re-packing the whole canvas for it would cost 7 ms.

## What makes it fast

Idle cost is genuinely zero: a low-level keyboard hook wakes the process on key events, the overlay windows stay hidden, and the message loop sleeps on a 1-second timeout until the wheel is open.

When you do open it, the work has mostly already been done:

- **Everything paintable is cached and content-keyed.** Wedges, the hub, labels, icon sizes and whole composed frames are all keyed by what's in them, so cycling between two apps repaints nothing at all - a settled frame is served from cache and only handed to the compositor.
- **Startup prewarms the caches** on the UI thread: icons, executable names, the ring plate and each slice's highlight are built before your first Alt+Tab, not during it.
- **The backdrop blur starts when you press Alt, not when you press Tab.** Reading the framebuffer back costs ~32 ms at 1080p however small a plate it is scaled into - it's the screen-DC access, not the pixel count - which is more than everything else an open does put together. Alt always lands before Tab, so the capture runs on a worker thread during that gap and the open finds a finished plate: **0.0 ms** on the critical path. It's then blurred at 1/6 resolution and stretched back up by the driver, which costs about 2 ms rather than the 30-odd a screen-sized layered upload would.
- **The open animation is free.** Fading and rising are both parameters of `UpdateLayeredWindow`, so the animation never touches a pixel - it re-presents the same bitmap at a new offset and opacity.
- **One persistent DIB per window.** The old renderer allocated a DC, a bitmap and a `bytes` copy every frame; each window now keeps one DIB section alive and frames are packed straight into it in a single pass (Pillow's `BGRa` packer does the premultiply and the channel swap together).
- **Window previews are captured on a background thread**, newest request first, and reused for a few seconds before being refreshed.

Render costs from `bench.py`, best of three runs on a 1080p laptop:

| | 5 windows | 20 windows |
|---|---|---|
| First open after the startup prewarm | 1.5 ms | 1.1 ms |
| Re-open, same window set | 0.00 ms | 0.01 ms |
| Re-open, order changed | 1.3 ms | 0.7 ms |
| Transition frame | 0.8 ms | 0.8 ms |
| Held frame | 0.00 ms | 0.3 ms |
| Cold start, nothing cached at all | 19 ms | 20 ms |

End to end against real windows at 120 dpi (`smoke.py`), which adds the Win32 calls and the DIB uploads the table above leaves out: first open **~19 ms**, re-open with hot caches **~14 ms**, held frame **1.2 ms**. Resolving the backdrop at open is **0.0 ms**, because the 35 ms capture already ran while Alt was held.

Take the absolute values with a pinch of salt - this laptop's clocks swing by 2-3x depending on thermal state, so only same-run comparisons mean much.

## Settings

Settings live in `%APPDATA%\fun-tab\config.json`, written with defaults on first run. Edit it from the tray menu, then **Reload settings** - no restart. Unknown keys are ignored and out-of-range values are clamped, so a bad edit can't stop it from starting.

| Key | Default | Notes |
|---|---|---|
| `theme` | `"auto"` | `auto` follows the Windows app theme; or `dark` / `light` |
| `accent` | `"system"` | Follows your Windows accent colour, or set `"#RRGGBB"` |
| `scale` | `1.0` | Extra size multiplier on top of monitor DPI |
| `outer_radius`, `inner_radius` | `165`, `52` | Ring geometry, in 96-dpi pixels |
| `icon_scale` | `1.0` | Icon size within each slice |
| `backdrop` | `"blur"` | `blur` (frozen, blurred desktop), `dim` (frozen and darkened, not blurred), or `none` |
| `appear_duration` | `0.11` | Seconds for the open animation |
| `transition_duration` | `0.13` | Seconds for the selection crossfade |
| `max_fps` | `144` | Frame cap while the wheel is open |
| `dim_blur`, `dim_veil`, `dim_scale` | `1.0`, `40`, `6` | Backdrop softness, darkening, and capture resolution divisor |
| `backdrop_ttl` | `2.0` | Seconds a captured backdrop is reused before it's grabbed again |
| `preview_enabled` | `true` | The live preview card |
| `preview_position` | `"top-left"` | `top`/`bottom` + `left`/`right`/`center` |
| `preview_width`, `preview_height`, `preview_margin` | `400`, `225`, `48` | Card size and screen inset |
| `prefetch_previews` | `true` | Capture the other windows in the background too |
| `capture_minimized` | `true` | Restore minimised windows off-screen to preview them |
| `thumb_ttl` | `4.0` | Seconds before a captured preview is refreshed |
| `show_counter`, `show_subtitle`, `show_hints` | `true` | Hub text: `n/N`, app name, key hints |
| `mru_order` | `true` | Most-recently-used ordering |
| `search_enabled`, `digit_jump`, `close_key_enabled` | `true` | Turn off the typing, number and close bindings |
| `wrap_navigation` | `true` | Cycling past the end wraps around |
| `minimized_last` | `false` | Push minimised windows to the end of the wheel |
| `exclude_exes` | `[]` | e.g. `["teams.exe"]` - never show these |
| `exclude_titles` | `[]` | Substring match on window titles |
| `colors` | `{}` | Override any palette key, e.g. `{"accent": "#ff8800", "text": "#ffffff"}` |

## Tray menu

- **Start with Windows** - adds a `pythonw` launcher to the per-user Run key
- **Edit settings** - opens `config.json`
- **Reload settings** - applies it without restarting
- **Quit**

## Development

```bat
python -m pytest            # 228 tests, no display needed
python bench.py             # render timings, cold and warm
python smoke.py             # creates real layered windows and times a live open
python backdrop_probe.py    # screenshots the backdrop and checks it isn't a flat tint
python live_check.py        # installs the real hook, sends a real Alt+Tab, screenshots it
python -m fun_tab.preview --demo --light   # writes preview_wheel.png
```

`preview.py` renders the wheel and preview card to a PNG without installing the keyboard hook, which is the quickest way to iterate on the look.

## Notes and caveats

- The backdrop is a frozen capture on purpose, not DWM acrylic. Both DWM routes - the legacy accent policy and Windows 11's `DWMWA_SYSTEMBACKDROP_TYPE` - return success on a full-screen topmost window and then composite a flat tint with no trace of the windows behind it, which is where the "backdrop turns black" bug came from. `backdrop_probe.py` screenshots each variant if you want to check your own build.
- Capture is unavailable on some remote sessions; those fall back to a plain translucent veil.
- Works with borderless and windowed games. Exclusive fullscreen can block any overlay.
- Only one instance runs at a time.
- **Anti-cheat:** Fun Tab installs a global keyboard hook (`WH_KEYBOARD_LL`) and a topmost overlay. It is not a cheat and never reads another process's memory, but hooks and overlays alone can trip heuristics. Quit it from the tray before launching anything protected by Easy Anti-Cheat, BattlEye, Vanguard or FACEIT.
