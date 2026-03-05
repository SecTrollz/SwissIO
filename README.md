# USB Nexus

**Universal USB-C Device Terminal** — plug in anything, get a terminal.

## What It Does

Connects to any USB-C device, auto-detects its capabilities, and provides:
- **Full display output** if the device supports DisplayPort Alt Mode
- **Terminal session** (browser-based) for everything else
- Supports DFU flashing, UART serial, JTAG/SWD, and raw USB memory access

## Quick Start (macOS)

```bash
chmod +x run.sh
./run.sh
# Opens http://localhost:8765 automatically
```

## Architecture

```
usb_nexus/
├── app.py              FastAPI backend + WebSocket terminal bridge
├── detector.py         USB enumeration, alt-mode detection, board fingerprinting
├── protocols/
│   └── __init__.py     DFUHandler / UARTHandler / OpenOCDHandler / IOMMUHandler
├── static/
│   └── index.html      Browser terminal UI (xterm.js + WebSocket)
└── run.sh              macOS setup + launch script
```

## System Dependencies (macOS)

```bash
brew install libusb open-ocd dfu-util
pip install fastapi uvicorn pyserial pyusb
```

## Terminal Commands

### Device Detection
| Command | Description |
|---------|-------------|
| `scan`  | Enumerate all connected USB devices |
| `ports` | List available serial ports |

### DFU (Device Firmware Upgrade)
| Command | Description |
|---------|-------------|
| `dfu list` | List DFU-capable devices |
| `dfu read [/path/out.bin]` | Read firmware from device |
| `dfu flash <firmware.bin>` | Flash firmware to device |
| `dfu detach` | Send DFU_DETACH command |

### UART / Serial
| Command | Description |
|---------|-------------|
| `uart open <port> [baud]` | Open serial connection (default 115200) |
| `uart baud <port>` | Auto-detect baud rate |
| `uart close` | Close active serial connection |

### JTAG / SWD (OpenOCD)
| Command | Description |
|---------|-------------|
| `jtag probe [iface] [target]` | Probe JTAG/SWD chain |
| `jtag halt` | Halt target CPU |
| `jtag dump [file] [addr] [len]` | Dump flash memory |
| `jtag flash <file> [addr]` | Flash binary image |
| `jtag server` | Start OpenOCD interactive server |
| `jtag cmd <tcl>` | Send Tcl command to running OpenOCD |

Supported interfaces: `stlink`, `jlink`, `ftdi`, `cmsis`, `picoprobe`  
Supported targets: `STM32`, `nRF52`, `RP2040`, `ESP32`, `Atmel SAM`

### Memory / IOMMU
| Command | Description |
|---------|-------------|
| `mem probe [vid] [pid]` | Enumerate USB device endpoints |
| `mem read <addr_hex> [len_hex]` | Vendor control transfer read |
| `mem write <addr_hex> <data_hex>` | Vendor control transfer write |
| `mem bulk [ep_hex] [len]` | Bulk endpoint read |

## Board Detection (Auto-Fingerprinted)

| VID:PID | Board |
|---------|-------|
| 0483:df11 | STM32 (DFU mode) |
| 0483:374b | STM32 + ST-Link v2 |
| 1915:521f | Nordic nRF52840 DFU |
| 303a:1001 | Espressif ESP32-S3 |
| 2e8a:0003 | Raspberry Pi RP2040 |
| 03eb:2ff4 | Atmel AVR DFU |
| 1a86:7523 | CH340 (common ESP32 UART bridge) |
| 10c4:ea60 | CP2102 (Silicon Labs UART bridge) |
| 0403:6001 | FTDI FT232 |

## Example: Vape from the Gas Station

```
nexus> scan
Found 1 device:
  Shenzhen Generic — USB SERIAL (CH340)
    VID:PID  1a86:7523
    Family   ESP32
    Mode     serial_cdc
    Port     /dev/tty.wchusbserial110

nexus> uart open /dev/tty.wchusbserial110 115200
[UART] Opening /dev/tty.wchusbserial110 @ 115200 baud...
[UART] Connected.
ets Jun  8 2016 00:22:57
rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)
...

nexus> uart close
nexus> mem probe 1a86 7523
[IOMMU] Found device: DEVICE ID 1A86:7523...
  Configuration 1
    Interface 0 | Class: 0xff | Subclass: 0x01
      EP 0x02 [OUT] Bulk maxPacket=32
      EP 0x82 [IN]  Bulk maxPacket=32
```

## WebSocket API

**Device events**: `ws://localhost:8765/ws/devices`  
**Terminal sessions**: `ws://localhost:8765/ws/terminal/{session_id}`

Messages follow `{ "type": "input"|"output"|"devices", "data": ... }` format.

## macOS Notes

- libusb is required for pyusb (`brew install libusb`)  
- Full IOMMU/`/dev/mem` access is blocked by macOS SIP — use USB control transfers instead  
- OpenOCD requires target-specific interface and target configs; edit `OPENOCD_TARGET_MAP` in `protocols/__init__.py`  
- For ST-Link access, macOS may need the FTDI kernel extension workaround
