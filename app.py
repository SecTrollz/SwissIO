"""
SwissIO — FastAPI backend
Local USB device discovery + terminal command bridge
"""

import asyncio
import base64
import json
import shlex
import sys
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
import uvicorn

# Local imports
sys.path.insert(0, str(Path(__file__).parent))
from detector import enumerate_devices, watch_devices, USBDevice
from protocols import DFUHandler, UARTHandler, OpenOCDHandler, IOMMUHandler
from local_ai import LocalAssistantOrchestrator, SessionContext
from ghidra_mcp import load_ghidra_mcp_config, describe_ghidra_mcp


app = FastAPI(title="SwissIO", version="1.1.0")

ROOT_DIR = Path(__file__).parent
INDEX_FILE = ROOT_DIR / "index.html"

device_ws_clients: list[WebSocket] = []
terminal_sessions: dict[str, dict] = {}  # session_id -> {ws, uart, armed, tier, profile}
framebuffer_sessions: dict[str, dict] = {}  # session_id -> {ws, enabled, width, height, fps, task, tick}
assistant = LocalAssistantOrchestrator()
ghidra_cfg = load_ghidra_mcp_config()


async def broadcast_devices(devices: list[USBDevice]):
    payload = json.dumps({"type": "devices", "data": [d.to_dict() for d in devices]})
    dead = []
    for ws in device_ws_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in device_ws_clients:
            device_ws_clients.remove(ws)


@app.on_event("startup")
async def startup():
    asyncio.create_task(watch_devices(broadcast_devices))


@app.get("/")
async def root():
    return FileResponse(INDEX_FILE)


@app.get("/api/devices")
async def get_devices():
    devices = await enumerate_devices()
    return [d.to_dict() for d in devices]


@app.websocket("/ws/devices")
async def ws_devices(ws: WebSocket):
    await ws.accept()
    device_ws_clients.append(ws)
    devices = await enumerate_devices()
    await ws.send_text(json.dumps({"type": "devices", "data": [d.to_dict() for d in devices]}))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in device_ws_clients:
            device_ws_clients.remove(ws)


@app.websocket("/ws/terminal/{session_id}")
async def ws_terminal(ws: WebSocket, session_id: str):
    await ws.accept()
    terminal_sessions[session_id] = {"ws": ws, "uart": None, "armed": False, "tier": 1, "profile": "beginner"}

    async def send(msg: str):
        await ws.send_text(json.dumps({"type": "output", "data": msg + "\r\n"}))

    await send("\x1b[1;36mSwissIO workbench connected\x1b[0m")
    await send("\x1b[90mUse Discover/Inspect/Operate actions or type help\x1b[0m")

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await send("\x1b[31mInvalid message format (expected JSON).\x1b[0m")
                continue
            if msg.get("type") == "input":
                await handle_command(msg.get("data", "").strip(), session_id, send)
    except WebSocketDisconnect:
        sess = terminal_sessions.pop(session_id, None)
        if sess and sess.get("uart"):
            sess["uart"].close()
        fb = framebuffer_sessions.pop(session_id, None)
        if fb and fb.get("task"):
            fb["enabled"] = False
            fb["task"].cancel()


@app.websocket("/ws/framebuffer/{session_id}")
async def ws_framebuffer(ws: WebSocket, session_id: str):
    await ws.accept()
    state = framebuffer_sessions.setdefault(
        session_id,
        {"ws": None, "enabled": False, "width": 360, "height": 640, "fps": 6, "task": None, "tick": 0},
    )
    state["ws"] = ws
    await ws.send_text(json.dumps({"type": "fb_status", "enabled": state["enabled"], "width": state["width"], "height": state["height"], "fps": state["fps"]}))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if session_id in framebuffer_sessions:
            framebuffer_sessions[session_id]["ws"] = None




def _profile_hint(profile: str) -> str:
    if profile == "expert":
        return "expert mode: aggressive read/diagnostic tactics enabled (still OS-permission bound)"
    return "beginner mode: guided safe discovery defaults"


def _requires_flash_gate(verb: str, args: list[str]) -> bool:
    if verb == "dfu" and args and args[0].lower() == "flash":
        return True
    if verb == "jtag" and args and args[0].lower() == "flash":
        return True
    if verb == "mem" and args and args[0].lower() == "write":
        return True
    return False


def _make_virtual_frame_rgb24(width: int, height: int, tick: int) -> bytes:
    frame = bytearray(width * height * 3)
    idx = 0
    for y in range(height):
        for x in range(width):
            frame[idx] = (x + tick) % 256
            frame[idx + 1] = (y + (tick * 2)) % 256
            frame[idx + 2] = (x ^ y ^ tick) % 256
            idx += 3
    return bytes(frame)


async def _framebuffer_loop(session_id: str):
    while True:
        state = framebuffer_sessions.get(session_id)
        if not state or not state.get("enabled"):
            return
        ws = state.get("ws")
        if ws:
            width = int(state["width"])
            height = int(state["height"])
            tick = int(state.get("tick", 0))
            rgb = await asyncio.to_thread(_make_virtual_frame_rgb24, width, height, tick)
            payload = {
                "type": "fb_frame",
                "width": width,
                "height": height,
                "encoding": "rgb24-base64",
                "tick": tick,
                "data": base64.b64encode(rgb).decode("ascii"),
            }
            try:
                await ws.send_text(json.dumps(payload))
            except Exception:
                state["ws"] = None
        state["tick"] = int(state.get("tick", 0)) + 1
        await asyncio.sleep(max(1.0 / float(state.get("fps", 6)), 0.05))


async def handle_command(cmd: str, session_id: str, send):
    if not cmd:
        return

    sess = terminal_sessions.get(session_id)
    if not sess:
        await send("\x1b[31mSession not found. Reconnect terminal.\x1b[0m")
        return
    try:
        parts = shlex.split(cmd)
    except ValueError as exc:
        await send(f"\x1b[31mCommand parse error: {exc}\x1b[0m")
        return
    if not parts:
        return
    verb = parts[0].lower()
    args = parts[1:]

    if _requires_flash_gate(verb, args):
        if not sess.get("armed"):
            await send("\x1b[31mWrite/flash blocked: ARM is off. Use `arm on` first.\x1b[0m")
            return
        if sess.get("tier") != 3:
            await send("\x1b[31mWrite/flash blocked: set tier 3 (Flash-Gated) first.\x1b[0m")
            return

    if verb == "profile":
        if not args:
            await send(f"Profile: {sess.get('profile', 'beginner')} ({_profile_hint(sess.get('profile', 'beginner'))})")
            return
        mode = args[0].lower()
        if mode in {"beginner", "expert"}:
            sess["profile"] = mode
            await send(f"Profile set to {mode} — {_profile_hint(mode)}")
        else:
            await send("Usage: profile <beginner|expert>")
        return

    if verb == "arm":
        if not args:
            await send(f"ARM is {'ON' if sess['armed'] else 'OFF'}")
            return
        mode = args[0].lower()
        if mode in {"on", "1", "true"}:
            sess["armed"] = True
            await send("ARM enabled for this session")
        elif mode in {"off", "0", "false"}:
            sess["armed"] = False
            await send("ARM disabled for this session")
        else:
            await send("Usage: arm [on|off]")
        return

    if verb == "tier":
        if not args:
            await send(f"Current discovery tier: {sess['tier']}")
            return
        if args[0] in {"1", "2", "3"}:
            sess["tier"] = int(args[0])
            tier_label = {1: "Passive", 2: "Protocol Ping", 3: "Flash-Gated"}[sess["tier"]]
            await send(f"Discovery tier set to {sess['tier']} ({tier_label})")
        else:
            await send("Usage: tier <1|2|3>")
        return

    if verb == "help":
        for line in [
            "\x1b[1mSwissIO command groups\x1b[0m",
            "  discover: scan (find devices), dfu list, mem probe",
            "  inspect : ports, uart baud <port>, jtag probe (read info)",
            "  operate : dfu read/flash, jtag dump/flash, mem read/write",
            "  session : profile <beginner|expert>, arm [on|off], tier <1|2|3>",
            "            tier 3 required for write/flash commands",
            "  expert  : mem probe <vid> <pid>, jtag probe <iface> <target>, dfu list",
            "  rights  : show right-to-repair workflow guidance",
            "  ai      : local AI assistant (status|ghidra|explain <text>|check <cmd>)",
            "  fb      : USB-C virtual framebuffer (status|start [w] [h] [fps]|stop)",
            "  terminal: uart open <port> [baud], uart close",
        ]:
            await send(line)
        return



    if verb == "ai":
        sub = args[0].lower() if args else "status"
        ctx = SessionContext(
            profile=sess.get("profile", "beginner"),
            armed=bool(sess.get("armed", False)),
            tier=int(sess.get("tier", 1)),
        )

        if sub == "status":
            await send(assistant.status())
            await send(describe_ghidra_mcp(ghidra_cfg))
            return

        if sub == "ghidra":
            await send(describe_ghidra_mcp(ghidra_cfg))
            return

        if sub == "explain":
            excerpt = " ".join(parts[2:]) if len(parts) > 2 else ""
            if not excerpt:
                await send("Usage: ai explain <terminal text>")
                return
            await send(assistant.explain_terminal_output(excerpt, ctx))
            return

        if sub == "check":
            candidate = " ".join(parts[2:]) if len(parts) > 2 else ""
            if not candidate:
                await send("Usage: ai check <command>")
                return
            gate = assistant.logic_check(candidate, ctx)
            state = "PASS" if gate.ok else "BLOCK"
            await send(f"AI logic-check {state}: {gate.reason}")
            return

        await send("Usage: ai status | ai ghidra | ai explain <terminal text> | ai check <command>")
        return

    if verb == "rights":
        lines = [
            "\x1b[1mRight-to-repair workflow (host-permission path)\x1b[0m",
            "1) Discover device identity and physical paths with scan/ports.",
            "2) Try vendor-supported recovery interfaces first (DFU/bootloader).",
            "3) Use serial/JTAG where exposed by hardware design.",
            "4) Backup firmware before any write/flash operation.",
            "5) Keep ARM + Tier 3 as intentional write gate to avoid accidental brick.",
            "6) For legacy tablets/phones, use official recovery/fastboot/DFU channels where available.",
            "\x1b[90mSwissIO does not bypass secure enclave/kernel protections; it helps you audit and repair via accessible interfaces.\x1b[0m",
        ]
        for line in lines:
            await send(line)
        return

    if verb == "scan":
        devices = await enumerate_devices()
        await send(f"\x1b[32mFound {len(devices)} device(s)\x1b[0m")
        await send(f"session profile: {sess.get('profile', 'beginner')}")
        for d in devices:
            await send(f"- {d.vendor_name} {d.product_name} [{d.vendor_id}:{d.product_id}]")
            await send(f"  location: {d.location or 'n/a'}")
            await send(f"  serial path: {d.serial_port or 'n/a'}")
        return

    if verb == "fb":
        sub = args[0].lower() if args else "status"
        state = framebuffer_sessions.setdefault(
            session_id,
            {"ws": None, "enabled": False, "width": 360, "height": 640, "fps": 6, "task": None, "tick": 0},
        )
        if sub == "status":
            await send(f"[FB] enabled={state['enabled']} size={state['width']}x{state['height']} fps={state['fps']}")
            return
        if sub == "start":
            try:
                width = int(args[1]) if len(args) > 1 else int(state["width"])
                height = int(args[2]) if len(args) > 2 else int(state["height"])
                fps = int(args[3]) if len(args) > 3 else int(state["fps"])
            except ValueError:
                await send("Usage: fb start [width] [height] [fps]")
                return
            if width <= 0 or height <= 0 or fps <= 0:
                await send("[FB] width/height/fps must be positive integers")
                return
            state["width"] = min(width, 1280)
            state["height"] = min(height, 1280)
            state["fps"] = min(fps, 20)
            state["enabled"] = True
            if not state.get("task") or state["task"].done():
                state["task"] = asyncio.create_task(_framebuffer_loop(session_id))
            await send(f"[FB] virtual capture started at {state['width']}x{state['height']} @ {state['fps']}fps")
            return
        if sub == "stop":
            state["enabled"] = False
            if state.get("task") and not state["task"].done():
                state["task"].cancel()
            state["task"] = None
            await send("[FB] capture stopped")
            return
        await send("Usage: fb status | fb start [width] [height] [fps] | fb stop")
        return

    if verb == "ports":
        async for line in UARTHandler.list_ports():
            await send(line)
        return

    if verb == "dfu":
        sub = args[0].lower() if args else "list"
        dfu = DFUHandler()
        if sub == "list":
            async for line in dfu.probe():
                await send(line)
        elif sub == "read":
            out = args[1] if len(args) > 1 else "/tmp/swissio_firmware.bin"
            async for line in dfu.read_firmware(out):
                await send(line)
        elif sub == "flash" and len(args) > 1:
            async for line in dfu.flash_firmware(args[1]):
                await send(line)
        else:
            await send("Usage: dfu list | dfu read [file] | dfu flash <file>")
        return

    if verb == "uart":
        sub = args[0].lower() if args else ""
        if sub == "open":
            port = args[1] if len(args) > 1 else "/dev/tty.usbmodem0001"
            try:
                baud = int(args[2]) if len(args) > 2 else 115200
            except ValueError:
                await send("\x1b[31mInvalid baud rate. Must be an integer.\x1b[0m")
                return
            handler = UARTHandler(port, baud)
            sess["uart"] = handler
            asyncio.create_task(_uart_stream(handler, send))
        elif sub == "close":
            if sess.get("uart"):
                sess["uart"].close()
                sess["uart"] = None
                await send("[UART] closed")
        elif sub == "baud":
            port = args[1] if len(args) > 1 else "/dev/tty.usbmodem0001"
            async for line in UARTHandler.detect_baud(port):
                await send(line)
        elif sess.get("uart"):
            await sess["uart"].write(cmd + "\r\n")
        else:
            await send("Usage: uart open <port> [baud] | uart baud <port> | uart close")
        return

    if verb == "jtag":
        sub = args[0].lower() if args else "probe"
        if sub == "probe":
            iface = args[1] if len(args) > 1 else "stlink"
            target = args[2] if len(args) > 2 else "STM32"
            ocd = OpenOCDHandler(interface=iface, target=target)
            async for line in ocd.probe():
                await send(line)
        elif sub == "dump":
            dump_file = args[1] if len(args) > 1 else "/tmp/swissio_dump.bin"
            address = args[2] if len(args) > 2 else "0x08000000"
            length = args[3] if len(args) > 3 else "0x80000"
            ocd = OpenOCDHandler()
            async for line in ocd.dump_flash(dump_file, address, length):
                await send(line)
        elif sub == "flash" and len(args) > 1:
            image_path = args[1]
            address = args[2] if len(args) > 2 else "0x08000000"
            ocd = OpenOCDHandler()
            async for line in ocd.flash_image(image_path, address):
                await send(line)
        else:
            await send("Usage: jtag probe [iface] [target] | jtag dump [file] [addr] [len] | jtag flash <file> [addr]")
        return

    if verb == "mem":
        sub = args[0].lower() if args else "probe"
        iommu = IOMMUHandler(args[1] if len(args) > 1 else None, args[2] if len(args) > 2 else None)
        if sub == "probe":
            async for line in iommu.probe():
                await send(line)
        elif sub == "read":
            try:
                addr = int(args[1], 16) if len(args) > 1 else 0
                length = int(args[2], 16) if len(args) > 2 else 0x100
            except ValueError:
                await send("\x1b[31mInvalid hex value. Use mem read <addr_hex> [len_hex].\x1b[0m")
                return
            async for line in iommu.read_memory(addr, length):
                await send(line)
        elif sub == "write" and len(args) > 2:
            try:
                address = int(args[1], 16)
                payload = bytes.fromhex(args[2])
            except ValueError:
                await send("\x1b[31mInvalid hex input. Use mem write <addr_hex> <data_hex>.\x1b[0m")
                return
            async for line in iommu.write_memory(address, payload):
                await send(line)
        else:
            await send("Usage: mem probe [vid] [pid] | mem read <addr_hex> [len_hex] | mem write <addr_hex> <data_hex>")
        return

    await send(f"Unknown command: {cmd}")


async def _uart_stream(handler: UARTHandler, send):
    try:
        async for data in handler.open():
            await send(data)
    except Exception as exc:
        await send(f"\x1b[31m[UART] stream error: {exc}\x1b[0m")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="warning")
