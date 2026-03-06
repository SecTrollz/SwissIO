# SwissIO Technical State of the Union

This document captures the current transition from the Python prototype to a low-level portable core.

## Legacy prototype (current runtime)

The existing host app (`app.py` + `detector.py`) is still the active local runtime.

- It orchestrates device discovery and protocol actions via Python handlers.
- It is useful for workflow validation and UI iteration.
- It remains limited by host dependency/tooling visibility.

## Locked architecture direction

SwissIO is moving to a **Direct Pipe** architecture:

1. Platform wrappers (Kotlin/Swift/C#) request host USB permission.
2. Wrapper opens the device and hands off a raw descriptor/handle.
3. Rust core (`SwissEngine`) maps descriptors/endpoints and performs protocol I/O.

## Discovery + ducking model

The target discovery flow is tiered:

1. Passive descriptor map (read-only)
2. Protocol ping (non-destructive)
3. Active fuzz (explicitly armed)

The shared risk model used by the Rust map is:

**Risk = Tier × (HiddenEndpoints + 1)**

where hidden endpoints are endpoints that ACK but are not descriptor-declared.

## Session safety lock

`SwissIOSession` is implemented as `Arc<RwLock<SessionState>>`.

- If session is not armed in Flash tier, write operations are blocked.
- `gated_write(...)` keeps write-lock checks centralized.

## Trace and entropy path

`TraceRingBuffer` provides frame-drain behavior suitable for a 60fps UI loop.

`estimate_entropy(...)` allows the trace panel to highlight likely compressed/encrypted/noisy regions.

## Platform parity targets

| Feature | Android 16 | macOS | Windows 11 |
| --- | --- | --- | --- |
| Permission API | `UsbManager` | `IOKit` / `IOUSBHost` | WinUSB |
| Root required | No | No | No |
| Shared core | Rust (`aarch64`) | Rust (`arm64`/`x86_64`) | Rust (`x86_64`) |
