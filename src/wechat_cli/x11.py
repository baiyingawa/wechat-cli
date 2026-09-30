import hashlib
import os
import select
import subprocess
import time
from dataclasses import asdict, dataclass

from PIL import Image
from Xlib import X, XK, display, error, protocol
from Xlib.ext import damage, xtest

from .errors import AutomationError
from .wait import wait_until


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    def as_list(self):
        return [self.x, self.y, self.width, self.height]

    def contains(self, x, y):
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height


@dataclass(frozen=True)
class Window:
    id: int
    title: str
    class_name: str
    rect: Rect

    def as_dict(self):
        return asdict(self)


class Desktop:
    def __init__(self, display_name):
        try:
            self.connection = display.Display(display_name)
        except Exception as exception:
            raise AutomationError("DISPLAY_UNAVAILABLE", str(exception),
                                  {"display": display_name}) from exception
        self.display_name = display_name
        self.root = self.connection.screen().root
        self.root.change_attributes(event_mask=X.SubstructureNotifyMask | X.PropertyChangeMask)
        self.damage_id = None
        self.revision = 0
        self.events = 0
        self.captures = 0
        self.capture_ms = 0.0
        self.expected_pointer = None
        self.clipboard_owner = None
        self.clipboard_data = None
        if self.connection.has_extension("DAMAGE"):
            self.connection.damage_query_version()
            self.damage_id = self.root.damage_create(damage.DamageReportNonEmpty)
        self.connection.sync()
        self.drain()

    def close(self):
        if self.clipboard_owner is not None:
            self.clipboard_owner.terminate()
        self.connection.close()

    @property
    def bounds(self):
        geometry = self.root.get_geometry()
        return Rect(0, 0, geometry.width, geometry.height)

    def drain(self):
        changed = False
        while self.connection.pending_events():
            event = self.connection.next_event()
            self.events += 1
            if event.type not in (X.MotionNotify, X.KeyPress, X.KeyRelease):
                changed = True
        if changed:
            self.revision += 1
        if self.damage_id is not None:
            self.connection.damage_subtract(self.damage_id)
            self.connection.flush()
        return changed

    def wake(self, timeout):
        if self.drain():
            return True
        ready, _, _ = select.select([self.connection.fileno()], [], [], timeout)
        if ready:
            return self.drain()
        return False

    def windows(self):
        result = []
        queue = [(self.root, 0)]
        seen = set()
        while queue:
            parent, depth = queue.pop(0)
            try:
                for window in parent.query_tree().children:
                    if window.id in seen:
                        continue
                    seen.add(window.id)
                    attributes = window.get_attributes()
                    if attributes.map_state != X.IsViewable:
                        continue
                    classes = window.get_wm_class() or ()
                    class_name = "/".join(classes)
                    title_property = window.get_full_property(
                        self.connection.intern_atom("_NET_WM_NAME"), X.AnyPropertyType)
                    title = (title_property.value.decode("utf-8", errors="replace")
                             if title_property is not None else window.get_wm_name() or "")
                    if "wechat" in class_name.lower():
                        geometry = window.get_geometry()
                        position = self.root.translate_coords(window, 0, 0)
                        if geometry.width > 20 and geometry.height > 20:
                            screen = self.bounds
                            x = max(screen.x, min(position.x, screen.x + screen.width - 1))
                            y = max(screen.y, min(position.y, screen.y + screen.height - 1))
                            width = min(geometry.width, screen.x + screen.width - x)
                            height = min(geometry.height, screen.y + screen.height - y)
                            result.append(Window(window.id, title, class_name,
                                                 Rect(x, y, max(1, width), max(1, height))))
                    elif depth < 2:
                        queue.append((window, depth + 1))
            except error.XError:
                continue
        return result

    def main_window(self):
        candidates = [window for window in self.windows() if window.title == "微信"]
        if not candidates:
            raise AutomationError("CLIENT_NOT_RUNNING", "No visible WeChat window", retryable=True)
        return max(candidates, key=lambda window: window.rect.width * window.rect.height)

    def capture(self, rect=None):
        rect = rect or self.bounds
        screen = self.bounds
        if (rect.width <= 0 or rect.height <= 0 or rect.x < 0 or rect.y < 0
                or rect.x + rect.width > screen.width or rect.y + rect.height > screen.height):
            raise AutomationError("INVALID_REGION", "Screenshot region is outside the display",
                                  {"region": rect.as_list(), "screen": screen.as_list()})
        started = time.monotonic()
        pixels = self.root.get_image(rect.x, rect.y, rect.width, rect.height, X.ZPixmap, 0xffffffff)
        formats = self.connection.display.info.pixmap_formats
        pixel_format = next(item for item in formats if item.depth == pixels.depth)
        if pixel_format.bits_per_pixel != 32 or self.connection.display.info.image_byte_order != 0:
            raise AutomationError("UNSUPPORTED_DISPLAY", "Use a little-endian 24-bit X11 display")
        image = Image.frombytes("RGB", (rect.width, rect.height), pixels.data, "raw", "BGRX")
        self.captures += 1
        self.capture_ms += (time.monotonic() - started) * 1000
        return image

    def fingerprint(self, rect):
        return hashlib.blake2b(self.capture(rect).tobytes(), digest_size=16).hexdigest()

    def changed(self, rect, before, timeout=5):
        return wait_until(lambda: self.fingerprint(rect) != before, self.wake,
                          timeout, "region to change")

    def settle(self, rect, timeout=2, quiet_ms=40):
        started = time.monotonic()
        deadline = started + timeout
        previous = self.fingerprint(rect)
        last_changed = started
        while time.monotonic() < deadline:
            remaining_quiet = quiet_ms / 1000 - (time.monotonic() - last_changed)
            if remaining_quiet <= 0:
                return {"stable": True, "elapsed_ms": round((time.monotonic() - started) * 1000, 3)}
            self.wake(min(remaining_quiet, deadline - time.monotonic()))
            current = self.fingerprint(rect)
            if current != previous:
                previous = current
                last_changed = time.monotonic()
        raise AutomationError("UI_UNSTABLE", "Region did not stop repainting", retryable=True)

    def focus(self, window):
        resource = self.connection.create_resource_object("window", window.id)
        resource.configure(stack_mode=X.Above)
        resource.set_input_focus(X.RevertToParent, X.CurrentTime)
        self.connection.sync()

    def maximize(self, window):
        bounds = self.bounds
        resource = self.connection.create_resource_object("window", window.id)
        resource.configure(x=0, y=0, width=bounds.width, height=bounds.height)
        self.connection.sync()
        result = wait_until(
            lambda: next((item.as_dict() for item in self.windows()
                          if item.id == window.id and item.rect == bounds), None),
            self.wake, 3, "window geometry",
        )
        return result.value

    def resize(self, window, rect):
        resource = self.connection.create_resource_object("window", window.id)
        resource.configure(x=rect.x, y=rect.y, width=rect.width, height=rect.height)
        self.connection.sync()
        return wait_until(lambda: next((item for item in self.windows()
                                        if item.id == window.id and item.rect == rect), None),
                          self.wake, 3, "popup geometry").value

    def close_window(self, window):
        resource = self.connection.create_resource_object("window", window.id)
        delete_atom = self.connection.intern_atom("WM_DELETE_WINDOW")
        supported = resource.get_wm_protocols() or []
        if delete_atom not in supported:
            raise AutomationError("UNSUPPORTED_WINDOW", "Window has no safe close protocol")
        message = protocol.event.ClientMessage(window=resource,
            client_type=self.connection.intern_atom("WM_PROTOCOLS"),
            data=(32, [delete_atom, X.CurrentTime, 0, 0, 0]))
        resource.send_event(message)
        self.connection.flush()
        return wait_until(lambda: not any(item.id == window.id for item in self.windows()),
                          self.wake, 3, "popup to close").value

    def begin_input(self):
        self.expected_pointer = None

    def check_pointer(self):
        if self.expected_pointer is not None:
            pointer = self.root.query_pointer()
            actual = (pointer.root_x, pointer.root_y)
            if actual != self.expected_pointer:
                raise AutomationError("USER_INTERFERENCE", "Pointer moved during automation",
                                      {"expected": self.expected_pointer, "actual": actual})

    def click(self, x, y, button=1):
        self.check_pointer()
        if not self.bounds.contains(x, y):
            raise AutomationError("INVALID_COORDINATES", "Click is outside the display")
        xtest.fake_input(self.connection, X.MotionNotify, x=int(x), y=int(y))
        xtest.fake_input(self.connection, X.ButtonPress, detail=button)
        xtest.fake_input(self.connection, X.ButtonRelease, detail=button)
        self.connection.sync()
        self.expected_pointer = (int(x), int(y))

    def move(self, x, y):
        self.check_pointer()
        if not self.bounds.contains(x, y):
            raise AutomationError("INVALID_COORDINATES", "Pointer is outside the display")
        xtest.fake_input(self.connection, X.MotionNotify, x=int(x), y=int(y))
        self.connection.sync()
        self.expected_pointer = (int(x), int(y))

    def key(self, name):
        self.check_pointer()
        keys = name.split("+")
        keycodes = []
        for key in keys:
            keysym = XK.string_to_keysym(key)
            keycode = self.connection.keysym_to_keycode(keysym)
            if not keycode:
                raise AutomationError("INVALID_KEY", f"Unknown key: {key}")
            keycodes.append(keycode)
        for keycode in keycodes:
            xtest.fake_input(self.connection, X.KeyPress, detail=keycode)
        for keycode in reversed(keycodes):
            xtest.fake_input(self.connection, X.KeyRelease, detail=keycode)
        self.connection.sync()

    def paste(self, text):
        self.check_pointer()
        data = text.encode("utf-8")
        clipboard_atom = self.connection.intern_atom("CLIPBOARD")
        previous_owner = self.selection_owner_id(clipboard_atom)
        try:
            writer = subprocess.run(
                ["xclip", "-selection", "clipboard", "-in"],
                input=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={**os.environ, "DISPLAY": self.display_name}, timeout=2,
            )
            if writer.returncode:
                raise AutomationError("CLIPBOARD_UNAVAILABLE", "xclip failed to claim clipboard")
            wait_until(lambda: self.selection_owner_id(clipboard_atom) not in (0, previous_owner),
                       self.wake, 1, "clipboard owner")
            check = subprocess.run(
                ["xclip", "-selection", "clipboard", "-out", "-t", "UTF8_STRING"],
                capture_output=True, env={**os.environ, "DISPLAY": self.display_name}, timeout=2,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exception:
            raise AutomationError("CLIPBOARD_UNAVAILABLE", str(exception)) from exception
        if check.returncode or check.stdout != data:
            raise AutomationError("CLIPBOARD_MISMATCH", "Clipboard did not retain requested text")
        self.clipboard_data = data
        self.key("Control_L+v")

    def selected_text(self):
        clipboard_atom = self.connection.intern_atom("CLIPBOARD")
        previous_owner = self.selection_owner_id(clipboard_atom)
        self.key("Control_L+a")
        self.key("Control_L+c")
        wait_until(lambda: self.selection_owner_id(clipboard_atom) not in (0, previous_owner),
                   self.wake, 1, "selected text clipboard owner")
        try:
            copied = subprocess.run(["xclip", "-selection", "clipboard", "-out", "-t", "UTF8_STRING"],
                                    capture_output=True, env={**os.environ, "DISPLAY": self.display_name}, timeout=2)
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            raise AutomationError("CLIPBOARD_UNAVAILABLE", str(error)) from error
        if copied.returncode:
            raise AutomationError("CLIPBOARD_UNAVAILABLE", "Could not copy selected text")
        return copied.stdout.decode("utf-8")

    def selection_owner_id(self, atom):
        owner = self.connection.get_selection_owner(atom)
        return getattr(owner, "id", owner)

    def metrics(self):
        return {"event_backend": "xdamage" if self.damage_id is not None else "x11+bounded-poll",
                "events": self.events, "captures": self.captures,
                "capture_ms": round(self.capture_ms, 3), "revision": self.revision}
