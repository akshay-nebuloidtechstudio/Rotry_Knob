import os
import threading
import time
from pathlib import Path
from queue import Empty, Queue

import pygame
from PIL import Image, ImageOps

try:
    from gpiozero import DigitalInputDevice
except Exception:  # Allows testing on Windows/non-Pi systems.
    DigitalInputDevice = None

ROOT_DIR = Path(__file__).resolve().parent
IMAGE_DIR = ROOT_DIR / "images"
CONFIG_PATH = ROOT_DIR / "config.json"

DEFAULT_SETTINGS = {
    "fullscreen": True,
    "display_width": 0,            # 0 = use the real screen size
    "display_height": 0,           # 0 = use the real screen size
    "image_scale_mode": "stretch", # "stretch", "fill" or "fit"
    "home_timeout_seconds": 60.0,
    "transition_seconds": 0.35,
    "gpio_enabled": True,
    "encoder_a_pin": 17,
    "encoder_b_pin": 27,
    "pulses_per_revolution": 360,
    "degrees_per_image": 36,       # CHANGED: one image per 36 degrees
    "pulse_reset_seconds": 10.0,   # keep a slow, partial turn longer
    "invert_direction": False,
}


def normalize_settings(settings):
    """Accept both current and legacy config key names."""
    normalized = dict(settings)

    if "clockwise_when_b_low" in normalized and "invert_direction" not in normalized:
        normalized["invert_direction"] = not bool(normalized["clockwise_when_b_low"])

    for legacy_key, canonical_key in {
        "clk_pin": "encoder_a_pin",
        "dt_pin": "encoder_b_pin",
        "degrees_per_photo": "degrees_per_image",
        "encoder_pulses_per_revolution": "pulses_per_revolution",
    }.items():
        if legacy_key in normalized and canonical_key not in normalized:
            normalized[canonical_key] = normalized[legacy_key]

    return normalized


def is_raspberry_pi():
    try:
        with open("/proc/device-tree/model", "r", encoding="utf-8") as handle:
            return "raspberry" in handle.read().lower()
    except OSError:
        return False


def load_settings():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            __import__("json").dumps(DEFAULT_SETTINGS, indent=2), encoding="utf-8"
        )
        return DEFAULT_SETTINGS.copy()

    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            loaded = __import__("json").load(handle)
    except (__import__("json").JSONDecodeError, OSError):
        loaded = {}

    merged = DEFAULT_SETTINGS.copy()
    merged.update(loaded)
    merged = normalize_settings(merged)
    with CONFIG_PATH.open("w", encoding="utf-8") as handle:
        __import__("json").dump(merged, handle, indent=2)
    return merged


SETTINGS = load_settings()


def load_source_surface(path):
    """Load JPG/JPEG/PNG/BMP/HEIC and mislabeled HEIC files through Pillow."""
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        raw = image.tobytes()
        return pygame.image.fromstring(raw, image.size, "RGB").convert()


def fit_image(source, screen_size, mode="stretch"):
    """Scale an image to the display.

    stretch - force the image to the exact screen size (no crop, no black bars)
    fill    - cover the whole screen, keep proportions, crop any overflow
    fit     - show the whole image, black bars where proportions differ
    """
    width, height = screen_size
    source_width = source.get_width()
    source_height = source.get_height()

    frame = pygame.Surface(screen_size).convert()
    frame.fill((0, 0, 0))

    if mode == "stretch":
        frame.blit(pygame.transform.smoothscale(source, (width, height)), (0, 0))
        return frame

    if mode == "fit":
        scale = min(width / source_width, height / source_height)
    else:  # "fill"
        scale = max(width / source_width, height / source_height)

    size = (
        max(1, round(source_width * scale)),
        max(1, round(source_height * scale)),
    )
    scaled = pygame.transform.smoothscale(source, size)
    x = (width - size[0]) // 2
    y = (height - size[1]) // 2
    frame.blit(scaled, (x, y))
    return frame


def load_images(screen_size):
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    supported = (".jpg", ".jpeg", ".png", ".bmp", ".heic", ".heif")
    mode = str(SETTINGS.get("image_scale_mode", "stretch")).lower()

    photo_files = []
    home_file = None
    for path in IMAGE_DIR.iterdir():
        if not path.is_file() or path.suffix.lower() not in supported:
            continue
        if path.stem.lower() == "home":
            home_file = path
        elif path.stem.isdigit() and 1 <= int(path.stem) <= 33:
            photo_files.append(path)

    photo_files.sort(key=lambda p: int(p.stem))

    missing = [str(n) for n in range(1, 34) if not any(int(p.stem) == n for p in photo_files)]
    if missing:
        raise SystemExit("Missing photo files: " + ", ".join(missing))
    if home_file is None:
        raise SystemExit("Missing images/home.jpeg (home photo).")

    photos = []
    for path in photo_files:
        try:
            photos.append(fit_image(load_source_surface(path), screen_size, mode))
        except Exception as exc:
            raise SystemExit(f"Could not load {path.name}: {exc}") from exc

    try:
        home = fit_image(load_source_surface(home_file), screen_size, mode)
    except Exception as exc:
        raise SystemExit(f"Could not load {home_file.name}: {exc}") from exc

    return photos, home


class PhotoGallery:
    def __init__(self):
        self.settings = SETTINGS
        self.events = Queue()
        self.photo_index = 0
        self.showing_home = True
        self.last_activity = time.monotonic()
        self.transitioning = False
        self.encoder = None
        self.encoder_a = None
        self.encoder_b = None

        # Pulse accumulation: one image change per degrees_per_image of rotation.
        self.pulse_count = 0
        self.last_pulse = 0.0
        self.lock = threading.Lock()

        pygame.init()
        pygame.display.set_caption("Rotary Photo Display")

        # width/height 0 = draw at the real screen size the Pi outputs.
        # A positive width and height = fixed canvas, scaled to the screen.
        target_w = int(self.settings.get("display_width", 0))
        target_h = int(self.settings.get("display_height", 0))
        fullscreen = bool(self.settings.get("fullscreen", True))
        if target_w > 0 and target_h > 0:
            size = (target_w, target_h)
            flags = pygame.SCALED | (pygame.FULLSCREEN if fullscreen else 0)
        else:
            size = (0, 0)
            flags = pygame.FULLSCREEN if fullscreen else 0
        try:
            self.screen = pygame.display.set_mode(size, flags)
        except pygame.error:
            self.screen = pygame.display.set_mode((1280, 720))

        self.width, self.height = self.screen.get_size()
        print(f"Display size in use: {self.width} x {self.height}")
        print(f"Pulses per image: {self.pulses_per_image()}")
        self.clock = pygame.time.Clock()
        self.photos, self.home = load_images((self.width, self.height))
        self.current = self.home
        self.setup_hardware()

    def setup_hardware(self):
        if not self.settings.get("gpio_enabled", True):
            return
        if not is_raspberry_pi() or DigitalInputDevice is None:
            return

        a_pin = int(self.settings.get("encoder_a_pin", 17))
        b_pin = int(self.settings.get("encoder_b_pin", 27))

        try:
            # Count ONLY A rising edges. Pulses are accumulated and one image
            # change happens every pulses_per_image() pulses.
            self.encoder_a = DigitalInputDevice(a_pin, pull_up=False)
            self.encoder_b = DigitalInputDevice(b_pin, pull_up=False)
            self.encoder_a.when_activated = self.on_encoder_pulse
        except Exception as exc:
            print(f"Encoder setup failed: {exc}")
            self.encoder_a = None
            self.encoder_b = None

    def pulses_per_image(self):
        ppr = float(self.settings.get("pulses_per_revolution", 360))
        deg = float(self.settings.get("degrees_per_image", 36))  # CHANGED
        return max(1, round(ppr * deg / 360.0))

    def on_encoder_pulse(self):
        # Pulses are not ignored while a slide is running, so every degree of
        # knob rotation is counted.
        if self.encoder_b is None:
            return

        # Quadrature direction: B LOW/HIGH on A rising determines direction.
        # If the physical installation is opposite, set invert_direction=true.
        clockwise = bool(self.encoder_b.value)
        if self.settings.get("invert_direction", False):
            clockwise = not clockwise

        now = time.monotonic()
        with self.lock:
            # Forget a stale partial turn.
            if now - self.last_pulse > float(self.settings.get("pulse_reset_seconds", 10.0)):
                self.pulse_count = 0
            self.last_pulse = now
            self.last_activity = now  # resets the 60 s home timer

            self.pulse_count += 1 if clockwise else -1
            if abs(self.pulse_count) >= self.pulses_per_image():
                direction = 1 if self.pulse_count > 0 else -1
                self.pulse_count = 0
                self.events.put(("encoder", direction))

    def transition(self, direction):
        """Slide to the next/previous photo; fade from home."""
        if self.transitioning:
            return
        self.transitioning = True

        old_image = self.current
        if self.showing_home:
            # First encoder movement always starts the gallery at Photo 1.
            new_index = 0
        else:
            new_index = (self.photo_index + direction) % len(self.photos)

        new_image = self.photos[new_index]
        duration = max(float(self.settings.get("transition_seconds", 0.35)), 0.05)
        start = time.monotonic()

        while True:
            progress = min((time.monotonic() - start) / duration, 1.0)
            eased = progress * progress * (3.0 - 2.0 * progress)

            self.screen.fill((0, 0, 0))
            if self.showing_home:
                # Home -> Photo 1: fade in Photo 1.
                old_image.set_alpha(255)
                self.screen.blit(old_image, (0, 0))
                new_image.set_alpha(round(255 * eased))
                self.screen.blit(new_image, (0, 0))
                new_image.set_alpha(255)
            else:
                # CW: old exits left, new enters from right.
                # CCW: old exits right, new enters from left.
                travel = round(self.width * eased)
                if direction > 0:
                    old_x = -travel
                    new_x = self.width - travel
                else:
                    old_x = travel
                    new_x = -self.width + travel
                self.screen.blit(old_image, (old_x, 0))
                self.screen.blit(new_image, (new_x, 0))

            pygame.display.flip()
            self.clock.tick(60)
            if progress >= 1.0:
                break

        self.photo_index = new_index
        self.showing_home = False
        self.current = new_image
        self.last_activity = time.monotonic()
        self.transitioning = False
        # Rotation counted during the slide is kept, so the next image still
        # comes at exactly 36 degrees.  # CHANGED

    def go_home(self):
        if self.showing_home or self.transitioning:
            return

        self.transitioning = True
        old_image = self.current
        duration = max(float(self.settings.get("transition_seconds", 0.35)), 0.05)
        start = time.monotonic()

        while True:
            progress = min((time.monotonic() - start) / duration, 1.0)
            eased = progress * progress * (3.0 - 2.0 * progress)
            self.screen.blit(old_image, (0, 0))
            self.home.set_alpha(round(255 * eased))
            self.screen.blit(self.home, (0, 0))
            pygame.display.flip()
            self.clock.tick(60)
            if progress >= 1.0:
                break

        self.home.set_alpha(255)
        self.current = self.home
        self.showing_home = True
        self.photo_index = 0
        self.transitioning = False

        with self.lock:
            self.pulse_count = 0
            while not self.events.empty():
                try:
                    self.events.get_nowait()
                except Empty:
                    break

    def process_queue(self):
        while True:
            try:
                action = self.events.get_nowait()
            except Empty:
                break
            if action[0] == "encoder":
                direction = action[1]
                self.last_activity = time.monotonic()
                self.transition(direction)

    def run(self):
        running = True
        timeout = max(float(self.settings.get("home_timeout_seconds", 60.0)), 1.0)

        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                # Keyboard is intentionally ignored: normal operation is encoder-only.

            self.process_queue()

            if (
                not self.showing_home
                and not self.transitioning
                and time.monotonic() - self.last_activity >= timeout
            ):
                self.go_home()

            self.screen.blit(self.current, (0, 0))
            pygame.display.flip()
            self.clock.tick(60)

        if self.encoder_a is not None:
            self.encoder_a.close()
        if self.encoder_b is not None:
            self.encoder_b.close()
        pygame.quit()


def main():
    PhotoGallery().run()


if __name__ == "__main__":
    main()