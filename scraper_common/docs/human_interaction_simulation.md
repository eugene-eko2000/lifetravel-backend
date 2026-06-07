# Human Interaction Simulation — Design Document

**Module**: `scraper_common.human_mouse`, `scraper_common.human_typing`  
**Entry points**: `patch_mouse_movement()`, `patch_watchdog_typing()`  
**Status**: Implemented (this document reverse-engineers the current state)

---

## 1. Purpose and Architecture

The goal is to make browser automation indistinguishable from real human interaction at the CDP (Chrome DevTools Protocol) level. Modern bot-detection systems (Cloudflare, DataDome, PerimeterX, Akamai, etc.) profile these signals:

- Inter-keystroke timing distributions
- Mouse movement trajectories and velocity profiles
- Click position within element bounds
- Mouse button press-hold durations
- Post-click cursor drift

The patches operate at the **lowest possible interception level** to maximise coverage with minimal code duplication:

| Layer | What is patched | File |
|---|---|---|
| `cdp_use.InputClient.dispatchMouseEvent` | All CDP mouse events | `human_mouse.py` |
| `browser_use.DefaultActionWatchdog._type_to_page` | Fallback typing path | `human_typing.py` |
| `browser_use.DefaultActionWatchdog._input_text_element_node_impl` | Primary typing path (via asyncio proxy) | `human_typing.py` |

Both patches are **idempotent** (guarded by `_human_typing_patched` / `_human_mouse_patched` flags) and registered once at startup from `scraper_base.py`.

---

## 2. Mouse Simulation (`human_mouse.py`)

### 2.1 Global State

```
_mouse_x, _mouse_y  float  (init: 100.0, 100.0)
```

A single process-wide cursor position. Every event that moves the cursor updates this. Because scraper tasks are expected to be sequential within a browser session, no per-session isolation is implemented.

**Known limitation**: concurrent browser sessions sharing one Python process will corrupt each other's cursor position.

---

### 2.2 Movement — Cubic Bézier Path

#### Trigger
Any `mouseMoved` CDP event whose target differs from `(_mouse_x, _mouse_y)` by ≥ 1 px.

#### Algorithm

1. **Waypoint count**: `n = clamp(distance / 12, 10, 50)` integer steps.
2. **Perpendicular unit vector**: `(px, py) = (-dy/dist, dx/dist)` — rotated 90° from the direction of travel.
3. **Bulge category** (sampled once per move):

   | Category | Probability | Bulge fraction of distance |
   |---|---|---|
   | Tight | 30 % | 6 – 13 % |
   | Medium | 45 % | 13 – 25 % |
   | Wide | 25 % | 25 – 45 % |

   Absolute floor: 6 px (prevents micro-moves from being perfectly straight).

4. **Control point placement**:
   - `cp1` at 20–40 % along the direct line, offset perpendicularly by `side1 × bulge × U(0.5, 1.0)`.
   - `cp2` at 60–80 % along the direct line, offset by `side2 × bulge × U(0.5, 1.0)`.
   - `side1 ∈ {-1, +1}` chosen randomly; `side2 = side1` with 70 % probability (single arc), else `-side1` (S-curve).

5. **Gaussian tremor** on all intermediate waypoints (not the final):
   - Per-axis jitter: `gauss(0, σ=1.0)`, hard-clamped to ±2 px.

6. **Timing** — asymmetric ease-in-out-sine:
   - Total path time: `clamp(TIME_BASE + dist × 0.35ms/px, _, 550ms)` ± 10 % uniform jitter.
   - Per-step time derived from the difference in `ease_inv(i/n)` between consecutive waypoints.
   - `ease_inv(p, skew)` maps spatial progress `p ∈ [0,1]` to elapsed-time fraction. At `skew=0.5` it is exact inverse-sine (symmetric). `skew ∈ [0.36, 0.46]` shifts peak velocity to 36–46 % through the path, producing quick acceleration followed by a longer deceleration phase — matching HCI-measured ballistic reach profiles.
   - Per-step fluctuation: ±15 % uniform on each step's sleep, so cruise speed is never perfectly constant.
   - Floor: 4 ms per step (`_MIN_STEP_SLEEP_S`).

#### Special case — cursor already on the target element
If `distance < 200 px` and a bounding-box query confirms `(_mouse_x, _mouse_y)` is already inside the target element (with 2 px edge margin), the move is **suppressed**. A no-op CDP event is sent at the cursor's current position. This models the behaviour after a screen change (modal close, accordion expansion) where a new clickable element loads under the existing cursor.

#### Parameters

| Symbol | Value | Meaning |
|---|---|---|
| `_TIME_BASE_S` | 0.10 s | Minimum path duration |
| `_TIME_PER_PX` | 0.00035 s/px | Speed slope |
| `_TIME_JITTER` | 0.10 | ±10 % speed variation |
| `_TIME_MAX_S` | 0.55 s | Maximum path duration |
| `_MIN_STEP_SLEEP_S` | 0.004 s | Floor per step |
| `_STEP_JITTER` | 0.15 | ±15 % per-step fluctuation |
| `_TREMOR_SIGMA` | 1.0 px | Gaussian jitter std-dev |
| `_TREMOR_CLAMP` | 2.0 px | Jitter hard cap |
| `_DECEL_SKEW_MIN/MAX` | 0.36 – 0.46 | Ease asymmetry range |

---

### 2.3 Pre-Click Hover (Hand Settling)

#### Trigger
`mousePressed` event, unless the cursor is already inside the target element's bounding box.

#### Algorithm
1. Query the target element's bounding box via `document.elementFromPoint(x,y).getBoundingClientRect()`.
2. If the cursor is already inside the box → skip hover entirely; jump to dwell pause.
3. Otherwise dispatch **2–4 random micro-moves** around the target:
   - Each offset drawn from `gauss(0, σ=1.5 px)` per axis.
   - Sleep `U(12, 30) ms` between each step.
4. **Dwell pause**: `U(200, 500) ms` after the final micro-move, before `mousedown`.

#### Parameters

| Symbol | Value |
|---|---|
| `_HOVER_STEPS_MIN/MAX` | 2 – 4 |
| `_HOVER_SIGMA` | 1.5 px |
| `_HOVER_STEP_MIN_S` | 0.012 s |
| `_HOVER_STEP_MAX_S` | 0.030 s |
| `_DWELL_MIN_S` | 0.20 s |
| `_DWELL_MAX_S` | 0.50 s |

---

### 2.4 Click Position Offset

After hover/dwell, the actual `mousedown` coordinates are **not** the element centre.

#### Motivation

Real users aim loosely at a button and click somewhere inside it; they almost never land on the geometric centre. Behavioural classifiers (DataDome, PerimeterX) flag repeated centre-clicks as synthetic. The model must produce a wide, natural-looking spread across the whole element surface.

#### Algorithm — with bounding box

When a trusted bounding box is available (`_BOUNDS_MIN_PX=8` ≤ width/height ≤ `_BOUNDS_MAX_PX=400`):

1. **Mean** — the geometric centre of the element, computed from the bounding box:
   ```
   mean_x = bx + bw / 2
   mean_y = by + bh / 2
   ```
   This is independent of the requested target point (which may already be the centre, or may be an arbitrary point inside the element — we discard it in favour of a box-derived anchor).

2. **Standard deviation** — proportional to the element dimensions with a generous scale factor so the distribution genuinely fills the element:
   ```
   σ_x = bw × _CLICK_SIGMA_FRAC     (e.g. bw/3)
   σ_y = bh × _CLICK_SIGMA_FRAC
   ```
   No small upper cap: on a 200 px wide card `σ_x ≈ 67 px`, giving clicks spread from left to right third naturally. A floor of `_CLICK_SIGMA_MIN_PX = 3 px` prevents near-zero σ on hairline elements.

3. **Sample**:
   ```
   cx = gauss(mean_x, σ_x)
   cy = gauss(mean_y, σ_y)
   ```

4. **Clamp** to an inset box to avoid clicking on borders/padding:
   ```
   margin_x = max(_CLICK_MARGIN_MIN_PX, bw × _CLICK_MARGIN_FRAC)
   margin_y = max(_CLICK_MARGIN_MIN_PX, bh × _CLICK_MARGIN_FRAC)
   cx = clamp(cx, bx + margin_x, bx + bw - margin_x)
   cy = clamp(cy, by + margin_y, by + bh - margin_y)
   ```

**Example** — 120 × 40 px button:
- σ_x = 40 px, σ_y = 13 px; margin_x = 4 px, margin_y = 2 px.
- ≈ 68 % of clicks land within ±40 px of centre horizontally, ±13 px vertically.
- Tails beyond the element edge are folded back by the clamp, producing a slight density pile-up near the edges — matching real-world click heatmaps on buttons.

**Example** — 20 × 20 px checkbox:
- σ_x = σ_y = 6.7 px; margin = 1 px.
- Clicks scatter naturally across the full face of the checkbox.

#### Algorithm — without bounding box

When the bounds query fails or the element is outside the trusted size range:
- Simple Gaussian offset from the requested target: `gauss(0, σ=_CLICK_FALLBACK_SIGMA_PX)` per axis.
- No clamping (no box to clamp to).

The chosen `(click_x, click_y)` is stored in `_mouse_x, _mouse_y` so the subsequent `mouseReleased` event uses the same coordinates.

#### Parameters

| Symbol | Proposed value | Meaning |
|---|---|---|
| `_CLICK_SIGMA_FRAC` | 0.33 | σ as a fraction of element dimension |
| `_CLICK_SIGMA_MIN_PX` | 3.0 px | Floor on σ for tiny elements |
| `_CLICK_MARGIN_FRAC` | 0.05 | Inset margin as fraction of dimension |
| `_CLICK_MARGIN_MIN_PX` | 2.0 px | Absolute inset floor |
| `_CLICK_FALLBACK_SIGMA_PX` | 4.0 px | Fallback offset when no bounding box |

> **Change from previous design**: the old algorithm used σ = `clamp(dim/4, 1, 10)` — a hard 10 px cap that made clicks cluster tightly near centre regardless of element size. The new design removes the cap and anchors the mean on the box centre rather than the requested target point.

---

### 2.5 Click Hold and Release

#### Algorithm
1. On `mouseReleased`, sleep for the **hold duration** before sending the event:
   - If `_click_hold_override` ContextVar is set → use that value exactly.
   - Otherwise → `U(50, 150) ms`.
2. Send `mouseReleased` at `(_mouse_x, _mouse_y)` (the press coordinates, not the original target).
3. **Lift-off drift**: after releasing, sleep `U(18, 45) ms` then send one more `mouseMoved` at `gauss(0, σ=1.2 px)` offset from the release point.

#### `click_hold(duration)` API
A context-manager and an imperative setter allow callers to override the button hold duration for long-press or drag scenarios:

```python
with click_hold(2.5):
    await page.click("#slider")   # holds 2.5 s
```

The override is stored in a `ContextVar` so concurrent asyncio tasks are isolated.

#### Parameters

| Symbol | Value |
|---|---|
| `_CLICK_HOLD_MIN_S` | 0.050 s |
| `_CLICK_HOLD_MAX_S` | 0.150 s |
| `_LIFT_DELAY_MIN_S` | 0.018 s |
| `_LIFT_DELAY_MAX_S` | 0.045 s |
| `_LIFT_SIGMA` | 1.2 px |

*(Click position offset parameters are in §2.4.)*

---

### 2.6 Element Bounds Query

`_query_element_bounds(client, session_id, x, y)` executes:

```js
(function(x,y){
  var e = document.elementFromPoint(x,y);
  if (!e) return null;
  var r = e.getBoundingClientRect();
  return [r.left, r.top, r.width, r.height];
})(x, y)
```

via `Runtime.evaluate`. Failures are swallowed; callers fall back to the `gauss` offset path.

A result is considered **trusted** only when `8 ≤ width ≤ 400` and `8 ≤ height ≤ 400` — rejects body/container elements (too large) and near-invisible elements (too small).

---

### 2.7 Scroll Simulation

#### Trigger
Any `mouseWheel` CDP event. Currently passes through unmodified (gap §5.5). This section describes the target design.

#### Motivation

A single large `deltaY` arriving in one CDP event is an immediate bot signal. Real users scroll in a series of discrete wheel notches or trackpad micro-gestures — each moving the page 150–250 px — with brief pauses between them. Detectors fingerprint the number of scroll events, their deltas, and the inter-event cadence.

#### Algorithm

1. **Determine step count**  
   Choose `n ∈ [3, 5]` uniformly at random. This is the number of sub-scroll events to emit.  
   Exception: if `|deltaY| < _SCROLL_SMALL_THRESHOLD` (e.g. 100 px), the scroll is already human-sized — emit it as a single event unchanged.

2. **Generate per-step deltas**  
   Draw `n` independent samples:
   ```
   raw_i = U(_SCROLL_STEP_MIN_PX, _SCROLL_STEP_MAX_PX)   for i in 1..n
   ```
   Preserve the sign of the original `deltaY`.

3. **Adjust the final step to honour the requested total**  
   Real humans do reach their intended scroll position, they just do so gradually. To avoid drifting away from what the agent intended:
   ```
   total_raw   = sum(raw_i)
   last_step   = |deltaY| - sum(raw_i for i in 1..n-1)
   last_step   = clamp(last_step, _SCROLL_STEP_MIN_PX, _SCROLL_STEP_MAX_PX × 1.5)
   ```
   If `last_step` would go negative or exceed the cap, distribute the remainder evenly across all `n` steps instead (scale each `raw_i` by `|deltaY| / total_raw`).

4. **Emit sub-scroll events**  
   For each step `i`:
   - Send a `mouseWheel` CDP event with the step's `deltaY` (and the original `deltaX`, `modifiers`, `x`, `y`).
   - Sleep `U(_SCROLL_INTER_STEP_MIN_S, _SCROLL_INTER_STEP_MAX_S)` before the next step.
   - On the first step only, optionally add a brief pre-scroll dwell `U(_SCROLL_DWELL_MIN_S, _SCROLL_DWELL_MAX_S)` to simulate the moment the user positions their fingers on the wheel.

5. **Horizontal scroll**  
   `deltaX ≠ 0` events follow the same algorithm applied to `deltaX`. Combined diagonal scroll (`deltaX` and `deltaY` both non-zero) splits each axis independently and interleaves the events.

#### Parameters

| Symbol | Proposed value | Meaning |
|---|---|---|
| `_SCROLL_SMALL_THRESHOLD` | 30 px | Pass-through threshold for tiny scrolls |
| `_SCROLL_STEPS_MIN` | 3 | Minimum wheel-notch events per scroll |
| `_SCROLL_STEPS_MAX` | 5 | Maximum wheel-notch events per scroll |
| `_SCROLL_STEP_MIN_PX` | 50 px | Minimum delta per individual notch |
| `_SCROLL_STEP_MAX_PX` | 100 px | Maximum delta per individual notch |
| `_SCROLL_INTER_STEP_MIN_S` | 0.060 s | Minimum pause between notches |
| `_SCROLL_INTER_STEP_MAX_S` | 0.180 s | Maximum pause between notches |
| `_SCROLL_DWELL_MIN_S` | 0.050 s | Pre-scroll finger-placement pause (min) |
| `_SCROLL_DWELL_MAX_S` | 0.150 s | Pre-scroll finger-placement pause (max) |

**Example** — agent requests `deltaY = 800 px` (scroll down):
- `n = 4` steps drawn.
- Raw deltas: `[210, 175, 230, ?]`; sum of first 3 = 615; last step = 800 − 615 = 185 px (within range).
- Emitted: `↓210`, sleep 95 ms, `↓175`, sleep 140 ms, `↓230`, sleep 75 ms, `↓185` — total 800 px, natural cadence.

---

### 2.8 Visualization Overlay

When `HEADLESS=false` (or `visualize=True` passed to `patch_mouse_movement()`), a JS overlay is injected into the live page via `Runtime.evaluate`:

- **Red dot**: tracks current cursor position via CSS `left/top`.
- **Fading red trail**: each waypoint creates a 5 px dot that fades to opacity 0 over 700 ms.
- **Green dot tint + expanding ring**: on `mousedown`.
- **Red dot restored**: on `mouseup`.

Updates are fire-and-forget `asyncio.Task`s. If `Runtime.evaluate` raises, viz is permanently disabled (`_VIZ_OK = False`) for the rest of the run to avoid slowing down the real input path.

---

## 3. Keyboard Simulation (`human_typing.py`)

### 3.1 Patching Strategy

`DefaultActionWatchdog._input_text_element_node_impl` is a 200+ line method. Rather than copying it, the patch installs a **module-level asyncio proxy** in the watchdog module's namespace:

```
_wdog_mod.asyncio = _AsyncioProxy()
```

When watchdog code executes `asyncio.sleep(x)`, Python resolves `asyncio` from the module's global dict — which is now the proxy. The proxy delegates all attribute access to the real `asyncio` except `.sleep`, which is swapped per-call.

This approach:
- Requires zero knowledge of the 200+ line method's internals.
- Is resilient to upstream code changes (no copied code to drift).
- Only intercepts sleeps in that one module.

`_type_to_page` (the fallback typing path) is shorter and is fully rewritten.

### 3.2 Inter-Keystroke Timing

A single `_human_delay()` function provides timing for all keystroke events:

```
delay = U(50, 150) ms
if random() < 0.08:
    delay += U(150, 400) ms   # hesitation pause
```

This produces a bimodal distribution: a fast mode centred around 100 ms (normal typing) and an occasional 250–550 ms outlier (word boundary hesitation, thinking pause). Real human typing inter-key intervals measured in studies are well-modelled by a log-normal or Weibull distribution in the 60–300 ms range.

#### Parameters

| Symbol | Value | Meaning |
|---|---|---|
| `_MIN_DELAY` | 0.05 s | Fast end (50 ms) |
| `_MAX_DELAY` | 0.15 s | Slow end (150 ms) |
| `_HESITATION_PROB` | 0.08 | 8 % chance of pause |
| `_HESITATION_EXTRA` | (0.15, 0.40) s | Extra pause duration |

### 3.3 Patch 1 — `_type_to_page` (Fallback Path)

Full rewrite. Sends raw CDP `Input.dispatchKeyEvent` events via a CDPSession:

- For `\n`: dispatches `keyDown(Enter)` + `char(\r)` + `keyUp(Enter)`.
- For all other characters: dispatches `keyDown(char)` + `char(char)` + `keyUp(char)`.
- Calls `_human_delay()` after each character's three events.

### 3.4 Patch 2 — `_input_text_element_node_impl` (Primary Path)

Wraps the original method with a per-call sleep swap:

```python
async def _fast_to_human(delay: float) -> None:
    if delay < 0.02:           # inter-keystroke sleep (1ms, 5ms, 10ms)
        await real_asyncio.sleep(_human_delay())
    else:
        await real_asyncio.sleep(delay)   # scroll waits etc. pass through
```

The `< 20 ms` threshold targets the original watchdog's inter-keystroke `asyncio.sleep(0.010)` calls while preserving any longer waits (e.g., scroll-into-view, element-ready delays).

---

## 4. Patching Lifecycle

```
scraper_base.py (import time)
  │
  ├─ patch_watchdog_typing()     [human_typing.py]
  │    ├─ import DefaultActionWatchdog
  │    ├─ guard: _human_typing_patched?
  │    ├─ install asyncio proxy in watchdog module globals
  │    ├─ replace _type_to_page
  │    └─ wrap _input_text_element_node_impl
  │
  └─ patch_mouse_movement()      [human_mouse.py]
       ├─ set _VIZ_ENABLED from env / arg
       ├─ import InputClient
       ├─ guard: _human_mouse_patched?
       └─ replace dispatchMouseEvent
```

---

## 5. Known Gaps and Improvement Opportunities

### 5.1 Cursor State is Process-Global
`_mouse_x/_mouse_y` are module-level globals. Two concurrent browser sessions in the same process will race. Should be moved to a per-`InputClient` (or per-session) instance variable.

### 5.2 Typing Timing Distribution
The current model is a two-component mixture (uniform + uniform with 8 % hesitation). A **log-normal** distribution better fits measured human WPM data. The hesitation mechanism is syntactically unaware (it fires randomly per character, not at word/phrase boundaries).

Proposed: switch to `random.lognormvariate(mu, sigma)` with parameters tuned to match a 60–80 WPM typist, add boundary-aware hesitations (higher probability after space, punctuation, end of word).

### 5.3 Movement — No Overshoot / Correction
Real humans frequently overshoot a target and then micro-correct (Fitts' law predicts this for small targets). The current Bézier path always terminates exactly on the target. Adding a stochastic overshoot-and-correction sub-path for elements narrower than ~30 px would improve realism.

### 5.4 Movement — No Speed Adaptation to Target Size
Fitts' law: movement time `T = a + b × log2(2D/W)`. The current model only adapts to distance, not target width. Smaller targets should produce slower, more careful approach speeds.

### 5.5 Scroll Events — addressed in §2.7
Design specified in §2.7. Not yet implemented. The chosen model (3–5 notches of 150–250 px with 60–180 ms inter-notch pauses) covers wheel-notch fingerprinting. A further enhancement would be **momentum easing**: the first notch of a scroll sequence is slightly smaller than the peak, and the last notch decelerates, mirroring trackpad inertia. This is deferred to a later iteration.

### 5.6 Keyboard — No Key-Down Hold Variation
`keyDown` and `keyUp` events are dispatched back-to-back with no delay between them (the per-character delay follows `keyUp`). Real typists hold keys for 60–120 ms. The key-down-hold duration should vary per key (longer for modifier keys, shorter for fast-typed common letters).

### 5.7 Keyboard — No Typo / Correction Simulation
Humans make typos and correct them. Injecting occasional `Backspace` sequences (with higher probability on long words) would further defeat ML-based behavioural classifiers trained on error-free bot input.

### 5.8 No Randomised Start Position
`_mouse_x/_mouse_y` initialises to `(100, 100)`. If the first event on a fresh page is a click, the cursor starts at a fixed position. A random initial position within the viewport would remove this artefact.

### 5.9 Viz Overlay — Fixed-element Pages
The overlay uses `position:fixed` which works on most pages. On pages that override `<html>` transform or use custom compositor layers, the overlay may not track correctly. Low-priority since viz is a debug aid only.

---

## 6. Data Flow Summary

```
browser_use action
       │
       ▼
DefaultActionWatchdog
  ├─ _input_text_element_node_impl   ──────────────────────────────────────┐
  │   (primary typing path)                                                 │
  │   asyncio.sleep(< 20ms) → intercepted → _human_delay()                │
  │                                                                         │
  └─ _type_to_page                                                          │
      (fallback typing path)                                                │
      rewritten: keyDown/char/keyUp + _human_delay() per char              │
                                                                            │
browser_use click / move / scroll                                           │
       │                                                                     │
       ▼                                                                     │
InputClient.dispatchMouseEvent (patched)                                    │
  ├─ mouseMoved                                                              │
  │   ├─ suppress if already inside target element                          │
  │   └─ Bézier path (n steps, async sleep per step, ease-in-out-sine)     │
  │                                                                          │
  ├─ mousePressed                                                            │
  │   ├─ query element bounds                                                │
  │   ├─ if already inside: dwell only                                       │
  │   └─ else: 2–4 hover micro-moves → dwell → Gaussian click offset        │
  │                                                                          │
  ├─ mouseReleased                                                            │
  │   ├─ hold delay (50–150ms or override)                                   │
  │   ├─ release at press coords                                              │
  │   └─ lift-off drift move                                                 │
  │                                                                           │
  └─ mouseWheel                                                               │
      ├─ if |deltaY| < 100 px: pass through unchanged                        │
      └─ else: 3–5 sub-notches (150–250 px each) + 60–180 ms inter-pause    │
                                                                             │
             _human_delay() ◄──────────────────────────────────────────────┘
             U(50,150ms) + 8% × U(150,400ms) hesitation
```

---

## 7. Test / Validation Approach

The patches are validated empirically:

1. **Visualization** (`HEADLESS=false`): watch the red-dot overlay trace natural curved paths.
2. **CDP event log**: record events with a CDP proxy logger; verify velocity profiles and inter-keystroke histograms.
3. **Detection canaries**: run the scraper against known detection-heavy pages (Cloudflare, Google reCAPTCHA v3 score endpoints) and monitor success/challenge rates.
4. **Unit tests** (not yet implemented): mock `InputClient._client.send_raw` and assert that for a given `mouseMoved` target, the emitted waypoint count, sleep durations, and final position match expectations within statistical bounds.
