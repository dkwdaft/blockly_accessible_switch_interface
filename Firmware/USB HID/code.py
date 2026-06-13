"""
Switch Interface for Adafruit Feather ESP32-S3 Rev TFT
USB HID Version — Triple Mode with Onboard Mode Selection
Uses built-in 1.14" 240x135 color TFT display
Compatible with CircuitPython

EXTERNAL SWITCHES (3.5mm jacks, wired to GND, Pull.UP active LOW):
  A1 → Navigate switch
  A2 → Select / Single switch

ONBOARD BUTTONS / EXTERNAL SWITCHES (mode selection):
  D0 (onboard)             → Direct Switch Mode (Mode 0)
  D1 (onboard)             → Enter Single-Switch Scanning Mode
  D2 (onboard)             → Enter Two-Switch Mode

  EXTERNAL SWITCHES at the mode-select screen:
    Single-click A2 (select) → Single-Switch Scanning Mode
    Single-click A1 (nav)    → Two-Switch Mode
    Double-click A1 or A2    → Direct Switch Mode (Mode 0)
  (Note: a single external-switch click waits one DOUBLE_CLICK_WINDOW before
   committing, so a second click can be detected for Mode 0.)

─── DIRECT SWITCH MODE (Mode 0) ───────────────────────
  A1 TAP    : Send Switch 1 action (default: Enter)
  A2 TAP    : Send Switch 2 action (default: Tab)
  A1/A2 HOLD (≥ SWITCH_HOLD_EXIT_SECS) : Back to Mode Select
  (Customisable via config.py — supports keyboard, mouse & media)

─── SINGLE-SWITCH MODE ─────────────────────────────────────
  A2 SHORT PRESS  (≤ MAX_SHORT_PRESS_SECS)  : Select & send current item
  A2 HOLD         (≥ HOLD_TO_SCAN_SECS)     : Start auto-scanning
  A2 PRESS during scan                      : Select & send, stop scanning
  RESET button                              : Back to Mode Select

─── TWO-SWITCH MODE ────────────────────────────────────────
  A1  : Advance to next item
  A2  : Select & send current item
  RESET button : Back to Mode Select

(To leave Single- or Two-Switch mode, press the onboard RESET button. Mode 0
 can also be left by holding an external switch, since it may be reached by
 double-click without onboard access.)
"""

import time
import board
import digitalio
import displayio
import terminalio
try:
    from fourwire import FourWire
except ImportError:
    from displayio import FourWire
import adafruit_st7789
from adafruit_display_text import label
import usb_hid
from adafruit_hid.keyboard import Keyboard
from adafruit_hid.keycode import Keycode

# Optional mouse / consumer control — imported only if needed
# These are imported lazily in run_direct_switch_mode() to avoid
# pulling in unused HID descriptors on minimal builds.
try:
    from adafruit_hid.mouse import Mouse
    _mouse_available = True
except ImportError:
    _mouse_available = False

try:
    from adafruit_hid.consumer_control import ConsumerControl
    from adafruit_hid.consumer_control_code import ConsumerControlCode
    _consumer_available = True
except ImportError:
    _consumer_available = False

print("Starting Switch Interface...")

# ===============================================
# ===== USER CONFIGURABLE VARIABLES =====
# ===============================================

# --- Single-Switch Mode ---
# How long (seconds) A2 must be held to begin auto-scanning
HOLD_TO_SCAN_SECS = 2.0

# How fast (seconds) the scanner advances to the next item
SCAN_INTERVAL_SECS = 1

# Maximum press duration (seconds) considered a "short press"
MAX_SHORT_PRESS_SECS = 0.5

# --- Two-Switch Mode ---
# Debounce delay (seconds) after a nav or select press
DEBOUNCE_TIME = 0.1

# --- Double-click to enter Mode 0 ---
# Maximum gap (seconds) between two clicks to count as a double-click
DOUBLE_CLICK_WINDOW = 0.4

# --- External switch hold to exit Mode 0 ---
# How long (seconds) an external switch (A1 or A2) must be held in Mode 0
# to return to the mode-select menu. Lets users who reached Mode 0 by
# double-click exit without needing the onboard buttons.
# (To exit Single- or Two-Switch mode, press the onboard RESET button.)
SWITCH_HOLD_EXIT_SECS = 1.5

# ─── MODE 0: DIRECT SWITCH ACTIONS ─────────────────────────────────────────
#
# Each action is a dict with the key "type" and type-specific fields:
#
#   Keyboard key press:
#     {"type": "key", "keys": [Keycode.ENTER]}
#     {"type": "key", "keys": [Keycode.LEFT_CONTROL, Keycode.C]}
#
#   Mouse button click:
#     {"type": "mouse_click", "button": Mouse.LEFT_BUTTON}   (requires adafruit_hid.mouse)
#
#   Mouse scroll:
#     {"type": "mouse_scroll", "x": 0, "y": 1}   (positive y = scroll up)
#
#   Mouse move:
#     {"type": "mouse_move", "x": 10, "y": 0}
#
#   Media / consumer control:
#     {"type": "media", "code": ConsumerControlCode.PLAY_PAUSE}
#     {"type": "media", "code": ConsumerControlCode.VOLUME_INCREMENT}
#     {"type": "media", "code": ConsumerControlCode.VOLUME_DECREMENT}
#     {"type": "media", "code": ConsumerControlCode.MUTE}
#     {"type": "media", "code": ConsumerControlCode.SCAN_NEXT_TRACK}
#     {"type": "media", "code": ConsumerControlCode.SCAN_PREVIOUS_TRACK}

SWITCH1_ACTION = {"type": "key", "keys": [Keycode.ENTER]}   # A1 press
SWITCH2_ACTION = {"type": "key", "keys": [Keycode.TAB]}     # A2 press

SWITCH1_LABEL  = "Enter"   # Shown on TFT
SWITCH2_LABEL  = "Tab"     # Shown on TFT
SWITCH1_SYMBOL = "ENT"     # Short TFT symbol (1-3 ASCII chars)
SWITCH2_SYMBOL = "TAB"     # Short TFT symbol (1-3 ASCII chars)
# NOTE: the onboard TFT uses terminalio.FONT (a built-in bitmap font) which
# only renders basic ASCII / Latin-1 characters. Emoji and most symbol glyphs
# (e.g. ⏎ ⇥ → ▶) will appear blank. Stick to plain letters/numbers/punctuation.
# To show emoji you would need to load a custom BDF/PCF font with adafruit_bitmap_font.

# ===============================================
# ===== DISPLAY INITIALIZATION =====
# ===============================================

print("\nInitializing display...")
displayio.release_displays()

spi = board.SPI()
tft_cs = board.TFT_CS
tft_dc = board.TFT_DC
tft_reset = board.TFT_RESET
tft_backlight = board.TFT_BACKLIGHT

display_bus = FourWire(
    spi,
    command=tft_dc,
    chip_select=tft_cs,
    reset=tft_reset,
    baudrate=24000000
)

display = adafruit_st7789.ST7789(
    display_bus,
    width=240,
    height=135,
    rowstart=40,
    colstart=52,
    rotation=270,
    bgr=True
)

backlight = digitalio.DigitalInOut(tft_backlight)
backlight.direction = digitalio.Direction.OUTPUT
backlight.value = True

print("Display initialized!")

# ===============================================
# ===== LED SETUP =====
# ===============================================

led = digitalio.DigitalInOut(board.LED)
led.direction = digitalio.Direction.OUTPUT

# ===============================================
# ===== EXTERNAL SWITCH PINS (A1, A2) =====
# Pull.UP — switch wired between pin and GND.
# Pressed = False (LOW).
# ===============================================

switch_nav = digitalio.DigitalInOut(board.A1)
switch_nav.direction = digitalio.Direction.INPUT
switch_nav.pull = digitalio.Pull.UP

switch_select = digitalio.DigitalInOut(board.A2)
switch_select.direction = digitalio.Direction.INPUT
switch_select.pull = digitalio.Pull.UP

def nav_pressed():
    return not switch_nav.value       # Active LOW

def select_pressed():
    return not switch_select.value    # Active LOW

# ===============================================
# ===== ONBOARD BUTTONS (D0, D1, D2) =====
# D0: Pull.UP  → active LOW
# D1: Pull.DOWN → active HIGH
# D2: Pull.DOWN → active HIGH
# ===============================================

button0 = digitalio.DigitalInOut(board.D0)
button0.switch_to_input(pull=digitalio.Pull.UP)

button1 = digitalio.DigitalInOut(board.D1)
button1.switch_to_input(pull=digitalio.Pull.DOWN)

button2 = digitalio.DigitalInOut(board.D2)
button2.switch_to_input(pull=digitalio.Pull.DOWN)

def d0_pressed():
    return not button0.value   # Active LOW

def d1_pressed():
    return button1.value       # Active HIGH

def d2_pressed():
    return button2.value       # Active HIGH

# ===============================================
# ===== USB HID SETUP =====
# ===============================================

time.sleep(1)
keyboard = Keyboard(usb_hid.devices)

_mouse = None
_consumer = None

def get_mouse():
    global _mouse
    if _mouse is None and _mouse_available:
        try:
            _mouse = Mouse(usb_hid.devices)
        except Exception as e:
            print(f"Mouse init failed: {e}")
    return _mouse

def get_consumer():
    global _consumer
    if _consumer is None and _consumer_available:
        try:
            _consumer = ConsumerControl(usb_hid.devices)
        except Exception as e:
            print(f"ConsumerControl init failed: {e}")
    return _consumer

# ===============================================
# ===== KEYCODES AND SYMBOL DEFINITIONS =====
# ===============================================

KEYCODES = [
    ("arrow right", [Keycode.RIGHT_ARROW]),
    ("arrow down",  [Keycode.DOWN_ARROW]),
    ("enter",       [Keycode.ENTER]),
    ("arrow left",  [Keycode.LEFT_ARROW]),
    ("arrow up",    [Keycode.UP_ARROW]),
    ("delete",      [Keycode.DELETE]),
    ("w",           [Keycode.W]),
    ("t",           [Keycode.T]),
    ("m",           [Keycode.M]),
]

KEY_SYMBOLS = {
    "arrow right": "R",
    "arrow down":  "D",
    "enter":       "e",
    "arrow left":  "L",
    "arrow up":    "U",
    "delete":      "x",
    "w":           "W",
    "t":           "T",
    "m":           "M",
}

# Load user overrides from config.py (if present on CIRCUITPY drive)
try:
    from config import *
except ImportError:
    pass  # config.py missing — use defaults above

# ===============================================
# ===== DISPLAY HELPERS =====
# ===============================================

SCREEN_WIDTH  = 240
SCREEN_HEIGHT = 135
LEFT_WIDTH    = SCREEN_WIDTH // 4
RIGHT_WIDTH   = SCREEN_WIDTH - LEFT_WIDTH

PURPLE     = 0x800080
SCAN_COLOR = 0x0055FF
DARK_BLUE  = 0x003399
TEAL       = 0x007060      # Mode 0 accent colour
LIGHT_GRAY = 0xAAAAAA
WHITE      = 0xFFFFFF
GREEN      = 0x00CC00
RED        = 0xFF0000

splash = displayio.Group()
display.root_group = splash

def create_background(color, x, y, width, height):
    bmp = displayio.Bitmap(width, height, 1)
    pal = displayio.Palette(1)
    pal[0] = color
    return displayio.TileGrid(bmp, pixel_shader=pal, x=x, y=y)

def clear_display():
    while splash:
        splash.pop()

def draw_keycode_screen(index, flash_color=None, scanning=False):
    """Standard two-pane keycode display shared by single- and two-switch modes."""
    clear_display()

    current_symbol = KEY_SYMBOLS.get(KEYCODES[index][0], "?")
    next_index     = (index + 1) % len(KEYCODES)
    next_symbol    = KEY_SYMBOLS.get(KEYCODES[next_index][0], "?")

    right_color = SCAN_COLOR if scanning else PURPLE
    splash.append(create_background(right_color, LEFT_WIDTH, 0, RIGHT_WIDTH, SCREEN_HEIGHT))
    splash.append(label.Label(
        terminalio.FONT, text=current_symbol, color=WHITE, scale=9,
        anchor_point=(0.5, 0.5),
        anchored_position=(LEFT_WIDTH + RIGHT_WIDTH // 2, SCREEN_HEIGHT // 2)
    ))

    splash.append(create_background(LIGHT_GRAY, 0, 0, LEFT_WIDTH, SCREEN_HEIGHT))
    splash.append(label.Label(
        terminalio.FONT, text=next_symbol, color=PURPLE, scale=3,
        anchor_point=(0.5, 0.5),
        anchored_position=(LEFT_WIDTH // 2, SCREEN_HEIGHT // 2)
    ))

    if flash_color is not None:
        splash.append(create_background(flash_color, 0, 0, SCREEN_WIDTH, SCREEN_HEIGHT))

def draw_direct_switch_screen(active=None, flash_color=None):
    """
    Two-pane display for Mode 0.
    active = None | 1 | 2   (which switch was just pressed)
    """
    clear_display()

    half = SCREEN_WIDTH // 2

    # Left pane — Switch 1 (A1)
    left_bg = flash_color if (active == 1 and flash_color) else TEAL
    right_bg = flash_color if (active == 2 and flash_color) else DARK_BLUE

    splash.append(create_background(left_bg,  0,    0, half,                SCREEN_HEIGHT))
    splash.append(create_background(right_bg, half, 0, SCREEN_WIDTH - half, SCREEN_HEIGHT))

    # Vertical divider
    splash.append(create_background(WHITE, half - 1, 0, 2, SCREEN_HEIGHT))

    # Switch 1 label
    splash.append(label.Label(
        terminalio.FONT, text="SW1", color=WHITE, scale=1,
        anchor_point=(0.5, 0.2),
        anchored_position=(half // 2, SCREEN_HEIGHT // 2 - 22)
    ))
    sym1 = SWITCH1_SYMBOL[:3] if len(SWITCH1_SYMBOL) <= 3 else SWITCH1_SYMBOL[:3]
    splash.append(label.Label(
        terminalio.FONT, text=sym1, color=WHITE, scale=4,
        anchor_point=(0.5, 0.5),
        anchored_position=(half // 2, SCREEN_HEIGHT // 2 + 4)
    ))

    # Switch 2 label
    splash.append(label.Label(
        terminalio.FONT, text="SW2", color=WHITE, scale=1,
        anchor_point=(0.5, 0.2),
        anchored_position=(half + (SCREEN_WIDTH - half) // 2, SCREEN_HEIGHT // 2 - 22)
    ))
    sym2 = SWITCH2_SYMBOL[:3] if len(SWITCH2_SYMBOL) <= 3 else SWITCH2_SYMBOL[:3]
    splash.append(label.Label(
        terminalio.FONT, text=sym2, color=WHITE, scale=4,
        anchor_point=(0.5, 0.5),
        anchored_position=(half + (SCREEN_WIDTH - half) // 2, SCREEN_HEIGHT // 2 + 4)
    ))

def draw_menu_screen():
    """Mode-select screen shown on startup and on return from any mode."""
    clear_display()

    third = SCREEN_WIDTH // 3

    splash.append(create_background(DARK_BLUE, 0, 0, SCREEN_WIDTH, SCREEN_HEIGHT))

    # Dividers
    splash.append(create_background(WHITE, third - 1,     0, 2, SCREEN_HEIGHT))
    splash.append(create_background(WHITE, 2 * third - 1, 0, 2, SCREEN_HEIGHT))

    # Left — D0 → Mode 0 Direct Switch
    splash.append(label.Label(
        terminalio.FONT, text="D0", color=WHITE, scale=2,
        anchor_point=(0.5, 0.5),
        anchored_position=(third // 2, SCREEN_HEIGHT // 2 - 20)
    ))
    splash.append(label.Label(
        terminalio.FONT, text="DIR", color=LIGHT_GRAY, scale=2,
        anchor_point=(0.5, 0.5),
        anchored_position=(third // 2, SCREEN_HEIGHT // 2 + 10)
    ))
    splash.append(label.Label(
        terminalio.FONT, text="direct", color=LIGHT_GRAY, scale=1,
        anchor_point=(0.5, 0.5),
        anchored_position=(third // 2, SCREEN_HEIGHT // 2 + 28)
    ))

    # Centre — D1 or A2 → Single-Switch mode
    splash.append(label.Label(
        terminalio.FONT, text="D1/A2", color=WHITE, scale=2,
        anchor_point=(0.5, 0.5),
        anchored_position=(third + third // 2, SCREEN_HEIGHT // 2 - 20)
    ))
    splash.append(label.Label(
        terminalio.FONT, text="1-SW", color=LIGHT_GRAY, scale=2,
        anchor_point=(0.5, 0.5),
        anchored_position=(third + third // 2, SCREEN_HEIGHT // 2 + 10)
    ))
    splash.append(label.Label(
        terminalio.FONT, text="select", color=LIGHT_GRAY, scale=1,
        anchor_point=(0.5, 0.5),
        anchored_position=(third + third // 2, SCREEN_HEIGHT // 2 + 28)
    ))

    # Right — D2 or A1 → Two-Switch mode
    splash.append(label.Label(
        terminalio.FONT, text="D2/A1", color=WHITE, scale=2,
        anchor_point=(0.5, 0.5),
        anchored_position=(2 * third + third // 2, SCREEN_HEIGHT // 2 - 20)
    ))
    splash.append(label.Label(
        terminalio.FONT, text="2-SW", color=LIGHT_GRAY, scale=2,
        anchor_point=(0.5, 0.5),
        anchored_position=(2 * third + third // 2, SCREEN_HEIGHT // 2 + 10)
    ))
    splash.append(label.Label(
        terminalio.FONT, text="nav", color=LIGHT_GRAY, scale=1,
        anchor_point=(0.5, 0.5),
        anchored_position=(2 * third + third // 2, SCREEN_HEIGHT // 2 + 28)
    ))

# ===============================================
# ===== SHARED ACTION DISPATCHER =====
# ===============================================

def dispatch_action(action, label_str="action"):
    """
    Execute a switch action dict.
    Supported types: key, mouse_click, mouse_scroll, mouse_move, media
    """
    try:
        t = action.get("type", "key")

        if t == "key":
            keys = action.get("keys", [Keycode.ENTER])
            keyboard.press(*keys)
            time.sleep(0.05)
            keyboard.release_all()
            print(f"EVENT: Sent key: {label_str}")

        elif t == "mouse_click":
            m = get_mouse()
            if m:
                btn = action.get("button", Mouse.LEFT_BUTTON)
                m.click(btn)
                print(f"EVENT: Mouse click: {label_str}")
            else:
                print("ERROR: Mouse HID not available")

        elif t == "mouse_scroll":
            m = get_mouse()
            if m:
                m.move(wheel=action.get("y", 0))
                print(f"EVENT: Mouse scroll: {label_str}")
            else:
                print("ERROR: Mouse HID not available")

        elif t == "mouse_move":
            m = get_mouse()
            if m:
                m.move(x=action.get("x", 0), y=action.get("y", 0))
                print(f"EVENT: Mouse move: {label_str}")
            else:
                print("ERROR: Mouse HID not available")

        elif t == "media":
            cc = get_consumer()
            if cc:
                cc.send(action.get("code", ConsumerControlCode.PLAY_PAUSE))
                print(f"EVENT: Media: {label_str}")
            else:
                print("ERROR: ConsumerControl HID not available")

        else:
            print(f"WARN: Unknown action type: {t}")

        return True
    except Exception as e:
        print(f"ERROR: dispatch_action failed ({label_str}): {e}")
        return False

# ===============================================
# ===== SHARED KEYCODE SENDER (Modes 1 & 2) =====
# ===============================================

def send_keycode(index):
    """Send keycode at index, flash the screen. Returns True on success."""
    key_name, keycode = KEYCODES[index]
    try:
        keyboard.press(*keycode)
        time.sleep(0.05)
        keyboard.release_all()
        draw_keycode_screen(index, flash_color=GREEN)
        print(f"EVENT: Sent: {key_name}")
        time.sleep(0.1)
        return True
    except Exception as e:
        draw_keycode_screen(index, flash_color=RED)
        print(f"ERROR: Failed to send {key_name}: {e}")
        time.sleep(0.1)
        return False

# ===============================================
# ===== MODE 0: DIRECT SWITCH (A1=SW1, A2=SW2) =====
# ===============================================

def run_direct_switch_mode():
    """
    Mode 0 — Direct Switch Interface
    A1 → Switch 1 action  (default: Enter)
    A2 → Switch 2 action  (default: Tab)

    Exit to mode select by holding A1 or A2 for SWITCH_HOLD_EXIT_SECS.
    A short tap (released before the hold threshold) fires that switch's action;
    a long hold returns to the menu without firing the action.
    (If the board is reachable, the onboard RESET button also returns to the menu.)

    Actions are defined by SWITCH1_ACTION / SWITCH2_ACTION dicts and may be
    keyboard keys, mouse clicks/moves/scrolls, or media controls. See config.py.
    """
    print("\n--- Direct Switch Mode (Mode 0) ---")
    print(f"  SW1 (A1): {SWITCH1_LABEL}")
    print(f"  SW2 (A2): {SWITCH2_LABEL}")
    print(f"  Hold a switch {SWITCH_HOLD_EXIT_SECS}s = back to menu (or press RESET)\n")

    last_sw1 = nav_pressed()
    last_sw2 = select_pressed()

    draw_direct_switch_screen()

    while True:
        try:
            sw1 = nav_pressed()
            sw2 = select_pressed()
            led.value = sw1 or sw2

            # Fresh press on either switch → decide tap vs hold
            pressed_now = None
            if sw1 and not last_sw1:
                pressed_now = 1
            elif sw2 and not last_sw2:
                pressed_now = 2

            if pressed_now is not None:
                press_start = time.monotonic()

                # Watch the press: short release → fire action; long hold → exit
                while nav_pressed() or select_pressed():
                    held = time.monotonic() - press_start
                    if held >= SWITCH_HOLD_EXIT_SECS:
                        print("EVENT: Switch held → back to menu")
                        # Wait for full release before leaving
                        while nav_pressed() or select_pressed():
                            time.sleep(0.01)
                        return
                    time.sleep(0.01)

                # Released before hold threshold → fire the action for that switch
                if pressed_now == 1:
                    draw_direct_switch_screen(active=1, flash_color=GREEN)
                    dispatch_action(SWITCH1_ACTION, SWITCH1_LABEL)
                else:
                    draw_direct_switch_screen(active=2, flash_color=GREEN)
                    dispatch_action(SWITCH2_ACTION, SWITCH2_LABEL)

                time.sleep(0.05)
                draw_direct_switch_screen()
                time.sleep(DEBOUNCE_TIME)

            last_sw1 = nav_pressed()
            last_sw2 = select_pressed()

            time.sleep(0.01)

        except RuntimeError as e:
            print(f"RuntimeError: {e}")
            time.sleep(1.0)
        except Exception as e:
            print(f"Unexpected error: {e}")
            time.sleep(1.0)

# ===============================================
# ===== MODE 1: SINGLE SWITCH (A2) =====
# ===============================================

def run_single_switch_mode():
    """
    A2 is the single external switch (Pull.UP, active LOW).
      Short press  → select & send current item.
      Hold         → start auto-scanning.
      Press during scan → select & send, stop scanning.
      RESET button → return to mode select.
    """
    print("\n--- Single-Switch Mode (A2) ---")
    print(f"  Hold to scan:    {HOLD_TO_SCAN_SECS}s")
    print(f"  Scan interval:   {SCAN_INTERVAL_SECS}s")
    print(f"  Max short press: {MAX_SHORT_PRESS_SECS}s")
    print("  Press RESET = back to menu\n")

    current_index  = 0
    state          = "IDLE"   # IDLE | PRESS_PENDING | SCANNING
    press_start    = None
    last_scan_time = None

    draw_keycode_screen(current_index)

    while True:
        try:
            now = time.monotonic()

            sw = select_pressed()   # A2
            led.value = sw

            # ── IDLE ──────────────────────────────────────────────
            if state == "IDLE":
                if sw:
                    press_start = now
                    state = "PRESS_PENDING"

            # ── PRESS_PENDING ──────────────────────────────────────
            elif state == "PRESS_PENDING":
                hold = now - press_start

                if not sw:
                    if hold <= MAX_SHORT_PRESS_SECS:
                        print(f"EVENT: Short press ({hold:.2f}s) → select")
                        send_keycode(current_index)
                        draw_keycode_screen(current_index)
                    else:
                        print(f"DEBUG: Ambiguous release ({hold:.2f}s) — ignored")
                    state = "IDLE"

                elif hold >= HOLD_TO_SCAN_SECS:
                    print("EVENT: Hold threshold → start scanning")
                    last_scan_time = now
                    state = "SCANNING"
                    draw_keycode_screen(current_index, scanning=True)

            # ── SCANNING ──────────────────────────────────────────
            elif state == "SCANNING":
                if sw:
                    print("EVENT: Press during scan → select")
                    send_keycode(current_index)
                    while select_pressed():
                        time.sleep(0.01)
                    state = "IDLE"
                    draw_keycode_screen(current_index, scanning=False)

                elif now - last_scan_time >= SCAN_INTERVAL_SECS:
                    current_index = (current_index + 1) % len(KEYCODES)
                    last_scan_time = now
                    draw_keycode_screen(current_index, scanning=True)
                    print(f"SCAN: → {KEYCODES[current_index][0]}")

            time.sleep(0.01)

        except RuntimeError as e:
            print(f"RuntimeError: {e}")
            time.sleep(1.0)
        except Exception as e:
            print(f"Unexpected error: {e}")
            time.sleep(1.0)

# ===============================================
# ===== MODE 2: TWO SWITCH (A1 = NAV, A2 = SELECT) =====
# ===============================================

def run_two_switch_mode():
    """
    A1 = Navigate  (advance to next item, Pull.UP active LOW).
    A2 = Select    (send current item,    Pull.UP active LOW).
    RESET button   = Return to mode select.
    """
    print("\n--- Two-Switch Mode (A1=Nav, A2=Select) ---")
    print("  Press RESET = back to menu\n")

    current_index  = 0
    last_nav_state = nav_pressed()
    last_sel_state = select_pressed()

    draw_keycode_screen(current_index)

    while True:
        try:
            nav_state = nav_pressed()
            sel_state = select_pressed()
            led.value = nav_state or sel_state

            # A1 rising edge → navigate
            if nav_state and not last_nav_state:
                current_index = (current_index + 1) % len(KEYCODES)
                draw_keycode_screen(current_index)
                print(f"EVENT: Navigate → {KEYCODES[current_index][0]}")
                time.sleep(DEBOUNCE_TIME)

            # A2 rising edge → select
            if sel_state and not last_sel_state:
                send_keycode(current_index)
                draw_keycode_screen(current_index)
                time.sleep(DEBOUNCE_TIME)

            last_nav_state = nav_state
            last_sel_state = sel_state

            time.sleep(0.01)

        except RuntimeError as e:
            print(f"RuntimeError: {e}")
            time.sleep(1.0)
        except Exception as e:
            print(f"Unexpected error: {e}")
            time.sleep(1.0)

# ===============================================
# ===== EXTERNAL-SWITCH GESTURE HELPER =====
# ===============================================

def resolve_external_switch_gesture(first_switch):
    """
    Called the moment an external switch press is first detected at the
    mode-select screen. `first_switch` is "A1" or "A2".

    Waits for the first press to release, then watches for a SECOND press
    within DOUBLE_CLICK_WINDOW seconds.

    Returns:
      "mode0"   if a second press arrives in time (double-click)
      "single"  if no second press and the first switch was A2
      "two"     if no second press and the first switch was A1
    """
    # Wait for the first press to be released
    while nav_pressed() or select_pressed():
        time.sleep(0.005)

    # Watch for a second press within the window
    window_start = time.monotonic()
    while time.monotonic() - window_start < DOUBLE_CLICK_WINDOW:
        if nav_pressed() or select_pressed():
            # Second press → double-click → Mode 0
            while nav_pressed() or select_pressed():
                time.sleep(0.005)
            return "mode0"
        time.sleep(0.005)

    # No second press → single click maps to a mode by which switch was used
    return "single" if first_switch == "A2" else "two"


# ===============================================
# ===== STARTUP =====
# ===============================================

print("=" * 40)
print("Switch Interface — Triple Mode")
print("Feather ESP32-S3 Rev TFT")
print("=" * 40)
print(f"Loaded {len(KEYCODES)} keycodes")
print("External switches: A1 = Navigate/SW1, A2 = Select/SW2")
print("D0 (onboard)             = Direct Switch Mode (Mode 0)")
print("D1 (onboard) or single-click A2 = Single-Switch Scanning Mode")
print("D2 (onboard) or single-click A1 = Two-Switch Mode")
print("Double-click A1 or A2    = Direct Switch Mode (Mode 0)")
print()

# ===============================================
# ===== MAIN LOOP — MODE SELECT =====
# ===============================================

while True:
    try:
        draw_menu_screen()
        print("MODE SELECT:")
        print("  D0 (onboard)          → Direct Switch (Mode 0)")
        print("  D1 (onboard)          → Single-Switch")
        print("  D2 (onboard)          → Two-Switch")
        print("  Single-click A2       → Single-Switch")
        print("  Single-click A1       → Two-Switch")
        print("  Double-click A1 / A2  → Direct Switch (Mode 0)")

        # Drain any buttons/switches still held from a previous mode before listening
        while d0_pressed() or d1_pressed() or d2_pressed() or select_pressed() or nav_pressed():
            time.sleep(0.01)

        # Wait for a fresh activation.
        # Onboard buttons act instantly (unambiguous).
        # An external-switch press is DEFERRED: we wait to see whether a second
        # press follows (double-click → Mode 0) before committing to a mode.
        while True:
            # ── Onboard buttons — instant ──────────────────────────
            if d0_pressed():
                while d0_pressed():
                    time.sleep(0.01)
                run_direct_switch_mode()
                break

            if d1_pressed():
                while d1_pressed():
                    time.sleep(0.01)
                run_single_switch_mode()
                break

            if d2_pressed():
                while d2_pressed():
                    time.sleep(0.01)
                run_two_switch_mode()
                break

            # ── External switch — single vs double click ───────────
            if nav_pressed() or select_pressed():
                first = "A1" if nav_pressed() else "A2"
                gesture = resolve_external_switch_gesture(first)
                if gesture == "mode0":
                    print("EVENT: Double-click → Direct Switch Mode (Mode 0)")
                    run_direct_switch_mode()
                elif gesture == "single":
                    print("EVENT: Single-click A2 → Single-Switch Mode")
                    run_single_switch_mode()
                else:  # "two"
                    print("EVENT: Single-click A1 → Two-Switch Mode")
                    run_two_switch_mode()
                break

            time.sleep(0.01)

    except Exception as e:
        print(f"Menu error: {e}")
        time.sleep(1.0)
