# Human Interaction Simulation — Design Document

**Module**: `scraper_common.human_mouse`, `scraper_common.human_typing`, `scraper_common.human_scrolling`  
**Entry points**: `patch_mouse_movement()`, `patch_watchdog_typing()`, `patch_scroll_page()`  
**Status**: Fully implemented

---

## 1. Purpose and Architecture

The goal is to make browser automation indistinguishable from real human interaction at the CDP (Chrome DevTools Protocol) level. Modern bot-detection systems (Cloudflare, DataDome, PerimeterX, Akamai, etc.) profile these signals:

- Inter-keystroke timing distributions
- Mouse movement trajectories and velocity profiles
- Click position within element bounds
- Mouse button press-hold durations
- Post-click cursor drift
- Absence of a mouse-move + click preceding text input (programmatic typing without focus interaction)

The patches operate at the **lowest possible interception level** to maximise coverage with minimal code duplication:

| Layer | What is patched | File |
|---|---|---|
| `cdp_use.InputClient.dispatchMouseEvent` | All CDP mouse events | `human_mouse.py` |
| `browser_use.DefaultActionWatchdog._type_to_page` | Fallback typing path — prefixed with pre-typing focus sequence (mouse move + click) | `human_typing.py` |
| `browser_use.DefaultActionWatchdog._input_text_element_node_impl` | Primary typing path — outer wrapper for pre-typing focus sequence + inner asyncio proxy for keystroke timing | `human_typing.py` |
| `browser_use.DefaultActionWatchdog.on_ScrollEvent` | LLM-initiated page scroll commands | `human_scrolling.py` |

All patches are **idempotent** (guarded by `_human_typing_patched` / `_human_mouse_patched` / `_human_scrolling_patched` flags) and registered once at startup from `scraper_base.py`.

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
| `_DWELL_MIN_S` | 0.35 s |
| `_DWELL_MAX_S` | 0.65 s |

---

### 2.4 Click Position Offset

After hover/dwell, the actual `mousedown` coordinates are **not** the element centre.

#### Motivation

Real users aim loosely at a button and click somewhere inside it; they almost never land on the geometric centre. Behavioural classifiers (DataDome, PerimeterX) flag repeated centre-clicks as synthetic. The model must produce a flat, uniform spread across the entire element surface — every interior point is equally likely, just as a human carelessly tapping anywhere inside the target would produce.

This same rule applies to the pre-typing focus click: when the mouse is moved and clicked to focus an input field before keystrokes begin, the click lands at a uniformly-sampled position inside the input's bounding box, not at its centre.

#### Algorithm — with bounding box

When a trusted bounding box is available (`_BOUNDS_MIN_PX=8` ≤ width/height ≤ `_BOUNDS_MAX_PX=400`):

1. **Inset the box** — shrink by a small margin to avoid clicking on the element's border or padding:
   ```
   margin_x = max(_CLICK_MARGIN_MIN_PX, bw × _CLICK_MARGIN_FRAC)
   margin_y = max(_CLICK_MARGIN_MIN_PX, bh × _CLICK_MARGIN_FRAC)

   x_min = bx + margin_x
   x_max = bx + bw - margin_x
   y_min = by + margin_y
   y_max = by + bh - margin_y
   ```

2. **Sample uniformly** inside the inset box:
   ```
   cx = U(x_min, x_max)
   cy = U(y_min, y_max)
   ```

   The requested target point is discarded; the position is drawn solely from the element's geometry. Every point within the inset rectangle is equally likely — no density pile-up at the centre or near the edges.

**Example** — 120 × 40 px button:
- margin_x = max(2, 6) = 6 px; margin_y = max(2, 2) = 2 px.
- `cx ∈ U(bx+6, bx+114)`, `cy ∈ U(by+2, by+38)`.
- Clicks are evenly distributed across the full face of the button.

**Example** — 20 × 20 px checkbox:
- margin_x = margin_y = 2 px.
- `cx ∈ U(bx+2, bx+18)`, `cy ∈ U(by+2, by+18)`.
- All 16×16 interior pixels are equally reachable.

**Example** — input field focused before typing, 240 × 36 px:
- margin_x = max(2, 12) = 12 px; margin_y = max(2, 1.8) = 2 px.
- Mouse clicks at any `(U(bx+12, bx+228), U(by+2, by+34))` — spread naturally across the text-entry area.

#### Algorithm — without bounding box

When the bounds query fails or the element is outside the trusted size range:
- Simple Gaussian offset from the requested target: `gauss(0, σ=_CLICK_FALLBACK_SIGMA_PX)` per axis.
- No clamping (no box to clamp to).

The chosen `(click_x, click_y)` is stored in `_mouse_x, _mouse_y` so the subsequent `mouseReleased` event uses the same coordinates.

#### Parameters

| Symbol | Value | Meaning |
|---|---|---|
| `_CLICK_MARGIN_FRAC` | 0.05 | Inset margin as fraction of element dimension |
| `_CLICK_MARGIN_MIN_PX` | 2.0 px | Absolute inset floor |
| `_CLICK_FALLBACK_SIGMA_PX` | 4.0 px | Fallback Gaussian offset when no bounding box |

> **Change from previous design**: the previous algorithm sampled from a Gaussian centred on the element's geometric centre (σ = bw/3, clamped to the element bounds). The new design replaces this with a uniform distribution over the inset bounding box. This eliminates the centre-biased density concentration and ensures every interior point is equally likely — a more accurate model of real human click spread.

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

`DefaultActionWatchdog.on_ScrollEvent` — the method called when the LLM agent issues a scroll command. This is patched by full method replacement, **not** at the CDP `mouseWheel` level.

**Why not `mouseWheel`**: browser-use never emits a `mouseWheel` CDP event when the LLM sends a scroll command. The installed version routes LLM page scrolls through `_scroll_with_cdp_gesture`, which calls `Input.synthesizeScrollGesture` directly at very high speed — bypassing `InputClient.dispatchMouseEvent` entirely. Intercepting `mouseWheel` would therefore never fire for LLM-driven scrolls.

**Element-level scrolls** (`event.node is not None`) are delegated to the original `on_ScrollEvent` handler unchanged, because those need element-specific targeting logic that is not replicated here.

#### Patching strategy

The patch replaces `DefaultActionWatchdog.on_ScrollEvent` (guarded by `_human_scrolling_patched` in `human_scrolling.py`):

```python
_orig = DefaultActionWatchdog.on_ScrollEvent

async def on_ScrollEvent(self, event) -> None:
    if event.node is not None:
        return await _orig(self, event)
    await _human_scroll_impl(self.browser_session, event.direction, event.amount)

DefaultActionWatchdog.on_ScrollEvent = on_ScrollEvent
```

The replacement function is named `on_ScrollEvent` (not `_human_on_ScrollEvent`) because browser-use asserts that registered handlers must satisfy `handler.__name__.startswith('on_')`.

`_human_scroll_impl` decomposes the total `amount` into bell-shaped notch steps and drives them via raw CDP `Input.dispatchMouseEvent(mouseWheel)` calls (via `cdp_client.send_raw`) — the only place in this codebase that intentionally emits `mouseWheel` CDP events for scroll. The viewport centre is used as the scroll position; the CDP session is fetched once and reused for all notch events in a single scroll gesture.

#### Motivation

A single large `deltaY` arriving in one CDP event is an immediate bot signal. Real users scroll in a series of discrete wheel notches or trackpad micro-gestures with brief pauses between them. Crucially, human scroll velocity is not constant: users accelerate into a scroll gesture and decelerate at the end — the first and last notches are always shorter than the peak-speed notches in the middle. This bell-shaped step-size distribution is a strong behavioural signal that flat or random-uniform decompositions fail to reproduce. Detectors fingerprint not only the total delta and notch count, but also the per-step size distribution.

A second, finer-grained concern motivates the micro-increment layer: each physical mouse-wheel notch is not delivered as one instantaneous CDP event — the OS reports it as a rapid burst of small `mouseWheel` ticks spanning 8–20 ms each as the wheel rotates through its detent. Emitting a single large `deltaY` per notch therefore still looks synthetic at the per-event level. The micro-increment decomposition models this physical wheel-rotation characteristic: each bell-shaped notch is subdivided into 3–7 equal micro-events fired 8–20 ms apart, while the inter-notch pauses (250–600 ms) remain to separate distinct wheel detent engagements.

#### Algorithm

1. **Pass-through for tiny scrolls**  
   If `|amount| < _SCROLL_SMALL_THRESHOLD` (30 px), the scroll is already human-sized — emit it as a single `mouseWheel` CDP event and return.

2. **Choose step count**  
   Draw `n ∈ [_SCROLL_STEPS_MIN, _SCROLL_STEPS_MAX]` uniformly at random.

3. **Compute bell-shaped base weights**  
   Use a half-sine curve to derive per-step proportions:
   ```
   w_i = sin(π × (i + 0.5) / n)   for i = 0 … n−1
   ```
   This yields a symmetric bell: the endpoint steps are `sin(π / (2n))` (smallest) and the centre step(s) approach 1.0 (largest).

   Shape by step count:

   | n | weights (approximate) |
   |---|---|
   | 3 | 0.50 · 1.00 · 0.50 |
   | 4 | 0.38 · 0.92 · 0.92 · 0.38 |
   | 5 | 0.31 · 0.81 · 1.00 · 0.81 · 0.31 |
   | 6 | 0.26 · 0.71 · 0.97 · 0.97 · 0.71 · 0.26 |

4. **Convert weights to proportional step sizes**  
   Normalise so the base steps sum exactly to `|amount|`:
   ```
   W      = Σ w_i
   raw_i  = |amount| × w_i / W
   ```

5. **Apply per-step jitter**  
   Each step receives an independent multiplicative perturbation. The jitter magnitude itself is drawn per-step so no two steps share the same noise level:
   ```
   j_i      = U(_SCROLL_JITTER_MIN, _SCROLL_JITTER_MAX)   # magnitude: 10–20 %
   dir_i    = choice(−1, +1)                               # random sign
   scale_i  = 1 + dir_i × j_i
   s_i      = raw_i × scale_i
   ```

6. **Re-normalise to preserve exact total**  
   The jitter shifts the sum away from `|amount|`. Divide out the drift so the total never overshoots or undershoots the agent's intent:
   ```
   s_i_final = s_i × |amount| / Σ s_i
   ```
   This guarantees `Σ s_i_final = |amount|` exactly. The bell shape is preserved because re-normalisation is a uniform scalar — it scales every step by the same factor, leaving their relative proportions intact.

7. **Emit sub-scroll events**  
   Before the first step, sleep for a pre-scroll dwell `U(_SCROLL_DWELL_MIN_S, _SCROLL_DWELL_MAX_S)` to model the moment the user positions their fingers on the wheel. Then, for each notch step `i`:

   a. **Sub-divide into micro-increments**: draw `m_i ∈ [_SCROLL_MICRO_STEPS_MIN, _SCROLL_MICRO_STEPS_MAX]` uniformly at random. Distribute `s_i_final[i]` evenly into `m_i` micro-deltas:
      ```
      μ_j = s_i_final[i] / m_i   for j = 0 … m_i − 1
      ```
      Equal subdivision keeps each micro-event the same size within a notch; the bell-shape variation between notches is preserved at the coarser level.

   b. **Emit micro-events**: send `m_i` consecutive raw CDP `Input.dispatchMouseEvent(mouseWheel)` events at the viewport centre, each carrying `deltaX=0, deltaY=sign(amount) × μ_j`. Between consecutive micro-events sleep `U(_SCROLL_MICRO_INTER_MIN_S, _SCROLL_MICRO_INTER_MAX_S)` (skip the pause after the last micro-event of the notch).

   c. **Inter-notch pause**: after all micro-events of notch `i` complete, sleep `U(_SCROLL_INTER_STEP_MIN_S, _SCROLL_INTER_STEP_MAX_S)` before notch `i+1` (skipped after the last notch).

8. **Horizontal scroll**  
   When `direction == "left"` or `"right"`, the same algorithm applies to `deltaX`. For combined diagonal scroll, each axis runs through its own independent bell decomposition (separate draws of `n` and jitter values); the resulting step sequences are interleaved alternately (one X notch, one Y notch, …).

#### Parameters

| Symbol | Value | Meaning |
|---|---|---|
| `_SCROLL_SMALL_THRESHOLD` | 30 px | Pass-through threshold for tiny scrolls |
| `_SCROLL_STEPS_MIN` | 3 | Minimum notch events per scroll |
| `_SCROLL_STEPS_MAX` | 6 | Maximum notch events per scroll |
| `_SCROLL_JITTER_MIN` | 0.10 | Minimum per-step jitter magnitude (10 %) |
| `_SCROLL_JITTER_MAX` | 0.20 | Maximum per-step jitter magnitude (20 %) |
| `_SCROLL_INTER_STEP_MIN_S` | 0.25 s | Minimum pause between notches |
| `_SCROLL_INTER_STEP_MAX_S` | 0.60 s | Maximum pause between notches |
| `_SCROLL_DWELL_MIN_S` | 0.30 s | Pre-scroll finger-placement pause (min) |
| `_SCROLL_DWELL_MAX_S` | 0.70 s | Pre-scroll finger-placement pause (max) |
| `_SCROLL_MICRO_STEPS_MIN` | 3 | Minimum micro-events per notch |
| `_SCROLL_MICRO_STEPS_MAX` | 7 | Maximum micro-events per notch |
| `_SCROLL_MICRO_INTER_MIN_S` | 0.008 s | Minimum pause between micro-events within a notch |
| `_SCROLL_MICRO_INTER_MAX_S` | 0.020 s | Maximum pause between micro-events within a notch |

**Example** — agent requests `deltaY = 800 px` (scroll down), `n = 5` drawn:
- Base weights: [0.309, 0.809, 1.000, 0.809, 0.309]; W ≈ 3.236
- Raw steps (bell): [76, 200, 247, 200, 77] px — sum = 800
- Per-step jitter magnitudes: [14 %, 18 %, 11 %, 20 %, 16 %]; directions: [−, +, −, +, −]
- Scaled: [65, 236, 220, 240, 65] px — sum = 826
- Re-normalised: [63, 229, 213, 233, 62] px — sum = 800 exactly
- Micro-event counts drawn: [4, 6, 5, 4, 3]
- Emitted sequence (dwell 490 ms first):
  - Notch 1 (63 px → 4×15.75 px): `↓15.8` sleep 11ms `↓15.8` sleep 14ms `↓15.8` sleep 9ms `↓15.8` → inter-notch sleep 340 ms
  - Notch 2 (229 px → 6×38.2 px): `↓38.2` sleep 17ms … (×5 more) → inter-notch sleep 270 ms
  - Notch 3 (213 px → 5×42.6 px): `↓42.6` sleep 12ms … (×4 more) → inter-notch sleep 510 ms
  - Notch 4 (233 px → 4×58.3 px): `↓58.3` sleep 19ms … (×3 more) → inter-notch sleep 390 ms
  - Notch 5 (62 px → 3×20.7 px): `↓20.7` sleep 8ms `↓20.7` sleep 15ms `↓20.7`

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

### 3.0 Pre-Typing Focus Sequence

#### Motivation

When a human types into a web form they first move the mouse to the input field, click it to give it keyboard focus, wait briefly for focus handlers to settle, and only then begin pressing keys. Bot-detection systems flag text-input CDP events that arrive without a preceding click on the target element — this is one of the clearest programmatic-typing signals. Both typing paths must therefore be prefixed with a complete human-style focus interaction using the existing mouse simulation infrastructure.

#### Algorithm

1. **Resolve element coordinates**  
   Call `_query_element_bounds(client, session_id, x, y)` (the same helper used in §2.3 / §2.4) with the element's nominal centre coordinates. If the query fails or returns a distrusted bounding box (outside `[8, 400] px`), the pre-typing sequence is **skipped** and typing begins immediately.

2. **Mouse move to element** (`mouseMoved`)  
   Dispatch a synthetic `Input.dispatchMouseEvent(mouseMoved)` targeting the element's centre `(bx + bw/2, by + bh/2)`. Because `InputClient.dispatchMouseEvent` is already patched by `patch_mouse_movement()`, this single call automatically executes:
   - Suppression check — if the cursor is already inside the element, the move is suppressed and the path is skipped entirely (§2.2 special case)
   - Cubic Bézier curved path from current cursor position (§2.2)
   - Asymmetric ease-in-out-sine timing (§2.2)
   - Gaussian tremor on intermediate waypoints (§2.2)

3. **Click to focus** (`mousePressed` + `mouseReleased`)  
   Dispatch `mousePressed` then `mouseReleased` at the element centre. The existing mouse patch automatically executes:
   - Hover micro-moves (2–4 steps, §2.3) — may be skipped if the cursor arrived inside the element at step 2
   - Dwell pause `U(200, 500) ms` (§2.3)
   - Click position uniform offset (§2.4)
   - Click hold `U(50, 150) ms` + lift-off drift (§2.5)

4. **Post-focus dwell**  
   Sleep `U(_PRE_TYPE_POST_CLICK_MIN_S, _PRE_TYPE_POST_CLICK_MAX_S)` before the first keystroke. This gap lets the browser dispatch `focus` / `focusin` events and gives any JS focus handler time to run (e.g. clearing placeholder text, opening dropdowns) before keyboard events arrive.

#### Parameters

| Symbol | Value | Meaning |
|---|---|---|
| `_PRE_TYPE_POST_CLICK_MIN_S` | 2.75 s | Minimum pause between click release and first keystroke |
| `_PRE_TYPE_POST_CLICK_MAX_S` | 3.55 s | Maximum pause between click release and first keystroke |

#### Patching Strategy

Both typing paths gain an outer method-level wrapper for the focus sequence, applied inside `patch_watchdog_typing()`:

- **`_input_text_element_node_impl`** (primary path): a new async wrapper replaces the method. It runs the pre-typing focus sequence, then delegates to the original method (which already has the `asyncio` proxy installed for keystroke timing). The element node object carries the nodeId which is resolved to viewport coordinates via `DOM.getBoxModel` or `Runtime.evaluate(node.getBoundingClientRect())`.
- **`_type_to_page`** (fallback path): the full rewrite already owns the method body, so the focus sequence is prepended directly at the top.

In both cases the pre-typing focus sequence executes through `InputClient.dispatchMouseEvent`, meaning it benefits from all mouse simulation logic automatically and requires no duplication of movement or click code.

#### Interaction with Existing Cursor State

The mouse move dispatched in step 2 updates the process-global `_mouse_x / _mouse_y` state (§2.1) exactly as any other human move would. After the click sequence completes, the cursor sits near the element (within the lift-off drift, §2.5). The subsequent keystroke events are unaffected.

---

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

Full rewrite. Method body structure:

1. **Pre-typing focus sequence** (§3.0): resolve element coords → `mouseMoved` → `mousePressed` → `mouseReleased` → post-focus dwell.
2. **Keystroke loop** via raw CDP `Input.dispatchKeyEvent` events:
   - For `\n`: dispatches `keyDown(Enter)` + `char(\r)` + `keyUp(Enter)`.
   - For all other characters: dispatches `keyDown(char)` + `char(char)` + `keyUp(char)`.
   - Calls `_human_delay()` after each character's three events.

### 3.4 Patch 2 — `_input_text_element_node_impl` (Primary Path)

The primary path receives **two layers of wrapping**, both applied inside `patch_watchdog_typing()`:

1. **Outer wrapper** — prepends the pre-typing focus sequence (§3.0): mouse move → click → post-focus dwell. This replaces the method on `DefaultActionWatchdog` and calls the original (inner) method after the focus sequence completes.

2. **Inner asyncio proxy** — installed in the watchdog module's global namespace (see §3.1). It intercepts per-character `asyncio.sleep` calls inside the original method body and replaces them with `_human_delay()`.

The outer wrapper handles pre-typing interaction; the inner proxy handles keystroke timing. They are independent and compose cleanly: the proxy is installed first (module-level), the outer wrapper is applied second (method-level).

The outer wrapper resolves element coordinates from the `element_node` parameter via `Runtime.evaluate(node.getBoundingClientRect())` using the CDP client extracted from `self.browser_session`.

**Inner asyncio proxy sleep swap (unchanged from current implementation):**

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
  ├─ patch_mouse_movement()      [human_mouse.py]        ← MUST run first
  │    ├─ set _VIZ_ENABLED from env / arg
  │    ├─ import InputClient
  │    ├─ guard: _human_mouse_patched?
  │    └─ replace dispatchMouseEvent
  │         (the typing patch calls dispatchMouseEvent, so mouse must be
  │          patched before typing to get human-style moves on pre-type focus)
  │
  ├─ patch_watchdog_typing()     [human_typing.py]
  │    ├─ import DefaultActionWatchdog
  │    ├─ guard: _human_typing_patched?
  │    ├─ install asyncio proxy in watchdog module globals
  │    │    (intercepts per-keystroke asyncio.sleep for _input_text_element_node_impl)
  │    ├─ replace _type_to_page
  │    │    └─ new body: pre-typing focus sequence (§3.0) → keyDown/char/keyUp loop
  │    └─ wrap _input_text_element_node_impl with outer wrapper
  │         ├─ outer: pre-typing focus sequence (§3.0)
  │         │         resolve coords → mouseMoved → mousePressed → mouseReleased
  │         │         → post-focus dwell → call original
  │         └─ inner: asyncio proxy already in place for keystroke timing
  │
  └─ patch_scroll_page()         [human_scrolling.py]
       ├─ import DefaultActionWatchdog
       ├─ guard: _human_scrolling_patched?
       └─ replace on_ScrollEvent
```

**Startup order requirement**: `patch_mouse_movement()` must be called before `patch_watchdog_typing()`. The typing patch dispatches `mouseMoved` / `mousePressed` / `mouseReleased` events via `InputClient.dispatchMouseEvent`; if the mouse patch has not yet been applied those calls bypass the Bézier / hover / dwell logic entirely. `scraper_base.py` must preserve this ordering.

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

### 5.5 Scroll Events — implemented in `human_scrolling.py`
The patch intercepts `DefaultActionWatchdog.on_ScrollEvent` (not `mouseWheel` CDP events — the installed browser-use routes LLM page scrolls through `synthesizeScrollGesture`, never `mouseWheel`). The two-tier delivery model drives raw `Input.dispatchMouseEvent(mouseWheel)` calls via `cdp_client.send_raw` at the viewport centre:

- **Tier 1 (notch level)**: bell-shaped decomposition (half-sine weights, 10–20 % per-step jitter, re-normalised to exact total), 250–600 ms inter-notch pauses.
- **Tier 2 (micro-event level)**: each notch is subdivided into 3–7 equal micro-events fired 8–20 ms apart, modelling the rapid burst of OS wheel-rotation ticks that a single physical detent produces.

### 5.6 Pre-Typing Focus Sequence — Implemented

Both typing paths now execute the pre-typing focus sequence described in §3.0 before sending any keystroke events.

- **`_input_text_element_node_impl`**: coordinates are resolved via `self.browser_session.get_element_coordinates(element_node.backend_node_id, cdp_session)`, which tries `DOM.getContentQuads` → `DOM.getBoxModel` → `Runtime.callFunctionOn(getBoundingClientRect)` in order. The `cdp_session` comes from `self.browser_session.cdp_client_for_node(element_node)`.
- **`_type_to_page`**: coordinates are resolved by evaluating `document.activeElement.getBoundingClientRect()` via `Runtime.evaluate`. This works because the LLM typically clicks an element before the fallback typing path is invoked, leaving it as `document.activeElement`.
- In both cases the sequence is silently skipped if coordinates cannot be resolved or fall outside the `[8, 400] px` trust range.

### 5.7 Keyboard — No Key-Down Hold Variation
`keyDown` and `keyUp` events are dispatched back-to-back with no delay between them (the per-character delay follows `keyUp`). Real typists hold keys for 60–120 ms. The key-down-hold duration should vary per key (longer for modifier keys, shorter for fast-typed common letters).

### 5.8 Keyboard — No Typo / Correction Simulation
Humans make typos and correct them. Injecting occasional `Backspace` sequences (with higher probability on long words) would further defeat ML-based behavioural classifiers trained on error-free bot input.

### 5.9 No Randomised Start Position
`_mouse_x/_mouse_y` initialises to `(100, 100)`. If the first event on a fresh page is a click, the cursor starts at a fixed position. A random initial position within the viewport would remove this artefact.

### 5.10 Viz Overlay — Fixed-element Pages
The overlay uses `position:fixed` which works on most pages. On pages that override `<html>` transform or use custom compositor layers, the overlay may not track correctly. Low-priority since viz is a debug aid only.

---

## 6. Data Flow Summary

```
browser_use action
       │
       ▼
DefaultActionWatchdog
  ├─ _input_text_element_node_impl   ──────────────────────────────────────────┐
  │   (primary typing path)                                                     │
  │                                                                             │
  │   [outer wrapper — pre-typing focus sequence]                              │
  │   1. query element bounds → getBoundingClientRect()                        │
  │   2. dispatchMouseEvent(mouseMoved, elem_centre)  ──────────────────────►──┤
  │        → Bézier path, ease-in-out-sine, hover suppress if on-target        │
  │   3. dispatchMouseEvent(mousePressed, elem_centre) ─────────────────────►──┤
  │        → hover micro-moves (or skip) → dwell → uniform click offset       │
  │   4. dispatchMouseEvent(mouseReleased, click_pos) ──────────────────────►──┤
  │        → hold 50–150 ms → release → lift-off drift                        │
  │   5. asyncio.sleep U(100, 300 ms)  [post-focus dwell]                     │
  │                                                                             │
  │   [inner asyncio proxy — keystroke timing]                                 │
  │   asyncio.sleep(< 20ms) → intercepted → _human_delay()                    │
  │                                                                             │
  ├─ _type_to_page                                                              │
  │   (fallback typing path — full rewrite)                                    │
  │                                                                             │
  │   [pre-typing focus sequence — same as above]                              │
  │   1. query element bounds                                                   │
  │   2–4. dispatchMouseEvent: mouseMoved → mousePressed → mouseReleased  ───►─┤
  │   5. asyncio.sleep U(100, 300 ms)  [post-focus dwell]                     │
  │                                                                             │
  │   [keystroke loop]                                                         │
  │   keyDown/char/keyUp + _human_delay() per character                        │
  │                                                                             │
  └─ on_ScrollEvent (patched)                                                  │
      element scroll (node≠None): delegates to original handler unchanged      │
      page scroll: browser-use uses synthesizeScrollGesture, not               │
        mouseWheel — on_ScrollEvent is the correct intercept point             │
      ├─ if |amount| < 30 px: single mouseWheel CDP event (no dwell)           │
      └─ else: dwell 300–700 ms, then 3–6 bell-shaped notches                 │
              (half-sine weights, ±10–20 % jitter, re-normalised)             │
              each notch → 3–7 micro-events (8–20 ms apart)                   │
              emitted via cdp_client.send_raw(mouseWheel) at viewport          │
              centre; 250–600 ms inter-notch pauses between detents            │
                                                                               │
browser_use click / move  ◄────────────────────────────────────────────────────┘
       │                   (typing patch calls dispatchMouseEvent for focus)
       ▼
InputClient.dispatchMouseEvent (patched)
  ├─ mouseMoved
  │   ├─ suppress if already inside target element
  │   └─ Bézier path (n steps, async sleep per step, ease-in-out-sine)
  │
  ├─ mousePressed
  │   ├─ query element bounds
  │   ├─ if already inside: dwell only
  │   └─ else: 2–4 hover micro-moves → dwell → Gaussian click offset
  │
  └─ mouseReleased
      ├─ hold delay (50–150ms or override)
      ├─ release at press coords
      └─ lift-off drift move

             _human_delay() ← used by both typing paths for per-keystroke timing
             U(50,150ms) + 8% × U(150,400ms) hesitation
```

---

## 7. Test / Validation Approach

The patches are validated empirically:

1. **Visualization** (`HEADLESS=false`): watch the red-dot overlay trace natural curved paths including the pre-typing mouse move to the input field and the focus click before keystrokes begin.
2. **CDP event log**: record events with a CDP proxy logger; verify velocity profiles and inter-keystroke histograms. For typing actions, confirm that the event stream contains `mouseMoved` + `mousePressed` + `mouseReleased` before the first `keyDown`, with realistic timing gaps.
3. **Detection canaries**: run the scraper against known detection-heavy pages (Cloudflare, Google reCAPTCHA v3 score endpoints) and monitor success/challenge rates.
4. **Unit tests** (not yet implemented): mock `InputClient._client.send_raw` and assert that for a given `mouseMoved` target, the emitted waypoint count, sleep durations, and final position match expectations within statistical bounds.
5. **Pre-typing sequence test** (not yet implemented): invoke a patched typing path with a known element node, capture all CDP events emitted, and assert: (a) a `mouseMoved` event sequence precedes the first `keyDown`, (b) a `mousePressed` / `mouseReleased` pair follows the move, (c) the gap between `mouseReleased` and the first `keyDown` is within `[_PRE_TYPE_POST_CLICK_MIN_S, _PRE_TYPE_POST_CLICK_MAX_S]`.
