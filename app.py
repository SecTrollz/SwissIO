"""
USB Nexus — FastAPI Backend
WebSocket terminal bridge + device event streaming + REST API
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
import uvicorn

# Local imports
sys.path.insert(0, str(Path(__file__).parent))
from detector import enumerate_devices, watch_devices, USBDevice, ConnectionMode
from protocols import DFUHandler, UARTHandler, OpenOCDHandler, IOMMUHandler


# ─────────────────────────────────────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="USB Nexus", version="1.0.0")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Active WebSocket connections
device_ws_clients: list[WebSocket] = []
terminal_sessions: dict[str, dict] = {}  # session_id → {ws, handler}


# ─────────────────────────────────────────────────────────────────────────────
# Device event broadcaster
# ─────────────────────────────────────────────────────────────────────────────

async def broadcast_devices(devices: list[USBDevice]):
    payload = json.dumps({
        "type": "devices",
        "data": [d.to_dict() for d in devices]
    })
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


# ─────────────────────────────────────────────────────────────────────────────
# REST endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/devices")
async def get_devices():
    devices = await enumerate_devices()
    return [d.to_dict() for d in devices]


@app.get("/api/ports")
async def get_ports():
    ports = []
    async for line in UARTHandler.list_ports():
        ports.append(line)
    return {"ports": ports}


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket: device events stream
# ─────────────────────────────────────────────────────────────────────────────

@app.websocket("/ws/devices")
async def ws_devices(ws: WebSocket):
    await ws.accept()
    device_ws_clients.append(ws)
    # Send current state immediately
    devices = await enumerate_devices()
    await ws.send_text(json.dumps({
        "type": "devices",
        "data": [d.to_dict() for d in devices]
    }))
    try:
        while True:
            await ws.receive_text()  # keep alive
    except WebSocketDisconnect:
        if ws in device_ws_clients:
            device_ws_clients.remove(ws)


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket: terminal sessions
# ─────────────────────────────────────────────────────────────────────────────

@app.websocket("/ws/terminal/{session_id}")
async def ws_terminal(ws: WebSocket, session_id: str):
    await ws.accept()
    terminal_sessions[session_id] = {"ws": ws, "uart": None}

    async def send(msg: str):
        try:
            await ws.send_text(json.dumps({"type": "output", "data": msg + "\r\n"}))
        except Exception:
            pass

    await send("\x1b[1;32mUSB Nexus Terminal Ready\x1b[0m")
    await send("\x1b[90mType 'help' for available commands\x1b[0m")
    await send("")

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)

            if msg.get("type") == "input":
                cmd = msg.get("data", "").strip()
                await handle_command(cmd, session_id, send)

            elif msg.get("type") == "resize":
                pass  # cols/rows for future pty support

    except WebSocketDisconnect:
        # Clean up UART if open
        sess = terminal_sessions.pop(session_id, None)
        if sess and sess.get("uart"):
            sess["uart"].close()


async def handle_command(cmd: str, session_id: str, send):
    """Route terminal commands to the appropriate protocol handler."""
    if not cmd:
        return

    parts = cmd.split()
    verb = parts[0].lower() if parts else ""
    args = parts[1:]
    sess = terminal_sessions.get(session_id, {})

    # ── Help ──────────────────────────────────────────────────────────────
    if verb == "help":
        help_text = [
            "\x1b[1;36m┌─ USB Nexus Commands ────────────────────────────────────┐\x1b[0m",
            "\x1b[1;36m│\x1b[0m \x1b[1mDevice Detection\x1b[0m",
            "\x1b[1;36m│\x1b[0m   scan              — Enumerate connected USB devices",
            "\x1b[1;36m│\x1b[0m   ports             — List available serial ports",
            "\x1b[1;36m│\x1b[0m",
            "\x1b[1;36m│\x1b[0m \x1b[1mDFU (Device Firmware Upgrade)\x1b[0m",
            "\x1b[1;36m│\x1b[0m   dfu list          — List DFU devices",
            "\x1b[1;36m│\x1b[0m   dfu read [file]   — Read firmware from device",
            "\x1b[1;36m│\x1b[0m   dfu flash <file>  — Flash firmware to device",
            "\x1b[1;36m│\x1b[0m   dfu detach        — Send DFU_DETACH command",
            "\x1b[1;36m│\x1b[0m",
            "\x1b[1;36m│\x1b[0m \x1b[1mUART / Serial\x1b[0m",
            "\x1b[1;36m│\x1b[0m   uart open <port> [baud]  — Open serial connection",
            "\x1b[1;36m│\x1b[0m   uart baud <port>         — Auto-detect baud rate",
            "\x1b[1;36m│\x1b[0m   uart close               — Close serial connection",
            "\x1b[1;36m│\x1b[0m",
            "\x1b[1;36m│\x1b[0m \x1b[1mJTAG / SWD (OpenOCD)\x1b[0m",
            "\x1b[1;36m│\x1b[0m   jtag probe [iface] [target]  — Probe JTAG chain",
            "\x1b[1;36m│\x1b[0m   jtag halt                    — Halt target CPU",
            "\x1b[1;36m│\x1b[0m   jtag dump [file] [addr] [len]— Dump flash",
            "\x1b[1;36m│\x1b[0m   jtag flash <file> [addr]     — Flash image",
            "\x1b[1;36m│\x1b[0m   jtag server                  — Start OpenOCD server",
            "\x1b[1;36m│\x1b[0m   jtag cmd <tcl>               — Send Tcl command",
            "\x1b[1;36m│\x1b[0m",
            "\x1b[1;36m│\x1b[0m \x1b[1mMemory / IOMMU\x1b[0m",
            "\x1b[1;36m│\x1b[0m   mem probe [vid] [pid]        — Enumerate USB endpoints",
            "\x1b[1;36m│\x1b[0m   mem read <addr> [len]        — Control transfer read",
            "\x1b[1;36m│\x1b[0m   mem write <addr> <hexdata>   — Control transfer write",
            "\x1b[1;36m│\x1b[0m   mem bulk [ep] [len]          — Bulk endpoint read",
            "\x1b[1;36m│\x1b[0m",
            "\x1b[1;36m└─────────────────────────────────────────────────────────┘\x1b[0m",
        ]
        for line in help_text:
            await send(line)

    # ── Scan ──────────────────────────────────────────────────────────────
    elif verb == "scan":
        await send("\x1b[36mScanning USB devices...\x1b[0m")
        devices = await enumerate_devices()
        if not devices:
            await send("\x1b[33mNo USB devices found.\x1b[0m")
            return
        await send(f"\x1b[32mFound {len(devices)} device(s):\x1b[0m")
        for d in devices:
            await send(f"  \x1b[1m{d.vendor_name} — {d.product_name}\x1b[0m")
            await send(f"    VID:PID  \x1b[33m{d.vendor_id}:{d.product_id}\x1b[0m")
            await send(f"    Family   \x1b[32m{d.board_family.value}\x1b[0m")
            await send(f"    Mode     \x1b[36m{d.connection_mode.value}\x1b[0m")
            if d.serial_port:
                await send(f"    Port     \x1b[35m{d.serial_port}\x1b[0m")
            await send("")

    # ── Ports ─────────────────────────────────────────────────────────────
    elif verb == "ports":
        async for line in UARTHandler.list_ports():
            await send(line)

    # ── DFU ───────────────────────────────────────────────────────────────
    elif verb == "dfu":
        sub = args[0].lower() if args else "list"
        dfu = DFUHandler()
        if sub == "list":
            async for line in dfu.probe():
                await send(line)
        elif sub == "read":
            outfile = args[1] if len(args) > 1 else "/tmp/firmware_dump.bin"
            async for line in dfu.read_firmware(outfile):
                await send(line)
        elif sub == "flash":
            if len(args) < 2:
                await send("\x1b[31mUsage: dfu flash <firmware.bin>\x1b[0m")
                return
            async for line in dfu.flash_firmware(args[1]):
                await send(line)
        elif sub == "detach":
            async for line in dfu.detach():
                await send(line)

    # ── UART ──────────────────────────────────────────────────────────────
    elif verb == "uart":
        sub = args[0].lower() if args else ""
        if sub == "baud":
            port = args[1] if len(args) > 1 else "/dev/tty.usbmodem0001"
            async for line in UARTHandler.detect_baud(port):
                await send(line)
        elif sub == "open":
            port = args[1] if len(args) > 1 else "/dev/tty.usbmodem0001"
            baud = int(args[2]) if len(args) > 2 else 115200
            handler = UARTHandler(port, baud)
            sess["uart"] = handler
            asyncio.create_task(_uart_stream(handler, send))
        elif sub == "close":
            if sess.get("uart"):
                sess["uart"].close()
                sess["uart"] = None
                await send("\x1b[33m[UART] Connection closed.\x1b[0m")
            else:
                await send("\x1b[31m[UART] No active connection.\x1b[0m")
        else:
            # Pass raw input to open serial connection
            if sess.get("uart"):
                await sess["uart"].write(cmd + "\r\n")

    # ── JTAG ──────────────────────────────────────────────────────────────
    elif verb == "jtag":
        sub = args[0].lower() if args else "probe"
        iface = args[1] if len(args) > 1 else "stlink"
        target = args[2] if len(args) > 2 else "STM32"
        ocd = OpenOCDHandler(interface=iface, target=target)
        if sub == "probe":
            async for line in ocd.probe():
                await send(line)
        elif sub == "halt":
            async for line in ocd.halt():
                await send(line)
        elif sub == "dump":
            outfile = args[1] if len(args) > 1 else "/tmp/flash_dump.bin"
            addr   = args[2] if len(args) > 2 else "0x08000000"
            length = args[3] if len(args) > 3 else "0x80000"
            async for line in ocd.dump_flash(outfile, addr, length):
                await send(line)
        elif sub == "flash":
            if len(args) < 2:
                await send("\x1b[31mUsage: jtag flash <file> [address]\x1b[0m")
                return
            fpath = args[1]
            addr  = args[2] if len(args) > 2 else "0x08000000"
            async for line in ocd.flash_image(fpath, addr):
                await send(line)
        elif sub == "server":
            asyncio.create_task(_openocd_server(ocd, send))
        elif sub == "cmd":
            tcl_cmd = " ".join(args[1:])
            async for line in ocd.tcl_command(tcl_cmd):
                await send(line)

    # ── Memory / IOMMU ────────────────────────────────────────────────────
    elif verb == "mem":
        sub = args[0].lower() if args else "probe"
        vid = args[1] if len(args) > 1 else None
        pid = args[2] if len(args) > 2 else None
        iommu = IOMMUHandler(vid, pid)
        if sub == "probe":
            async for line in iommu.probe():
                await send(line)
        elif sub == "read":
            addr = int(args[1], 16) if len(args) > 1 else 0
            length = int(args[2], 16) if len(args) > 2 else 256
            async for line in iommu.read_memory(addr, length):
                await send(line)
        elif sub == "write":
            if len(args) < 3:
                await send("\x1b[31mUsage: mem write <addr_hex> <data_hex>\x1b[0m")
                return
            addr = int(args[1], 16)
            data = bytes.fromhex(args[2])
            async for line in iommu.write_memory(addr, data):
                await send(line)
        elif sub == "bulk":
            ep     = int(args[1], 16) if len(args) > 1 else 0x81
            length = int(args[2])     if len(args) > 2 else 512
            async for line in iommu.bulk_read(ep, length):
                await send(line)

    # ── Unknown ───────────────────────────────────────────────────────────
    else:
        await send(f"\x1b[31mUnknown command: {cmd}\x1b[0m  Type \x1b[36mhelp\x1b[0m for commands.")


async def _uart_stream(handler: UARTHandler, send):
    async for data in handler.open():
        await send(data)


async def _openocd_server(handler: OpenOCDHandler, send):
    async for line in handler.interactive():
        await send(line)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\x1b[1;32m")
    print("  ██╗   ██╗███████╗██████╗     ███╗   ██╗███████╗██╗  ██╗██╗   ██╗███████╗")
    print("  ██║   ██║██╔════╝██╔══██╗    ████╗  ██║██╔════╝╚██╗██╔╝██║   ██║██╔════╝")
    print("  ██║   ██║███████╗██████╔╝    ██╔██╗ ██║█████╗   ╚███╔╝ ██║   ██║███████╗")
    print("  ██║   ██║╚════██║██╔══██╗    ██║╚██╗██║██╔══╝   ██╔██╗ ██║   ██║╚════██║")
    print("  ╚██████╔╝███████║██████╔╝    ██║ ╚████║███████╗██╔╝ ██╗╚██████╔╝███████║")
    print("   ╚═════╝ ╚══════╝╚═════╝     ╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝")
    print("\x1b[0m")
    print("  \x1b[36mUniversal USB-C Device Terminal\x1b[0m")
    print("  \x1b[90mhttp://localhost:8765\x1b[0m")
    print()
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="warning")
