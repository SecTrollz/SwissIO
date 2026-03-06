# SwissIO

SwissIO is a **local-only USB device workbench** transitioning from a Python prototype to a shared Rust hardware core.

> SwissIO operates within host OS permissions and protocol access. It does not bypass kernel/device security boundaries.

## Current repository layout

- `app.py`, `detector.py`, `protocols.py`: active Python host prototype (device events + terminal bridge).
- `index.html`: 3-pane workbench UI (Rack / Workflow Canvas / Logic Trace).
- `swissengine/`: Rust core primitives for session safety, endpoint sweep mapping, risk scoring, and trace buffering.
- `swissio_state_of_union.md`: architecture direction and parity targets.

## Active runtime (prototype)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 app.py
```

Open: `http://localhost:8765`

## Workflow model

The UI supports a simple **Beginner mode** with step-by-step guidance and an **Expert mode** with deeper diagnostic tactics and richer protocol workflow hints.

1. **Discover**
   - Passive inventory (`scan`) and capability checks (`dfu list`).
2. **Inspect**
   - Read-only probing (`ports`, `mem probe`, `uart baud`).
3. **Operate**
   - Active write/flash commands are blocked unless the session is armed **and** set to tier 3 (Flash-Gated).

Session controls:

- `profile beginner|expert` sets the command guidance/tactics profile for the terminal session.
- `arm on|off` enables/disables write-class commands (`dfu flash`, `jtag flash`, `mem write`).
- `tier 1|2|3` sets discovery depth intent for the Python session (`tier 3` is Flash-Gated).

## Rust core mechanics (`swissengine`)

- `SwissIOSession` (`Arc<RwLock<SessionState>>`) for backend safety gating.
- `gated_write(...)` helper that rejects writes unless the session is armed in flash tier.
- `EndpointMap::from_endpoint_sweep(...)` for endpoint probing across `0x01..0x0F` and `0x81..0x8F`.
- Risk formula implementation: **`Risk = Tier * (HiddenEndpoints + 1)`**.
- `TraceRingBuffer` + `estimate_entropy(...)` for entropy-driven trace visualization pipelines.

Run crate tests:

```bash
cargo test --manifest-path swissengine/Cargo.toml
```


## Local AI + Ghidra MCP assist

SwissIO now includes a local assistant command group in the terminal:

- `ai status` — show local assistant state and MCP bridge config
- `ai ghidra` — print Ghidra MCP local endpoint config
- `ai explain <terminal text>` — summarize what output likely means
- `ai check <command>` — run preflight logic gate checks before execution

Environment variables for local Ghidra MCP wiring:

- `SWISSIO_GHIDRA_MCP_ENABLED=1`
- `SWISSIO_GHIDRA_MCP_SERVER=ghidra-local`
- `SWISSIO_GHIDRA_MCP_ENDPOINT=mcp://ghidra-local`
- `SWISSIO_GHIDRA_PROJECT_DIR=./ghidra_projects`

## Right-to-repair

See `RIGHT_TO_REPAIR.md` for a practical repair/repurpose playbook focused on auditable host-permission workflows.

## Notes

- Repository remains host-only and local-only.
- Protocol operations still depend on host tooling availability (`dfu-util`, `openocd`, serial access).
