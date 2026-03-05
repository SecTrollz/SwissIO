"""
USB Nexus - Device Detection & Fingerprinting Engine
Supports macOS via system_profiler + libusb (pyusb)
"""

import asyncio
import subprocess
import json
import re
import os
import glob
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class ConnectionMode(Enum):
    DISPLAY_PORT  = "displayport"
    THUNDERBOLT   = "thunderbolt"
    USB3          = "usb3"
    USB2          = "usb2"
    SERIAL_CDC    = "serial_cdc"
    SERIAL_UART   = "serial_uart"
    DFU           = "dfu"
    UNKNOWN       = "unknown"


class BoardFamily(Enum):
    STM32         = "STM32"
    NRF52         = "nRF52"
    ESP32         = "ESP32"
    RP2040        = "RP2040"
    ATMEL_AVR     = "Atmel AVR"
    ATMEL_SAM     = "Atmel SAM"
    CYPRESS_PSoC  = "Cypress PSoC"
    NORDIC        = "Nordic"
    SILABS        = "Silicon Labs"
    GENERIC       = "Generic MCU"
    UNKNOWN       = "Unknown"


@dataclass
class USBDevice:
    vendor_id: str = "0000"
    product_id: str = "0000"
    vendor_name: str = "Unknown"
    product_name: str = "Unknown"
    serial_number: str = ""
    usb_version: str = "2.0"
    speed: str = "Unknown"
    location: str = ""
    # Detected capabilities
    connection_mode: ConnectionMode = ConnectionMode.UNKNOWN
    board_family: BoardFamily = BoardFamily.UNKNOWN
    serial_port: Optional[str] = None
    supports_dfu: bool = False
    supports_jtag: bool = False
    supports_display: bool = False
    extra: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "vendor_id": self.vendor_id,
            "product_id": self.product_id,
            "vendor_name": self.vendor_name,
            "product_name": self.product_name,
            "serial_number": self.serial_number,
            "usb_version": self.usb_version,
            "speed": self.speed,
            "location": self.location,
            "connection_mode": self.connection_mode.value,
            "board_family": self.board_family.value,
            "serial_port": self.serial_port,
            "supports_dfu": self.supports_dfu,
            "supports_jtag": self.supports_jtag,
            "supports_display": self.supports_display,
            "extra": self.extra,
        }


# VID:PID → board family mappings
BOARD_FINGERPRINTS = {
    # STM32 DFU
    ("0483", "df11"): (BoardFamily.STM32, True, False),
    ("0483", "5740"): (BoardFamily.STM32, False, False),   # STM32 VCP
    ("0483", "374b"): (BoardFamily.STM32, False, True),    # ST-Link v2
    ("0483", "3748"): (BoardFamily.STM32, False, True),    # ST-Link v1
    # Nordic nRF
    ("1915", "521f"): (BoardFamily.NRF52, True, False),    # nRF52840 DFU
    ("1915", "c00a"): (BoardFamily.NRF52, False, False),
    ("1366", "0101"): (BoardFamily.NRF52, False, True),    # J-Link
    # ESP32
    ("10c4", "ea60"): (BoardFamily.ESP32, False, False),   # CP2102 (common ESP32 UART)
    ("1a86", "7523"): (BoardFamily.ESP32, False, False),   # CH340 (common cheap boards)
    ("1a86", "55d4"): (BoardFamily.ESP32, False, False),   # CH9102
    ("303a", "1001"): (BoardFamily.ESP32, True, False),    # ESP32-S3 native USB
    ("303a", "0002"): (BoardFamily.ESP32, True, False),    # ESP32-S2
    # RP2040
    ("2e8a", "0003"): (BoardFamily.RP2040, True, False),   # RP2040 MSD Boot
    ("2e8a", "000a"): (BoardFamily.RP2040, False, False),  # Pico SDK CDC
    # Atmel/Microchip AVR
    ("03eb", "2ff4"): (BoardFamily.ATMEL_AVR, True, False),# ATmega DFU
    ("03eb", "2fe4"): (BoardFamily.ATMEL_AVR, True, False),
    ("03eb", "2104"): (BoardFamily.ATMEL_SAM, True, False),# ATSAM DFU
    ("03eb", "6124"): (BoardFamily.ATMEL_SAM, True, False),
    # FTDI serial bridges (common on dev boards)
    ("0403", "6001"): (BoardFamily.GENERIC, False, False),
    ("0403", "6010"): (BoardFamily.GENERIC, False, False),
    ("0403", "6011"): (BoardFamily.GENERIC, False, False),
    # Cypress/Infineon PSoC
    ("04b4", "f13b"): (BoardFamily.CYPRESS_PSoC, False, True),
    ("04b4", "0007"): (BoardFamily.CYPRESS_PSoC, True, False),
    # Silicon Labs
    ("10c4", "ea61"): (BoardFamily.SILABS, False, False),
}

# Vendor ID → name
VENDOR_NAMES = {
    "0483": "STMicroelectronics",
    "1915": "Nordic Semiconductor",
    "1366": "SEGGER (J-Link)",
    "303a": "Espressif Systems",
    "2e8a": "Raspberry Pi",
    "03eb": "Microchip/Atmel",
    "10c4": "Silicon Laboratories",
    "1a86": "QinHeng Electronics (CH34x)",
    "0403": "FTDI",
    "04b4": "Cypress Semiconductor",
    "05ac": "Apple",
    "18d1": "Google",
    "2109": "VIA Labs (USB Hub)",
    "0bda": "Realtek",
}


def _parse_system_profiler_usb() -> list[dict]:
    """Run system_profiler and parse USB device list."""
    try:
        result = subprocess.run(
            ["system_profiler", "SPUSBDataType", "-json"],
            capture_output=True, text=True, timeout=10
        )
        data = json.loads(result.stdout)
        devices = []
        
        def walk(node):
            if isinstance(node, list):
                for item in node:
                    walk(item)
            elif isinstance(node, dict):
                if "vendor_id" in node or "product_id" in node:
                    devices.append(node)
                for v in node.values():
                    walk(v)

        walk(data)
        return devices
    except Exception:
        return []


def _find_serial_ports() -> list[str]:
    """Find USB serial ports on macOS."""
    patterns = [
        "/dev/tty.usbmodem*",
        "/dev/tty.usbserial*",
        "/dev/tty.SLAB_USBtoUART*",
        "/dev/tty.wchusbserial*",
        "/dev/cu.usbmodem*",
        "/dev/cu.usbserial*",
    ]
    ports = []
    for p in patterns:
        ports.extend(glob.glob(p))
    return sorted(set(ports))


def _detect_connection_mode(dev: dict, has_serial: bool) -> ConnectionMode:
    """Heuristically determine how this device is best communicated with."""
    name_lower = (dev.get("_name", "") + " " + dev.get("manufacturer", "")).lower()
    speed = dev.get("speed", "").lower()

    # Thunderbolt
    if "thunderbolt" in name_lower:
        return ConnectionMode.THUNDERBOLT

    # DisplayPort alt-mode (rare to detect via USB profiler, but try)
    if "displayport" in name_lower or "display" in name_lower:
        return ConnectionMode.DISPLAY_PORT

    # DFU class - detected from class codes or known VID/PIDs
    if "dfu" in name_lower:
        return ConnectionMode.DFU

    # CDC serial
    if has_serial or "modem" in name_lower or "serial" in name_lower:
        return ConnectionMode.SERIAL_CDC

    # Speed-based fallback
    if "super" in speed or "5 gbit" in speed or "10 gbit" in speed:
        return ConnectionMode.USB3

    return ConnectionMode.USB2


def _clean_hex_id(raw: str) -> str:
    """Normalize '0x0483' or '0483' → '0483'."""
    if not raw:
        return "0000"
    return raw.lower().replace("0x", "").zfill(4)


async def enumerate_devices() -> list[USBDevice]:
    """Main enumeration — returns list of detected USB devices."""
    raw_devices = await asyncio.to_thread(_parse_system_profiler_usb)
    serial_ports = await asyncio.to_thread(_find_serial_ports)
    results = []

    for raw in raw_devices:
        vid = _clean_hex_id(raw.get("vendor_id", "0000"))
        pid = _clean_hex_id(raw.get("product_id", "0000"))

        dev = USBDevice(
            vendor_id=vid,
            product_id=pid,
            vendor_name=VENDOR_NAMES.get(vid, raw.get("manufacturer", "Unknown Vendor")),
            product_name=raw.get("_name", "Unknown Device"),
            serial_number=raw.get("serial_num", ""),
            usb_version=raw.get("bcd_usb", "2.0"),
            speed=raw.get("speed", "Unknown"),
            location=raw.get("location_id", ""),
        )

        # Fingerprint board family
        key = (vid, pid)
        if key in BOARD_FINGERPRINTS:
            family, dfu, jtag = BOARD_FINGERPRINTS[key]
            dev.board_family = family
            dev.supports_dfu = dfu
            dev.supports_jtag = jtag

        # Match serial ports to this device
        for port in serial_ports:
            # Best effort: match by modem/serial patterns
            dev.serial_port = port  # simplified; real impl would match location IDs
            dev.connection_mode = ConnectionMode.SERIAL_CDC
            break

        dev.connection_mode = _detect_connection_mode(raw, dev.serial_port is not None)
        results.append(dev)

    return results


async def watch_devices(callback):
    """Poll for device changes and fire callback with updated list."""
    last_set = set()
    while True:
        devices = await enumerate_devices()
        current_set = {(d.vendor_id, d.product_id, d.location) for d in devices}
        if current_set != last_set:
            last_set = current_set
            await callback(devices)
        await asyncio.sleep(2)
