# SIMINPUT Configurator

Configure your button box from your desktop.

The SIMINPUT Configurator is the companion app for [SIMINPUT](https://siminput.com) controllers. Connect over USB, tweak your buttons, axes, and rules, watch your inputs live, and flash firmware updates. All from one window, no drivers needed.

SIMINPUT makes fully modular button boxes for sim racing and flight simulation. The controllers are RP2040-based (Raspberry Pi Pico) running CircuitPython, and present as a standard USB HID joystick with 128 buttons and 8 axes. Firmware, hardware, enclosure CAD, and PCB are all public. Inspect it, fork it, or build your own.

## Download

Grab the latest build from the [Releases](https://github.com/Dirt-m/siminput-firmware-updater/releases) page and pick the file for your operating system. It's a single file, no installation required.

**Windows:** download `siminput-updater-windows.exe` and double-click to run.

**Linux:** download `siminput-updater-linux`, then make it executable and run it.

```bash
chmod +x siminput-updater-linux
./siminput-updater-linux
```

### Linux notes

Serial access needs your user in the `dialout` group:

```bash
sudo usermod -aG dialout $USER   # log out and back in afterwards
```

If you hit a tkinter error, install it for your distro:

```bash
sudo dnf install python3-tkinter   # Fedora
sudo apt install python3-tk        # Ubuntu / Debian
```

## What it does

**Finds your controller automatically.** Discovery detects SIMINPUT boards by USB vendor ID and connects on its own.

**Shows your inputs live.** A real-time monitor draws every button and axis at 200 Hz, so you can see exactly what the box is sending.

**Edits your config visually.** Device settings, boolean variables, axes, and input rules, all in a tabbed editor with a drag-to-reorder rule builder.

**Runs a real rule engine.** Seven rule types cover the common cases: Direct Map, All-Off Detector, Toggle Switch, Timed Pulse, Rotary Encoder, Increase Axis, and Decrease Axis. Rules can read the outputs of earlier rules in the same cycle.

**Flashes firmware safely.** Upload firmware packages (`.zip`) with chunked transfer, SHA-256 verification, and live progress.

**Works without hardware.** Run with `--mock` to explore and develop against a simulated device.

## Development

Built with [customtkinter](https://github.com/TomSchimansky/CustomTkinter) and [uv](https://github.com/astral-sh/uv). Python 3.12 or newer.

```bash
uv sync                          # install dependencies
uv run siminput-updater          # run against a real device
uv run siminput-updater --mock   # run against a simulated device
```

### Options

| Flag | What it does |
|------|--------------|
| `--mock` | Use a simulated device. Also settable via `SIMINPUT_MOCK=1`. |
| `--scale <factor>` | Override the UI scale factor, e.g. `1.5` for Linux HiDPI. Also settable via `SIMINPUT_SCALE`. |

### Building an executable

```bash
uv run pyinstaller siminput-updater.spec
```

CI builds Windows and Linux binaries automatically on every `v*` tag.

## How it's built

Three layers, kept separate:

**Data** (`config_model.py`): pure dataclasses for the config, with JSON round-trip and validation that reports dotted paths like `rules[3].inputs`. No UI or serial imports.

**Device** (`device.py`, `mock_device.py`): a JSON-line client over USB CDC serial. The real device and the mock share one interface. On Linux, live input monitoring reads HID state through evdev rather than the serial link, since serial is reserved for config and firmware commands.

**UI** (`app.py`, `pages/`, `widgets/`): the customtkinter front end, split into a device page, a configure page, and an update page. Serial work runs on background threads so the window stays responsive.

## Protocol

The app talks to controllers over a JSON-line serial protocol.

- Ports are filtered by Adafruit vendor ID (`0x239A`) and deduped by serial number.
- Payloads over 3 KB use chunked base64 transfer with per-chunk acks.
- Every file write is verified with a SHA-256 checksum.
- Firmware packages are zips with a `manifest.json`. Upload order is lib files, then `boot.py`, then `code.py` last, since `code.py` is what runs.

## License

Released under the [MIT License](LICENSE). Open source, like the rest of SIMINPUT.
