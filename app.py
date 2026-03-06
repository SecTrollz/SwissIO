"""
SwissIO — FastAPI backend
Local USB device discovery + terminal command bridge
"""

import asyncio
import json
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
            msg = json.loads(raw)
            if msg.get("type") == "input":
                await handle_command(msg.get("data", "").strip(), session_id, send)
    except WebSocketDisconnect:
        sess = terminal_sessions.pop(session_id, None)
        if sess and sess.get("uart"):
            sess["uart"].close()




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


async def handle_command(cmd: str, session_id: str, send):
    if not cmd:
        return

    sess = terminal_sessions[session_id]
    parts = cmd.split()
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
            excerpt = cmd.split(" ", 2)[2] if len(parts) > 2 else ""
            await send(assistant.explain_terminal_output(excerpt, ctx))
            return

        if sub == "check":
            candidate = cmd.split(" ", 2)[2] if len(parts) > 2 else ""
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
            baud = int(args[2]) if len(args) > 2 else 115200
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
        ocd = OpenOCDHandler(interface=(args[1] if len(args) > 1 else "stlink"), target=(args[2] if len(args) > 2 else "STM32"))
        if sub == "probe":
            async for line in ocd.probe():
                await send(line)
        elif sub == "dump":
            async for line in ocd.dump_flash(args[1] if len(args) > 1 else "/tmp/swissio_dump.bin", args[2] if len(args) > 2 else "0x08000000", args[3] if len(args) > 3 else "0x80000"):
                await send(line)
        elif sub == "flash" and len(args) > 1:
            async for line in ocd.flash_image(args[1], args[2] if len(args) > 2 else "0x08000000"):
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
            addr = int(args[1], 16) if len(args) > 1 else 0
            length = int(args[2], 16) if len(args) > 2 else 0x100
            async for line in iommu.read_memory(addr, length):
                await send(line)
        elif sub == "write" and len(args) > 2:
            async for line in iommu.write_memory(int(args[1], 16), bytes.fromhex(args[2])):
                await send(line)
        else:
            await send("Usage: mem probe [vid] [pid] | mem read <addr_hex> [len_hex] | mem write <addr_hex> <data_hex>")
        return

    await send(f"Unknown command: {cmd}")


async def _uart_stream(handler: UARTHandler, send):
    async for data in handler.open():
        await send(data)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="warning")
