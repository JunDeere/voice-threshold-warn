import ctypes
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

import numpy as np
import pystray
import sounddevice as sd
from PIL import Image, ImageDraw


APP_NAME = "Voice Threshold Overlay"
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_CONFIG = {
    "overlay": {"x": 120, "y": 680, "width": 360, "height": 54, "visible": True},
    "threshold": 65,
    "device_name": "",
    "device_index": None,
}

GWL_EXSTYLE = -20
HWND_TOPMOST = -1
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config.update({k: v for k, v in data.items() if k != "overlay"})
    config["overlay"].update(data.get("overlay", {}))
    return config


def save_config(config):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp_path, CONFIG_PATH)


def hide_from_alt_tab(window):
    """Mark a Tk toplevel as a tool window so Windows keeps it out of Alt+Tab."""
    if sys.platform != "win32":
        return

    window.update_idletasks()
    hwnd = int(window.winfo_id())
    user32 = ctypes.windll.user32
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style &= ~WS_EX_APPWINDOW
    style |= WS_EX_TOOLWINDOW
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    user32.SetWindowPos(
        hwnd,
        HWND_TOPMOST,
        0,
        0,
        0,
        0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW,
    )


def force_topmost(window):
    if sys.platform != "win32":
        window.attributes("-topmost", True)
        return

    try:
        hwnd = int(window.winfo_id())
    except tk.TclError:
        return

    ctypes.windll.user32.SetWindowPos(
        hwnd,
        HWND_TOPMOST,
        0,
        0,
        0,
        0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
    )


class AudioMonitor:
    def __init__(self, on_error):
        self.on_error = on_error
        self.stream = None
        self.level = 1
        self.lock = threading.Lock()
        self.device_index = None

    def input_devices(self):
        hostapis = sd.query_hostapis()
        raw_devices = []

        for index, info in enumerate(sd.query_devices()):
            if info.get("max_input_channels", 0) > 0:
                name = info["name"]
                api_name = hostapis[info["hostapi"]]["name"]
                if name.startswith(("Microsoft Sound Mapper", "Primary Sound Capture Driver")):
                    continue
                raw_devices.append((index, name, api_name))

        # PortAudio exposes the same physical Windows device through several
        # host APIs. Prefer WASAPI so the settings list stays human-sized.
        wasapi_devices = [(index, name) for index, name, api in raw_devices if api == "Windows WASAPI"]
        if wasapi_devices:
            return sorted(wasapi_devices, key=lambda item: item[1].lower())

        api_rank = {"Windows DirectSound": 0, "MME": 1, "Windows WDM-KS": 2}
        unique = {}
        for index, name, api in raw_devices:
            key = self.clean_device_name(name).lower()
            rank = api_rank.get(api, 99)
            if key not in unique or rank < unique[key][0]:
                unique[key] = (rank, index, self.clean_device_name(name))

        return sorted(((index, name) for _, index, name in unique.values()), key=lambda item: item[1].lower())

    @staticmethod
    def clean_device_name(name):
        if name.startswith("Headset (@System32"):
            return "Headset"
        return name.strip()
        return devices

    def choose_device(self, saved_name, saved_index=None):
        try:
            devices = self.input_devices()
        except Exception as exc:
            self.on_error(f"Could not list microphones: {exc}")
            return None

        visible_indexes = {index for index, _ in devices}
        if saved_index is not None:
            try:
                saved_index = int(saved_index)
                if saved_index in visible_indexes:
                    return saved_index
            except Exception:
                pass

        if saved_name:
            for index, name in devices:
                if name == saved_name:
                    return index
        return devices[0][0] if devices else None

    def start(self, device_index=None):
        self.stop()
        self.device_index = device_index
        if device_index is None:
            self.on_error("No microphone input device found.")
            return

        try:
            info = sd.query_devices(device_index, "input")
            samplerate = int(info["default_samplerate"])
            self.stream = sd.InputStream(
                device=device_index,
                channels=1,
                samplerate=samplerate,
                blocksize=1024,
                callback=self._callback,
            )
            self.stream.start()
            self.on_error("")
        except Exception as exc:
            self.stream = None
            with self.lock:
                self.level = 1
            self.on_error(f"Microphone error: {exc}")

    def stop(self):
        if self.stream is None:
            return
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass
        self.stream = None

    def _callback(self, indata, frames, time_info, status):
        if status:
            self.on_error(str(status))

        samples = np.asarray(indata, dtype=np.float32)
        rms = float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0

        # Typical microphone RMS values are small. This scaling gives a useful
        # 1-100 display without requiring per-device calibration.
        level = int(max(1, min(100, round(rms * 420))))
        with self.lock:
            self.level = level

    def current_level(self):
        with self.lock:
            return self.level


class VoiceThresholdOverlay:
    def __init__(self):
        self.config = load_config()
        self.ui_queue = queue.Queue()
        self.last_flash = 0.0
        self.flash_window = None
        self.drag_start = None
        self.resize_start = None
        self.right_dragged = False
        self.tray_icon = None

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.88)
        self.root.configure(bg="#151515")
        self.root.protocol("WM_DELETE_WINDOW", self.hide_indicator)

        overlay = self.config["overlay"]
        self.root.geometry(f'{overlay["width"]}x{overlay["height"]}+{overlay["x"]}+{overlay["y"]}')

        self.canvas = tk.Canvas(self.root, highlightthickness=0, bg="#151515")
        self.canvas.pack(fill="both", expand=True)

        self.canvas.bind("<ButtonPress-1>", self.begin_move)
        self.canvas.bind("<B1-Motion>", self.move_overlay)
        self.canvas.bind("<ButtonRelease-1>", lambda event: self.save_overlay_geometry())
        self.canvas.bind("<ButtonPress-3>", self.begin_resize)
        self.canvas.bind("<B3-Motion>", self.resize_overlay)
        self.canvas.bind("<ButtonRelease-3>", self.finish_right_click)
        self.canvas.bind("<Double-Button-1>", lambda event: self.show_settings())
        self.canvas.bind("<Double-Button-3>", lambda event: self.show_settings())
        self.root.bind("<Configure>", lambda event: self.draw_overlay())

        hide_from_alt_tab(self.root)
        if not overlay.get("visible", True):
            self.root.withdraw()

        self.threshold_var = tk.IntVar(value=int(self.config.get("threshold", 65)))
        self.level_var = tk.StringVar(value="1")
        self.device_var = tk.StringVar(value="")
        self.error_var = tk.StringVar(value="")

        self.audio = AudioMonitor(self.set_audio_error)
        self.devices = []
        self.settings = None
        self.device_combo = None

        self.refresh_devices()
        selected_index = self.audio.choose_device(
            self.config.get("device_name", ""),
            self.config.get("device_index"),
        )
        if selected_index is not None:
            self.device_var.set(self.device_display_name(selected_index))
        self.audio.start(selected_index)

        self.start_tray_icon()
        self.root.after(30, self.update_loop)
        self.root.after(500, self.keep_overlay_above_taskbar)
        self.root.after(250, self.process_ui_queue)

    def set_audio_error(self, message):
        self.ui_queue.put(("error", message))

    def call_ui(self, func, *args):
        self.ui_queue.put(("call", func, args))

    def process_ui_queue(self):
        while True:
            try:
                item = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            if item[0] == "error":
                self.error_var.set(item[1])
            elif item[0] == "call":
                _, func, args = item
                func(*args)

        self.root.after(100, self.process_ui_queue)

    def refresh_devices(self):
        try:
            self.devices = self.audio.input_devices()
        except Exception as exc:
            self.devices = []
            self.error_var.set(f"Could not list microphones: {exc}")

        if self.device_combo is not None and self.device_combo.winfo_exists():
            values = [self.device_display_name(index) for index, _ in self.devices]
            self.device_combo["values"] = values
            if self.device_var.get() not in values:
                current_index = self.audio.device_index
                current_display = self.device_display_name(current_index) if current_index is not None else ""
                self.device_var.set(current_display if current_display in values else (values[0] if values else ""))

    def device_display_name(self, index):
        for device_index, name in self.devices:
            if device_index == index:
                return f"{name}  [{device_index}]"
        return ""

    def parse_device_index(self, display):
        if "[" not in display or not display.endswith("]"):
            return None
        try:
            return int(display.rsplit("[", 1)[1][:-1])
        except ValueError:
            return None

    def update_loop(self):
        level = self.audio.current_level()
        threshold = self.threshold_var.get()
        self.level_var.set(str(level))
        self.draw_overlay()

        if level >= threshold:
            now = time.monotonic()
            if now - self.last_flash > 0.85:
                self.last_flash = now
                self.flash_screen()

        self.root.after(33, self.update_loop)

    def keep_overlay_above_taskbar(self):
        if self.root.winfo_exists() and self.root.winfo_viewable():
            self.root.attributes("-topmost", True)
            force_topmost(self.root)
        self.root.after(500, self.keep_overlay_above_taskbar)

    def draw_overlay(self):
        if not self.root.winfo_viewable():
            return

        width = max(160, self.canvas.winfo_width())
        height = max(36, self.canvas.winfo_height())
        level = self.audio.current_level()
        threshold = self.threshold_var.get()

        pad = 10
        bar_x = pad
        bar_y = max(10, height // 2 - 9)
        bar_w = max(1, width - pad * 2)
        bar_h = 18
        fill_w = int(bar_w * (level / 100))
        threshold_x = bar_x + int(bar_w * (threshold / 100))

        if level >= threshold:
            color = "#e84141"
        elif level >= max(1, threshold - 12):
            color = "#d8b33f"
        else:
            color = "#39b36b"

        self.canvas.delete("all")
        self.canvas.create_rectangle(0, 0, width, height, fill="#151515", outline="")
        self.canvas.create_rectangle(bar_x, bar_y, bar_x + bar_w, bar_y + bar_h, fill="#303030", outline="#5b5b5b")
        self.canvas.create_rectangle(bar_x, bar_y, bar_x + fill_w, bar_y + bar_h, fill=color, outline="")
        self.canvas.create_line(threshold_x, bar_y - 4, threshold_x, bar_y + bar_h + 4, fill="#ffffff", width=2)
        self.canvas.create_text(
            width // 2,
            max(10, bar_y - 8),
            text=f"Mic {level:03d} / Threshold {threshold:03d}",
            fill="#f2f2f2",
            font=("Segoe UI", 9, "bold"),
        )
        self.canvas.create_polygon(
            width - 14,
            height - 5,
            width - 5,
            height - 5,
            width - 5,
            height - 14,
            fill="#707070",
            outline="",
        )

    def begin_move(self, event):
        self.drag_start = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def move_overlay(self, event):
        if self.drag_start is None:
            return
        start_x, start_y, win_x, win_y = self.drag_start
        self.root.geometry(f"+{win_x + event.x_root - start_x}+{win_y + event.y_root - start_y}")

    def begin_resize(self, event):
        self.right_dragged = False
        self.resize_start = (
            event.x_root,
            event.y_root,
            self.root.winfo_width(),
            self.root.winfo_height(),
        )

    def resize_overlay(self, event):
        if self.resize_start is None:
            return
        start_x, start_y, start_w, start_h = self.resize_start
        dx = event.x_root - start_x
        dy = event.y_root - start_y
        if abs(dx) > 3 or abs(dy) > 3:
            self.right_dragged = True
        width = max(180, start_w + dx)
        height = max(42, start_h + dy)
        self.root.geometry(f"{width}x{height}")

    def finish_right_click(self, event):
        self.save_overlay_geometry()
        if not self.right_dragged:
            self.show_settings()
        self.resize_start = None

    def save_overlay_geometry(self):
        if not self.root.winfo_exists():
            return
        self.config["overlay"].update(
            {
                "x": self.root.winfo_x(),
                "y": self.root.winfo_y(),
                "width": max(1, self.root.winfo_width()),
                "height": max(1, self.root.winfo_height()),
            }
        )
        self.save_current_config()

    def save_current_config(self):
        self.config["threshold"] = int(self.threshold_var.get())
        device_index = self.parse_device_index(self.device_var.get())
        if device_index is not None:
            try:
                self.config["device_index"] = device_index
                self.config["device_name"] = sd.query_devices(device_index)["name"]
            except Exception:
                pass
        save_config(self.config)

    def flash_screen(self):
        if self.flash_window is not None and self.flash_window.winfo_exists():
            return

        flash = tk.Toplevel(self.root)
        self.flash_window = flash
        flash.overrideredirect(True)
        flash.attributes("-topmost", True)
        flash.attributes("-alpha", 0.16)
        flash.configure(bg="#ff3333")
        flash.geometry(
            f"{flash.winfo_screenwidth()}x{flash.winfo_screenheight()}+0+0"
        )
        hide_from_alt_tab(flash)
        flash.after(130, self.destroy_flash)

    def destroy_flash(self):
        if self.flash_window is not None:
            try:
                self.flash_window.destroy()
            except tk.TclError:
                pass
            self.flash_window = None

    def show_settings(self):
        if self.settings is None or not self.settings.winfo_exists():
            self.create_settings()

        self.settings.deiconify()
        self.settings.lift()
        self.settings.attributes("-topmost", True)
        self.settings.after(200, lambda: self.settings.attributes("-topmost", False))
        hide_from_alt_tab(self.settings)

    def create_settings(self):
        self.settings = tk.Toplevel(self.root)
        self.settings.title(APP_NAME + " Settings")
        self.settings.geometry("440x320")
        self.settings.resizable(False, False)
        self.settings.protocol("WM_DELETE_WINDOW", self.settings.withdraw)

        frame = ttk.Frame(self.settings, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Microphone").grid(row=0, column=0, sticky="w")
        ttk.Button(frame, text="Refresh", command=self.refresh_microphones).grid(row=0, column=1, sticky="e")
        self.device_combo = ttk.Combobox(frame, textvariable=self.device_var, state="readonly")
        self.device_combo.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 12))
        self.device_combo["values"] = [self.device_display_name(index) for index, _ in self.devices]
        self.device_combo.bind("<<ComboboxSelected>>", self.change_device)

        ttk.Label(frame, text="Threshold").grid(row=2, column=0, sticky="w")
        threshold_label = ttk.Label(frame, textvariable=self.threshold_var, width=4)
        threshold_label.grid(row=2, column=1, sticky="e")

        slider = ttk.Scale(
            frame,
            from_=1,
            to=100,
            variable=self.threshold_var,
            command=lambda value: self.threshold_changed(value),
        )
        slider.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 14))

        ttk.Label(frame, text="Current level").grid(row=4, column=0, sticky="w")
        ttk.Label(frame, textvariable=self.level_var, font=("Segoe UI", 18, "bold")).grid(
            row=5, column=0, sticky="w", pady=(2, 12)
        )

        ttk.Label(
            frame,
            text=(
                "Overlay controls:\n"
                "Left-click drag moves the indicator.\n"
                "Right-click drag resizes it.\n"
                "Double-click or right-click opens this panel."
            ),
            justify="left",
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 12))

        error = ttk.Label(frame, textvariable=self.error_var, foreground="#b00020", wraplength=380)
        error.grid(row=7, column=0, columnspan=2, sticky="ew")

        frame.columnconfigure(0, weight=1)
        hide_from_alt_tab(self.settings)

    def threshold_changed(self, value):
        self.threshold_var.set(max(1, min(100, int(float(value)))))
        self.save_current_config()

    def refresh_microphones(self):
        previous = self.device_var.get()
        self.refresh_devices()
        values = list(self.device_combo["values"]) if self.device_combo is not None else []
        if previous in values:
            self.device_var.set(previous)
        elif values:
            self.device_var.set(values[0])
            self.change_device()

    def change_device(self, event=None):
        device_index = self.parse_device_index(self.device_var.get())
        self.audio.start(device_index)
        self.save_current_config()

    def show_indicator(self):
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        hide_from_alt_tab(self.root)
        self.config["overlay"]["visible"] = True
        self.save_current_config()

    def hide_indicator(self):
        self.save_overlay_geometry()
        self.root.withdraw()
        self.config["overlay"]["visible"] = False
        self.save_current_config()

    def start_tray_icon(self):
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((8, 18, 56, 46), radius=8, fill=(35, 35, 35, 255))
        draw.rectangle((14, 25, 41, 39), fill=(57, 179, 107, 255))
        draw.line((45, 21, 45, 43), fill=(255, 255, 255, 255), width=3)

        menu = pystray.Menu(
            pystray.MenuItem("Show Settings", lambda icon, item: self.call_ui(self.show_settings)),
            pystray.MenuItem("Show Indicator", lambda icon, item: self.call_ui(self.show_indicator)),
            pystray.MenuItem("Hide Indicator", lambda icon, item: self.call_ui(self.hide_indicator)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", lambda icon, item: self.call_ui(self.exit_app)),
        )
        self.tray_icon = pystray.Icon("voice_threshold_overlay", image, APP_NAME, menu)
        thread = threading.Thread(target=self.tray_icon.run, daemon=True)
        thread.start()

    def exit_app(self):
        self.save_overlay_geometry()
        self.save_current_config()
        self.audio.stop()

        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass

        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self):
        try:
            self.root.mainloop()
        finally:
            self.audio.stop()
            if self.tray_icon is not None:
                try:
                    self.tray_icon.stop()
                except Exception:
                    pass


if __name__ == "__main__":
    if sys.platform != "win32":
        print("Voice Threshold Overlay is intended for Windows.")
    app = VoiceThresholdOverlay()
    app.run()
