# Voice Threshold Overlay

A small Windows desktop utility that shows live microphone input volume in a floating, always-on-top overlay above the taskbar.

The overlay displays a 1-100 microphone level bar, a configurable threshold marker, and a subtle screen flash when the microphone level exceeds the threshold.

## Features

- Real-time microphone input monitoring
- Floating transparent overlay
- Always-on-top Windows tool window
- Hidden from Alt+Tab where Windows allows it
- Drag to move the overlay
- Right-click drag to resize the overlay
- Saved overlay position and size
- Adjustable threshold from 1 to 100
- Microphone selection with duplicate Windows audio backend filtering
- Saved selected microphone
- System tray icon with:
  - Show Settings
  - Show Indicator
  - Hide Indicator
  - Exit
- Config saved under the user's AppData folder

## Requirements

- Windows
- Python

Install dependencies:

```powershell
py -m pip install sounddevice numpy pystray pillow
```

## Run

```powershell
py voice_threshold_overlay.py
```

## Controls

- Left-click drag: move the overlay
- Right-click drag: resize the overlay
- Right-click: open settings
- Double-click: open settings

## Settings

The settings window lets you select the microphone, refresh the device list, adjust the warning threshold, and view the current detected level.

`sounddevice` may report the same physical microphone through multiple Windows audio backends. The app prefers Windows WASAPI inputs and hides duplicate backend entries when possible.

## Configuration

Settings are saved to:

```text
%APPDATA%\Voice Threshold Overlay\config.json
```

The config stores overlay position, overlay size, overlay visibility, threshold, and selected microphone details.
