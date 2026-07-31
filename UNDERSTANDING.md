# UNDERSTANDING.md — Robot Tool Layer (Phase 1)

> **⚠️ Historical Phase-1 design record.** The tool surface below (14 tools) is the original
> plan and has since evolved. The **current surface is 18 tools** — see
> [README.md](README.md) for the authoritative list. Notable changes since:
> - `spatial_navigate` **removed** (the Director composes primitive moves in a closed loop);
> - `ask_clarification` **merged into** `speak(text, friction_type)` (one tool; `friction_type`
>   required — `none` or one of five friction types);
> - **added** expression tools (`move_arm`, `move_head`, `display_image`, `change_led`),
>   `get_world`, and `get_last_view`;
> - per-motor **locks** + async offload added for real concurrency.
>
> The analysis below is kept as the original reasoning; treat specifics as of Phase 1.

Pre-build analysis for `ponder_multi_agent`. **No code written yet.** This documents the
capabilities I will wrap from the two reference codebases and how I propose to expose them
as a standalone MCP server. It ends with open decisions I need you to confirm before I build.

Reference-only sources (do NOT modify):
- `../EmbodiedPF/` — original single-VLM robot system. Real robot capabilities live here.
- `../misty_multi_agent/` — prior hand-orchestrated multi-agent version. Ignore its
  architecture; reuse only the small, self-contained `vision_ops` module.

Scope of THIS phase: **only the robot tool layer (one MCP server).** No agent loop, no
Director, no steering files. Those come later, on top of this layer.

---

## 1. Where the real capabilities live

The four capabilities named in the brief map to concrete code as follows:

| Capability | File | Nature |
|---|---|---|
| `MistyController` | `EmbodiedPF/model/misty_controller.py` | Top-level orchestrator (owns the interaction loop). **Mostly out of scope** — the Agent SDK replaces this loop. Only its `setup_robot()` idea is reusable. |
| `ActionExecutor` | `EmbodiedPF/model/action_executor.py` | The real motion + behavior primitives (drive/turn/strafe/stop, speak, find_object, spatial_navigate) **plus timing calibration**. This is the heart of what we wrap. |
| `VisionHandler` | `EmbodiedPF/model/vision_handler.py` | Camera capture → base64 (`capture_and_encode()`). |
| `vision_ops` | `misty_multi_agent/misty_agents/vision_ops.py` | Two pure VLM operations (`describe_image`, `find_object_in_images`) refactored out of the old `LLMLayer`, routed through an injectable `LLMBackend`. |

Underlying transport: `EmbodiedPF/PythonSDKmain/mistyPy/Robot.py` (`Robot(ip)` → REST wrapper,
synchronous, `requests`-based). Key low-level commands I confirmed:
- `drive_time(linearVelocity, angularVelocity, timeMs, degree=None)` — REST `drive/time`
- `take_picture(base64, fileName, width, height, displayOnScreen, overwriteExisting)`
- `speak(text, pitch, speechRate, voice, flush, utteranceId, language)`
- `move_head(pitch, roll, yaw, velocity, duration, units)`
- `display_image(fileName, alpha, layer, isURL)`, `change_led(red, green, blue)`, `stop(hold)`

---

## 2. ActionExecutor — the primitives I will wrap (verified signatures)

All movement is **time-based dead reckoning** (no odometry). `drive_time(50,0,ms)` drives,
`drive_time(0,±100,ms)` rotates. Two calibration curves convert intent → milliseconds and
**must be preserved** (they encode real hardware behavior):

```python
_calculate_drive_time(distance):   # 2500 * distance^0.5  (ms)
_calculate_turn_time(degrees):     # <30°: degrees*100 ;  ≥30°: (4500/sqrt(90))*sqrt(degrees)
```

Primitives (return value = execution time in ms, used for sequencing sleeps):
- `move_forward(distance=1.0)` → `drive_time(50, 0, t)`
- `move_backward(distance=1.0)` → `drive_time(-50, 0, t)`
- `move_left(distance)` / `move_right(distance)` → **strafe** (turn 45°, drive, turn back)
- `turn_left(degrees, speak=True)` → `drive_time(0, 100, t)`
- `turn_right(degrees, speak=True)` → `drive_time(0, -100, t)`
- `stop()` → `robot.stop()`
- `speak(text)` → `robot.speak(text)`
- `describe_vision()` → `VisionHandler.capture_and_encode()` + VLM `describe_image`
- `find_object(target_object)` → **360° scan**: 4 captures at 90° intervals, returns to start,
  then `find_object_in_images(target, images)`. `images` = `[{"direction","data"}, ...]`.
- `spatial_navigate(target_object, distance, turn_degrees)` → turn (sign = direction) then
  drive `max(0.3, distance-0.5)` (collision margin). Vision is assumed already done upstream.

Note: in the old code these methods also **spoke narration** and consulted `friction_type`.
Friction and narration are *brain* concerns and should NOT live in the tool layer — tools
should do the physical act and return a factual result; the agent decides what to say.

## 3. VisionHandler — capture

- `capture_image()` → `take_picture(base64=True, width=1600, height=1200, ...)`, extracts
  filename from the response.
- `download_image(filename)` → HTTP GET `http://{ip}/api/images?FileName=...` → base64.
- `capture_and_encode()` → the above two combined → base64 JPEG string (or `None`).
- Has a built-in `time.sleep(2)` before capture (camera warm-up).

## 4. vision_ops — VLM reasoning (self-contained, reusable as-is)

- `describe_image(backend, image_b64) -> str` — 2–3 sentence description.
- `find_object_in_images(backend, target, images) -> {found, response, count, locations}` —
  analyzes the 4 scan frames, returns a spoken-ready `response` string.
- Both depend on `LLMBackend.generate(messages, images=[b64,...]) -> str`
  (`misty_multi_agent/misty_agents/backends/base.py`). Provider-agnostic.

---

## 5. Proposed MCP tool surface

A standalone MCP server (Python) that constructs one `Robot(ip)` and thin wrappers around
`ActionExecutor` / `VisionHandler` / `vision_ops`, exposing:

**Movement** — `move_forward`, `move_backward`, `strafe_left`, `strafe_right`,
`turn_left`, `turn_right`, `stop`. (Preserve the calibration math; return factual outcomes
like `{"ok": true, "duration_ms": N}` — no narration.)

**Navigation** — `spatial_navigate(target_object, distance, turn_degrees)` (kept as a
composite because it bundles the collision-margin logic).

**Speech** — `speak(text)`.

**Vision** — `capture_view()` and `find_object(target_object)` (360° scan). **See Decision A**
for how much VLM reasoning belongs here.

**Expression (optional)** — `display_image(emotion)`, `change_led`, `move_head` for affect.

I will keep the wrappers thin, port the calibration verbatim, and strip friction/narration.

---

## 6. Decisions I need you to confirm before building

**A. Where does vision *reasoning* live?** The Agent SDK's model is itself a VLM, so we can
either (1) have `capture_view`/`find_object` return **raw base64 image(s)** and let the agent
reason about them (most SDK-native; drops the `LLMBackend` dependency entirely), or
(2) wrap `vision_ops` internally so tools return **text descriptions** (self-contained, needs
a backend + API key in the server). *My recommendation: option 1* — return images, let the
agent see. `find_object` still performs the physical 360° scan and returns 4 labeled frames.

**B. Language/runtime for the MCP server.** Python (the SDK, `Robot`, and `vision_ops` are all
Python) via the MCP Python SDK / FastMCP. *Recommend Python.* Confirm?

**C. Robot connection & config.** Read `MISTY_IP` (and any API key) from env; construct one
`Robot` at startup. Fine to assume the robot may be offline during dev — should tools fail
loudly, or should I include a `--mock`/simulated mode for development without hardware?

**D. Import strategy for reference code.** The two sources are siblings, not packages here.
Do you want me to (1) `sys.path`-import from `../EmbodiedPF` and `../misty_multi_agent`, or
(2) **copy** the minimal files (`action_executor`, `vision_handler`, `vision_ops`, the
`mistyPy` SDK) into this repo so it's self-contained? *Recommend copying* the minimal set so
this repo stands alone and the references stay untouched.

**E. Expression tools** — include `display_image`/`change_led`/`move_head` now, or defer to a
later phase and ship only movement + speech + vision first?

---

---

## 7. LOCKED DECISIONS (confirmed)

- **A. Vision = raw images.** `capture_view` / `find_object` return raw base64 frames; the
  agent (itself a VLM) judges presence. `find_object` does the **physical 360° scan only**
  and returns the 4 labeled frames — it does NOT run `find_object_in_images`.
  → **`vision_ops` is dead code under this model and will NOT be copied.** No `LLMBackend`,
  no VLM API key in this server. The tool layer is purely mechanical.
- **B. Python.** MCP Python SDK / FastMCP.
- **C. Two runtimes.** *Real mode (default):* construct `Robot(MISTY_IP)` and **fail loudly
  at startup** if unreachable — no silent pretending. *Stub mode (`ROBOT_STUB=1`, opt-in):*
  fake all robot calls for offline desk testing. No auto-mock ever in real mode.
- **D. Copy specific files only.** Vendor `action_executor.py`, `vision_handler.py`, and the
  `mistyPy/` SDK into this repo. Do **NOT** copy `model/__init__.py` (it eagerly imports the
  SpeechHandler → pyaudio audio stack and would break import). Reference dirs stay untouched.
  - *Deviation:* movement primitives in `action_executor.py` hardcode `robot.speak(...)`
    narration. Tool wrappers will **reuse its calibration helpers** (`_calculate_drive_time`,
    `_calculate_turn_time`) but issue `drive_time` directly, so **tools never narrate**.
- **E. Expression tools deferred** (`display_image`/`change_led`/`move_head` — later phase).

### Added to scope
- **Speech split into two distinct tools:** `speak(text)` (normal) and
  `ask_clarification(question, friction_type=null)` (separate, so clarifications are
  countable/gate-able later). `friction_type` is a **logged label only, not enforced**.
- **World memory** — a `WorldState` class over a persistent JSON file, exposing
  `get_known_location(object)` and `update_world(object, info)` where `info` is an **open
  dict** (flexible schema, no required fields). `WorldState` owns safe I/O: **atomic writes,
  per-object updates never wipe other objects, no implicit deletes, every write logged.**
- **Tool-call logging (JSONL):** every call logs `{name, args, result, timestamp}`
  (`ask_clarification` especially). Image results are logged **redacted** (frame count/size,
  not raw base64) to keep the log readable.

### Final tool surface (14 tools)

| # | Tool | Signature | Returns |
|---|---|---|---|
| 1 | `move_forward` | `(distance=1.0)` | `{ok, duration_ms}` |
| 2 | `move_backward` | `(distance=1.0)` | `{ok, duration_ms}` |
| 3 | `strafe_left` | `(distance=1.0)` | `{ok, duration_ms}` |
| 4 | `strafe_right` | `(distance=1.0)` | `{ok, duration_ms}` |
| 5 | `turn_left` | `(degrees)` | `{ok, duration_ms}` |
| 6 | `turn_right` | `(degrees)` | `{ok, duration_ms}` |
| 7 | `stop` | `()` | `{ok}` |
| 8 | `spatial_navigate` | `(target_object, distance, turn_degrees)` | `{ok, duration_ms}` — composite (turn + drive w/ `max(0.3, distance-0.5)` collision margin) |
| 9 | `speak` | `(text)` | `{ok}` |
| 10 | `ask_clarification` | `(question, friction_type=null)` | `{ok}` — logged label |
| 11 | `capture_view` | `()` | one raw base64 JPEG frame |
| 12 | `find_object` | `(target_object)` | physical 360° scan → 4 labeled frames `[{direction, image}]`, no VLM |
| 13 | `get_known_location` | `(object)` | stored info dict or null |
| 14 | `update_world` | `(object, info: dict)` | `{ok}` — atomic, non-destructive merge |

*(Open question: #8 `spatial_navigate` is now partly redundant with `turn_* + move_forward`;
I kept it because it encodes the collision-margin safety. Drop it if you'd rather the agent
compose turn+move itself.)*

**Status: decisions locked. Tool list above awaiting your final OK before I build.**
