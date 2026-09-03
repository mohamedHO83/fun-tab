# Fun Tab

GTA-style **Alt+Tab** weapon wheel for Windows. Hold Alt, tap Tab, aim with the mouse, release Alt to switch.

## Why it’s lightweight

When you’re not switching apps the process is basically asleep:

- A low-level keyboard hook only wakes on key events (no polling loop eating CPU)
- The overlay windows stay **hidden** — nothing is painted
- Message wait uses a **500ms** idle timeout; only ramps to ~60fps while the wheel is open
- The dimmer is a constant-alpha black window (OS composited)
- The wheel itself is a small ~640px layered bitmap, not a full-screen screenshot blur

No Electron, no browser engine.

## Controls

| Input | Action |
|--------|--------|
| **Alt + Tab** | Open the wheel (current app selected) |
| **Move mouse** | Highlight a slice — name shows in the hub |
| **Tab / Shift+Tab** | Cycle clockwise / counter-clockwise |
| **Mouse wheel** | Cycle |
| **← / →** | Cycle |
| **Release Alt** or **click** | Switch to the highlighted app |
| **Esc** | Cancel, stay on the current app |

## Slice sizing

Slices always fill the ring equally:

- **1 app** → full ring  
- **2 apps** → 50 / 50  
- **N apps** → `360° / N` each  

Icons stay miniature; the hub shows the hovered app name.

## Run

```bat
pip install -r requirements.txt
run.bat
```

`run.bat` launches via `pythonw` — no console window, tray icon only.

Or:

```bat
python -m fun_tab
```

A tray icon appears — use **Quit** there when you’re done.

Optional preview (saves a PNG of the wheel without installing the hook):

```bat
python -m fun_tab.preview
```

## Notes

- Works best with **borderless / windowed** games. Exclusive fullscreen can block overlays.
- Only one instance runs at a time.
- Replaces the normal Windows Alt+Tab UI while Fun Tab is running.
- **Anti-cheat:** Fun Tab uses a global keyboard hook (`WH_KEYBOARD_LL`) and a topmost overlay. Some anti-cheat systems treat those as suspicious. Quit Fun Tab (tray → Quit) before launching games that use Easy Anti-Cheat, BattlEye, Vanguard, FACEIT, etc. This is not a cheat and does not read game memory — but hooks/overlays alone can still trip heuristics.

## Fun extras included

- Soft pop-in animation when the wheel opens (~0.5s)
- Live window previews under the wheel (Windows-style strip)
- `1/N` counter under the app name
- Frosted opaque wheel over a dimmer backdrop (wheel reads denser than the background)
