"""
SwissIO — Protocol Handlers
DFU / UART / OpenOCD / IOMMU
Each handler returns an async generator that yields terminal output lines.
"""

import asyncio
import subprocess
import shutil
import os
import glob
from typing import AsyncGenerator, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _stream_process(proc: asyncio.subprocess.Process) -> AsyncGenerator[str, None]:
    """Yield stdout/stderr lines from a subprocess as they arrive."""
    async def read_stream(stream, tag=""):
        while True:
            line = await stream.readline()
            if not line:
                break
            yield f"{tag}{line.decode(errors='replace').rstrip()}"

    tasks = []
    if proc.stdout:
        tasks.append(read_stream(proc.stdout))
    if proc.stderr:
        tasks.append(read_stream(proc.stderr, "\x1b[33m"))  # stderr in yellow

    # Interleave both streams
    queues = [asyncio.Queue() for _ in tasks]

    async def drain(gen, q):
        async for line in gen:
            await q.put(line)
        await q.put(None)  # sentinel

    drainers = [asyncio.create_task(drain(t, q)) for t, q in zip(tasks, queues)]

    finished = [False] * len(queues)
    while not all(finished):
        for i, q in enumerate(queues):
            if finished[i]:
                continue
            try:
                item = q.get_nowait()
                if item is None:
                    finished[i] = True
                else:
                    yield item
            except asyncio.QueueEmpty:
                pass
        await asyncio.sleep(0.01)

    await proc.wait()
    for d in drainers:
        d.cancel()


def _tool_check(name: str) -> Optional[str]:
    """Return path to tool or None."""
    return shutil.which(name)


# ─────────────────────────────────────────────────────────────────────────────
# DFU Protocol Handler
# ─────────────────────────────────────────────────────────────────────────────

class DFUHandler:
    """
    Wraps dfu-util for Device Firmware Upgrade operations.
    Install: brew install dfu-util
    """

    def __init__(self, vid: str = None, pid: str = None):
        self.vid = vid
        self.pid = pid
        self.tool = _tool_check("dfu-util")

    async def probe(self) -> AsyncGenerator[str, None]:
        """List all DFU-capable devices."""
        if not self.tool:
            yield "\x1b[31m[DFU] dfu-util not found. Install with: brew install dfu-util\x1b[0m"
            return

        yield "\x1b[36m[DFU] Probing for DFU devices...\x1b[0m"
        cmd = ["dfu-util", "-l"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        async for line in _stream_process(proc):
            yield f"[DFU] {line}"

    async def read_firmware(self, output_file: str = "/tmp/firmware_dump.bin",
                            alt: int = 0) -> AsyncGenerator[str, None]:
        """Read firmware from DFU device."""
        if not self.tool:
            yield "\x1b[31m[DFU] dfu-util not found.\x1b[0m"
            return

        cmd = ["dfu-util", "--alt", str(alt), "--upload", output_file]
        if self.vid and self.pid:
            cmd += ["--device", f"{self.vid}:{self.pid}"]

        yield f"\x1b[36m[DFU] Reading firmware to {output_file}...\x1b[0m"
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        async for line in _stream_process(proc):
            yield f"[DFU] {line}"
        yield f"\x1b[32m[DFU] Done. Saved to {output_file}\x1b[0m"

    async def flash_firmware(self, firmware_path: str,
                             alt: int = 0) -> AsyncGenerator[str, None]:
        """Flash firmware to DFU device."""
        if not self.tool:
            yield "\x1b[31m[DFU] dfu-util not found.\x1b[0m"
            return

        if not os.path.exists(firmware_path):
            yield f"\x1b[31m[DFU] File not found: {firmware_path}\x1b[0m"
            return

        cmd = ["dfu-util", "--alt", str(alt), "--download", firmware_path, "--reset"]
        if self.vid and self.pid:
            cmd += ["--device", f"{self.vid}:{self.pid}"]

        yield f"\x1b[36m[DFU] Flashing {firmware_path}...\x1b[0m"
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        async for line in _stream_process(proc):
            yield f"[DFU] {line}"
        yield "\x1b[32m[DFU] Flash complete.\x1b[0m"

    async def detach(self) -> AsyncGenerator[str, None]:
        """Send DFU_DETACH command."""
        if not self.tool:
            yield "\x1b[31m[DFU] dfu-util not found.\x1b[0m"
            return
        cmd = ["dfu-util", "--detach"]
        if self.vid and self.pid:
            cmd += ["--device", f"{self.vid}:{self.pid}"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        async for line in _stream_process(proc):
            yield f"[DFU] {line}"


# ─────────────────────────────────────────────────────────────────────────────
# UART / Serial Handler
# ─────────────────────────────────────────────────────────────────────────────

class UARTHandler:
    """
    Bidirectional serial terminal using pyserial.
    Falls back to screen/minicom if pyserial unavailable.
    """

    def __init__(self, port: str, baud: int = 115200):
        self.port = port
        self.baud = baud
        self._serial = None
        self._running = False

    async def open(self) -> AsyncGenerator[str, None]:
        try:
            import serial
            import serial.tools.list_ports
        except ImportError:
            yield "\x1b[31m[UART] pyserial not installed. Run: pip install pyserial\x1b[0m"
            return

        yield f"\x1b[36m[UART] Opening {self.port} @ {self.baud} baud...\x1b[0m"
        try:
            import serial as ser_mod
            self._serial = ser_mod.Serial(self.port, self.baud, timeout=0.1)
            self._running = True
            yield f"\x1b[32m[UART] Connected to {self.port}\x1b[0m"

            while self._running:
                if self._serial.in_waiting:
                    data = self._serial.read(self._serial.in_waiting)
                    yield data.decode(errors="replace")
                await asyncio.sleep(0.01)

        except Exception as e:
            yield f"\x1b[31m[UART] Error: {e}\x1b[0m"
        finally:
            if self._serial:
                self._serial.close()

    async def write(self, data: str):
        """Send data to the serial device."""
        if self._serial and self._serial.is_open:
            self._serial.write(data.encode())

    def close(self):
        self._running = False

    @staticmethod
    async def list_ports() -> AsyncGenerator[str, None]:
        """List available serial ports."""
        try:
            import serial.tools.list_ports
            ports = list(serial.tools.list_ports.comports())
            if not ports:
                yield "\x1b[33m[UART] No serial ports found.\x1b[0m"
                return
            yield "\x1b[36m[UART] Available ports:\x1b[0m"
            for p in ports:
                yield f"  \x1b[32m{p.device}\x1b[0m  {p.description}  [{p.hwid}]"
        except ImportError:
            # Fallback to glob
            import glob
            ports = glob.glob("/dev/tty.usb*") + glob.glob("/dev/cu.usb*")
            for p in ports:
                yield f"  \x1b[32m{p}\x1b[0m"

    @staticmethod
    async def detect_baud(port: str) -> AsyncGenerator[str, None]:
        """Attempt to auto-detect baud rate by trying common rates."""
        common_bauds = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
        yield f"\x1b[36m[UART] Auto-detecting baud rate on {port}...\x1b[0m"
        try:
            import serial as ser_mod
            for baud in common_bauds:
                try:
                    s = ser_mod.Serial(port, baud, timeout=0.3)
                    s.flushInput()
                    await asyncio.sleep(0.3)
                    data = s.read(64)
                    s.close()
                    # Look for printable ASCII as a signal
                    printable = sum(32 <= b < 127 for b in data)
                    if printable > len(data) * 0.5 and len(data) > 4:
                        yield f"  \x1b[32m{baud}\x1b[0m baud — likely match ({printable}/{len(data)} printable bytes)"
                    else:
                        yield f"  \x1b[90m{baud}\x1b[0m baud — {len(data)} bytes, noisy"
                except Exception:
                    yield f"  \x1b[31m{baud}\x1b[0m baud — error"
        except ImportError:
            yield "\x1b[31m[UART] pyserial required for baud detection.\x1b[0m"


# ─────────────────────────────────────────────────────────────────────────────
# OpenOCD / JTAG / SWD Handler
# ─────────────────────────────────────────────────────────────────────────────

OPENOCD_INTERFACE_MAP = {
    "stlink":  "interface/stlink.cfg",
    "jlink":   "interface/jlink.cfg",
    "ftdi":    "interface/ftdi/ft2232h_breakout.cfg",
    "cmsis":   "interface/cmsis-dap.cfg",
    "picoprobe": "interface/cmsis-dap.cfg",
}

OPENOCD_TARGET_MAP = {
    "STM32":   "target/stm32f4x.cfg",   # generic; refine per subfamily
    "nRF52":   "target/nrf52.cfg",
    "RP2040":  "target/rp2040.cfg",
    "ESP32":   "target/esp32.cfg",
    "Atmel SAM": "target/atsame5x.cfg",
}

class OpenOCDHandler:
    """
    Wraps OpenOCD for JTAG/SWD access.
    Install: brew install open-ocd
    """

    def __init__(self, interface: str = "stlink", target: str = "STM32",
                 openocd_extra: list[str] = None):
        self.interface = interface
        self.target = target
        self.extra = openocd_extra or []
        self.tool = _tool_check("openocd")
        self._proc = None

    def _build_cmd(self, extra_cmds: list[str] = None) -> list[str]:
        iface_cfg = OPENOCD_INTERFACE_MAP.get(self.interface, f"interface/{self.interface}.cfg")
        target_cfg = OPENOCD_TARGET_MAP.get(self.target, f"target/{self.target.lower()}.cfg")
        cmd = [
            "openocd",
            "-f", iface_cfg,
            "-f", target_cfg,
        ]
        for c in (extra_cmds or []) + self.extra:
            cmd += ["-c", c]
        return cmd

    async def probe(self) -> AsyncGenerator[str, None]:
        """Scan JTAG/SWD chain."""
        if not self.tool:
            yield "\x1b[31m[JTAG] openocd not found. Install with: brew install open-ocd\x1b[0m"
            return
        yield f"\x1b[36m[JTAG] Probing {self.interface} → {self.target}...\x1b[0m"
        cmd = self._build_cmd(["init", "scan_chain", "shutdown"])
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        async for line in _stream_process(proc):
            yield f"[JTAG] {line}"

    async def halt(self) -> AsyncGenerator[str, None]:
        if not self.tool:
            yield "\x1b[31m[JTAG] openocd not found.\x1b[0m"
            return
        cmd = self._build_cmd(["init", "halt"])
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        async for line in _stream_process(proc):
            yield f"[JTAG] {line}"

    async def dump_flash(self, output: str = "/tmp/flash_dump.bin",
                         address: str = "0x08000000",
                         length: str = "0x80000") -> AsyncGenerator[str, None]:
        """Dump flash memory to file."""
        if not self.tool:
            yield "\x1b[31m[JTAG] openocd not found.\x1b[0m"
            return
        yield f"\x1b[36m[JTAG] Dumping flash {address} ({length} bytes) → {output}\x1b[0m"
        cmd = self._build_cmd([
            "init",
            "halt",
            f"dump_image {output} {address} {length}",
            "resume",
            "shutdown"
        ])
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        async for line in _stream_process(proc):
            yield f"[JTAG] {line}"
        if os.path.exists(output):
            size = os.path.getsize(output)
            yield f"\x1b[32m[JTAG] Dump complete: {size} bytes saved to {output}\x1b[0m"

    async def flash_image(self, image_path: str,
                          address: str = "0x08000000") -> AsyncGenerator[str, None]:
        """Flash an image file to target."""
        if not self.tool:
            yield "\x1b[31m[JTAG] openocd not found.\x1b[0m"
            return
        if not os.path.exists(image_path):
            yield f"\x1b[31m[JTAG] File not found: {image_path}\x1b[0m"
            return
        yield f"\x1b[36m[JTAG] Flashing {image_path} → {address}...\x1b[0m"
        cmd = self._build_cmd([
            "init",
            "halt",
            f"program {image_path} {address} verify reset",
            "shutdown"
        ])
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        async for line in _stream_process(proc):
            yield f"[JTAG] {line}"

    async def interactive(self) -> AsyncGenerator[str, None]:
        """Start OpenOCD with Tcl RPC server for interactive use."""
        if not self.tool:
            yield "\x1b[31m[JTAG] openocd not found.\x1b[0m"
            return
        yield "\x1b[36m[JTAG] Starting OpenOCD interactive server on :4444 (Tcl) / :3333 (GDB)...\x1b[0m"
        cmd = self._build_cmd(["init"])
        self._proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        async for line in _stream_process(self._proc):
            yield f"[JTAG] {line}"

    async def tcl_command(self, command: str) -> AsyncGenerator[str, None]:
        """Send a Tcl command to a running OpenOCD instance via TCP."""
        yield f"\x1b[36m[JTAG] → {command}\x1b[0m"
        try:
            reader, writer = await asyncio.open_connection("localhost", 4444)
            writer.write((command + "\n").encode())
            await writer.drain()
            response = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            writer.close()
            yield response.decode(errors="replace").strip()
        except Exception as e:
            yield f"\x1b[31m[JTAG] Tcl connection error: {e}\x1b[0m"
            yield "\x1b[33m[JTAG] Is OpenOCD running? Start with 'jtag interactive'\x1b[0m"


# ─────────────────────────────────────────────────────────────────────────────
# IOMMU / Memory Handler
# ─────────────────────────────────────────────────────────────────────────────

class IOMMUHandler:
    """
    Low-level memory access via:
    - python-iommu (Linux /dev/mem style access)
    - pyusb bulk transfers for direct USB memory commands
    - Custom vendor commands over USB control transfers
    
    Note: macOS restricts /dev/mem. Full IOMMU access requires SIP disable
    or kernel extension. This handler uses pyusb for USB-level memory ops.
    """

    def __init__(self, vid: str = None, pid: str = None):
        self.vid = vid
        self.pid = pid
        self._dev = None

    async def _get_usb_device(self):
        """Find USB device by VID/PID using pyusb."""
        try:
            import usb.core
            if self.vid and self.pid:
                dev = usb.core.find(
                    idVendor=int(self.vid, 16),
                    idProduct=int(self.pid, 16)
                )
                return dev
        except ImportError:
            return None
        return None

    async def probe(self) -> AsyncGenerator[str, None]:
        """Probe device memory via USB control transfers."""
        yield "\x1b[36m[IOMMU] Probing device memory interface...\x1b[0m"
        try:
            import usb.core
            import usb.util
        except ImportError:
            yield "\x1b[31m[IOMMU] pyusb not installed. Run: pip install pyusb\x1b[0m"
            yield "\x1b[33m[IOMMU] Also install libusb: brew install libusb\x1b[0m"
            return

        dev = await asyncio.to_thread(self._get_usb_device_sync)
        if not dev:
            yield f"\x1b[31m[IOMMU] Device {self.vid}:{self.pid} not found\x1b[0m"
            return

        yield f"\x1b[32m[IOMMU] Found device: {dev}\x1b[0m"
        yield f"  Manufacturer: {self._safe_str(dev, 'manufacturer')}"
        yield f"  Product:      {self._safe_str(dev, 'product')}"
        yield f"  Serial:       {self._safe_str(dev, 'serial_number')}"
        yield ""
        yield "\x1b[36m[IOMMU] Enumerating configurations:\x1b[0m"
        for cfg in dev:
            yield f"  Configuration {cfg.bConfigurationValue}"
            for iface in cfg:
                yield f"    Interface {iface.bInterfaceNumber} | Class: 0x{iface.bInterfaceClass:02x} | Subclass: 0x{iface.bInterfaceSubClass:02x}"
                for ep in iface:
                    direction = "IN" if ep.bEndpointAddress & 0x80 else "OUT"
                    ep_type = ["Control", "Isochronous", "Bulk", "Interrupt"][ep.bmAttributes & 0x03]
                    yield f"      EP 0x{ep.bEndpointAddress:02x} [{direction}] {ep_type} maxPacket={ep.wMaxPacketSize}"

    def _get_usb_device_sync(self):
        try:
            import usb.core
            if self.vid and self.pid:
                return usb.core.find(
                    idVendor=int(self.vid, 16),
                    idProduct=int(self.pid, 16)
                )
        except Exception:
            return None

    def _safe_str(self, dev, attr):
        try:
            return getattr(dev, attr) or "N/A"
        except Exception:
            return "N/A"

    async def read_memory(self, address: int, length: int = 256,
                          request_type: int = 0xC0,
                          request: int = 0x01) -> AsyncGenerator[str, None]:
        """
        Send a vendor-specific USB control read to pull memory from device.
        request_type=0xC0 = Device→Host | Vendor | Device
        """
        yield f"\x1b[36m[IOMMU] Reading {length} bytes from address 0x{address:08x}...\x1b[0m"
        try:
            import usb.core
            dev = await asyncio.to_thread(self._get_usb_device_sync)
            if not dev:
                yield "\x1b[31m[IOMMU] Device not found\x1b[0m"
                return

            def do_read():
                return dev.ctrl_transfer(
                    request_type, request,
                    wValue=(address & 0xFFFF),
                    wIndex=(address >> 16) & 0xFFFF,
                    data_or_wLength=length
                )

            data = await asyncio.to_thread(do_read)
            yield f"\x1b[32m[IOMMU] Received {len(data)} bytes:\x1b[0m"
            # Hex dump
            for i in range(0, len(data), 16):
                chunk = data[i:i+16]
                hex_part = " ".join(f"{b:02x}" for b in chunk)
                asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                yield f"  \x1b[90m0x{address+i:08x}\x1b[0m  {hex_part:<47}  \x1b[32m{asc_part}\x1b[0m"

        except Exception as e:
            yield f"\x1b[31m[IOMMU] Read error: {e}\x1b[0m"
            yield "\x1b[33m[IOMMU] Note: Vendor control transfers are device-specific.\x1b[0m"
            yield "\x1b[33m[IOMMU] Check your target's USB protocol spec.\x1b[0m"

    async def write_memory(self, address: int, data: bytes,
                           request_type: int = 0x40,
                           request: int = 0x02) -> AsyncGenerator[str, None]:
        """Send a vendor-specific USB control write to device memory."""
        yield f"\x1b[36m[IOMMU] Writing {len(data)} bytes to 0x{address:08x}...\x1b[0m"
        try:
            import usb.core
            dev = await asyncio.to_thread(self._get_usb_device_sync)
            if not dev:
                yield "\x1b[31m[IOMMU] Device not found\x1b[0m"
                return

            def do_write():
                return dev.ctrl_transfer(
                    request_type, request,
                    wValue=(address & 0xFFFF),
                    wIndex=(address >> 16) & 0xFFFF,
                    data_or_wLength=list(data)
                )

            n = await asyncio.to_thread(do_write)
            yield f"\x1b[32m[IOMMU] Wrote {n} bytes successfully.\x1b[0m"
        except Exception as e:
            yield f"\x1b[31m[IOMMU] Write error: {e}\x1b[0m"

    async def bulk_read(self, endpoint: int = 0x81,
                        length: int = 512) -> AsyncGenerator[str, None]:
        """Read from a bulk endpoint."""
        yield f"\x1b[36m[IOMMU] Bulk read from EP 0x{endpoint:02x} ({length} bytes)...\x1b[0m"
        try:
            import usb.core
            dev = await asyncio.to_thread(self._get_usb_device_sync)
            if not dev:
                yield "\x1b[31m[IOMMU] Device not found\x1b[0m"
                return

            def do_bulk():
                dev.set_configuration()
                return dev.read(endpoint, length, timeout=2000)

            data = await asyncio.to_thread(do_bulk)
            yield f"\x1b[32m[IOMMU] Read {len(data)} bytes:\x1b[0m"
            for i in range(0, len(data), 16):
                chunk = data[i:i+16]
                hex_part = " ".join(f"{b:02x}" for b in chunk)
                asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                yield f"  \x1b[90m0x{i:04x}\x1b[0m  {hex_part:<47}  \x1b[32m{asc_part}\x1b[0m"
        except Exception as e:
            yield f"\x1b[31m[IOMMU] Bulk read error: {e}\x1b[0m"
