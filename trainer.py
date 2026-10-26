#!/usr/bin/env python3
"""Strafe trainer: runs beside the game (started by sourceyaw-run).

Reads the strafe, turn and jump keys plus mouse movement from the Source Yaw
input service (/run/sourceyaw-inputd.sock), and while a jump key is held:
  - plays a sound when you turn one way for a few ticks without holding that
    way's strafe key (no key, or the other one),
  - works out each strafe's offset: the ticks between turning the other way
    and switching strafe keys. Negative means the key came early, positive
    late, 0 on the same tick. The number is drawn over the game by the Source
    Yaw overlay layer (overlay/), through a small shared state file.

Keys come from the game's own binds (config.cfg plus aliases from every cfg),
so rebinding in the game just works as long as the input service forwards
that key (see /etc/sourceyaw-inputd.conf).

  trainer.py GAME            run until the game exits (sourceyaw-run does this)
  trainer.py GAME --binds    print which keys do what, then exit
"""

import array
import fcntl
from fractions import Fraction
import glob
import math
import mmap
import os
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import yawcore

SOCKET = "/run/sourceyaw-inputd.sock"
INPUTD_CONFIG = "/etc/sourceyaw-inputd.conf"
OVERLAY_DIR = yawcore.CONFIG_DIR / "overlay"
ATLAS = OVERLAY_DIR / "atlas.png"
STATE = OVERLAY_DIR / "state"
EVENT = struct.Struct("<qIHHi")
EV_KEY, EV_REL = 1, 2
REL_X, REL_WHEEL = 0, 8

# Source key names -> evdev codes the input service can forward
SOURCE_KEYS = {c: code for c, code in zip("qwertyuiop", range(16, 26))}
SOURCE_KEYS.update({c: code for c, code in zip("asdfghjkl", range(30, 39))})
SOURCE_KEYS.update({c: code for c, code in zip("zxcvbnm", range(44, 51))})
SOURCE_KEYS.update({d: code for d, code in zip("1234567890", range(2, 12))})
SOURCE_KEYS.update({"space": 57, "alt": 56, "ralt": 100, "ctrl": 29, "rctrl": 97, "shift": 42,
                    "rshift": 54, "tab": 15, "capslock": 58, "backspace": 14,
                    "mouse1": 272, "mouse2": 273, "mouse3": 274, "mouse4": 275, "mouse5": 276,
                    "mwheelup": "wheel", "mwheeldown": "wheel"})

# evdev names in the input service's config -> codes, for the keys above
EVDEV_NAMES = {**{f"KEY_{k.upper()}": c for k, c in SOURCE_KEYS.items() if len(k) == 1},
               "KEY_SPACE": 57, "KEY_LEFTALT": 56, "KEY_RIGHTALT": 100, "KEY_LEFTCTRL": 29,
               "KEY_RIGHTCTRL": 97, "KEY_LEFTSHIFT": 42, "KEY_RIGHTSHIFT": 54, "KEY_TAB": 15,
               "KEY_CAPSLOCK": 58, "KEY_BACKSPACE": 14, "BTN_LEFT": 272, "BTN_RIGHT": 273,
               "BTN_MIDDLE": 274, "BTN_SIDE": 275, "BTN_EXTRA": 276}


def forwarded():
    """Codes the input service passes on ("wheel" included), or None if it isn't installed."""
    try:
        text = Path(INPUTD_CONFIG).read_text()
    except OSError:
        return None
    codes = set()
    for line in text.splitlines():
        name, _, value = line.split("#", 1)[0].partition("=")
        if name.strip() == "keys":
            codes |= {EVDEV_NAMES[k] for k in value.split() if k in EVDEV_NAMES}
        elif name.strip() == "axes" and "REL_WHEEL" in value.split():
            codes.add("wheel")
    return codes


ROLES = {"+jump": "jump", "+moveleft": "strafe_left", "+moveright": "strafe_right",
         "+left": "turn_left", "+right": "turn_right"}

TRAINER_DEFAULTS = {
    "enabled": False,
    "sound": "tone",               # wrong-key cue: "tone", a game sound, or "" for none
    "tone_volume": 0.3,            # 0-1, for the constant tone
    "tone_pitch": 220,             # Hz of the other-key tone; the no-key tone is 2/3 of it
    "cue_wrong_key": True,         # cue when turning with the other strafe key held
    "cue_no_key": True,            # cue when turning with no strafe key held
    "wrong_ticks": 3,              # ticks of wrong (or no) key before the cue
    "repeat": 0.2,                 # game sound: seconds between plays while wrong; 0 = once
    "target": -1,                  # the offset counted as best (shown green)
    "show_offset": True,           # at launch; offset_key flips it in game
    "offset_key": "",              # key bound in yaws.cfg to show or hide the offset
    "x": 0.5, "y": 0.56,           # number's centre, as a fraction of the screen
    "size": 36,                    # font size in pixels
    "colors": {"early": "#ff5a5a", "perfect": "#5aff78", "late": "#ffc83c"},
    "hide_after": 2.0,             # seconds the last offset stays up
    "window": 0.25,                # seconds a key switch may be from a turn to count
    "mouse_threshold": 4,          # mouse counts the other way that make a turn
    # CS:S servers pick their own tickrate; Momentum's follows the map's game mode
    "tickrate": {"css": 66, "momentum": 100},
    "map_tickrates": {"surf_": 66, "bhop_": 100},
}


def settings(game):
    data = yawcore.load_settings(game).get("trainer") or {}
    merged = {**TRAINER_DEFAULTS, **data}
    merged["colors"] = {**TRAINER_DEFAULTS["colors"], **data.get("colors", {})}
    return merged


# ---------------------------------------------------------------- binds

_ALIAS = re.compile(r'^\s*alias\s+"?([^\s"]+)"?\s+"?(.*?)"?\s*(//.*)?$', re.IGNORECASE)


def _commands(text):
    return [c.strip().strip('"').split()[0].lower() for c in text.split(";") if c.strip().strip('"')]


def read_binds(game_dir):
    """{role: set of evdev codes or "wheel"}, plus Source key names with a role the
    input service does not forward."""
    cfg = Path(game_dir) / "cfg"
    aliases = {}
    for path in sorted(glob.glob(str(cfg / "**/*.cfg"), recursive=True)):
        try:
            text = Path(path).read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = _ALIAS.match(line)
            if m:
                aliases.setdefault(m.group(1).lower(), []).append(m.group(2))

    def roles_of(command, depth=0):
        found = set()
        for cmd in _commands(command):
            if cmd in ROLES:
                found.add(ROLES[cmd])
            elif depth < 6 and cmd in aliases:
                for body in aliases[cmd]:
                    found |= roles_of(body, depth + 1)
        return found

    roles = {r: set() for r in ROLES.values()}
    unseen = []
    allowed = forwarded()
    try:
        config = (cfg / "config.cfg").read_text(errors="replace")
    except OSError:
        config = ""
    for line in config.splitlines():
        m = yawcore._BIND.match(line)
        if not m:
            continue
        key = (m.group(1) or m.group(2)).lower()
        for role in roles_of(m.group(3).strip()):
            code = SOURCE_KEYS.get(key)
            if code is None or (allowed is not None and code not in allowed):
                unseen.append(f"{key} ({role.replace('_', ' ')})")
            else:
                roles[role].add(code)
    return roles, sorted(set(unseen))


# ---------------------------------------------------------------- overlay

def write_atlas(cfg):
    from PIL import Image, ImageDraw, ImageFont
    fonts = (glob.glob("/usr/share/fonts/**/DejaVuSansMono-Bold.ttf", recursive=True)
             or glob.glob("/usr/share/fonts/**/*Mono*Bold*.ttf", recursive=True))
    px = max(8, int(cfg["size"]))
    font = ImageFont.truetype(fonts[0], px) if fonts else ImageFont.load_default()
    stroke = max(2, px // 16)
    _, top, _, bottom = font.getbbox("0123456789+-", stroke_width=stroke)
    cell_w = int(font.getlength("0")) + 2 * stroke + 2
    cell_h = int(bottom - top) + 2 * stroke + 2
    colours = [cfg["colors"][k] for k in ("early", "perfect", "late")]
    im = Image.new("RGBA", (cell_w * 12, cell_h * len(colours)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)
    for row, colour in enumerate(colours):
        for i, c in enumerate("0123456789+-"):
            draw.text((i * cell_w + cell_w / 2, row * cell_h + cell_h / 2), c, font=font,
                      fill=colour, anchor="mm", stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
    OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ATLAS.with_suffix(".tmp.png")
    im.save(tmp)
    tmp.replace(ATLAS)


class Overlay:
    """Writes the state the overlay layer reads every frame (see overlay/src/layer.c)."""
    FMT = struct.Struct("<IIIIIfff16s")

    def __init__(self):
        OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
        if not STATE.exists() or STATE.stat().st_size < self.FMT.size:
            STATE.write_bytes(b"\0" * self.FMT.size)
        self.file = open(STATE, "r+b")
        self.map = mmap.mmap(self.file.fileno(), self.FMT.size)
        self.seq = struct.unpack_from("<I", self.map, 4)[0] & ~1

    def show(self, text, row, x, y, visible=True):
        self.seq += 1
        struct.pack_into("<I", self.map, 4, self.seq)  # odd: being written
        self.map[:self.FMT.size] = self.FMT.pack(0x314F5953, self.seq, int(visible), row, 3,
                                                 x, y, 1.0, text.encode()[:15])
        self.seq += 1
        struct.pack_into("<I", self.map, 4, self.seq)

    def hide(self):
        self.show("", 0, 0.5, 0.5, visible=False)


# ---------------------------------------------------------------- constant tone

class Tone:
    """A tone that sounds while `mode` is set, streamed to PipeWire through pw-cat.
    Each mode has its own pitch: "wrong" (other strafe key held) is `pitch`,
    "none" (no strafe key held) a fifth lower.

    Audio is written in 5 ms chunks paced by the clock, keeping only LEAD
    seconds queued, so switching it on or off is heard within ~20 ms (a plain
    pipe would buffer most of a second). Each pitch is pre-rendered as a loop of
    chunks that holds a whole number of cycles, so the chunks join without
    clicks; switching on, off or between pitches fades over one chunk."""
    RATE = 48000
    CHUNK = 240     # 5 ms
    LEAD = 0.015

    def __init__(self, volume, pitch=220):
        self.mode = None
        self.proc = None
        player = shutil.which("pw-cat")
        if not player:
            return
        self.proc = subprocess.Popen(
            [player, "--playback", "--raw", "--rate", str(self.RATE), "--channels", "1",
             "--format", "s16", "--latency", "10ms", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            fcntl.fcntl(self.proc.stdin, 1031, 4096)  # F_SETPIPE_SZ: the smallest pipe
        except OSError:
            pass
        amp = 32767 * max(0.0, min(1.0, volume))
        pitches = {"wrong": max(40, int(pitch)), "none": max(40, round(int(pitch) * 2 / 3))}
        self.silence = bytes(2 * self.CHUNK)
        # per mode: the loop of chunks (whole cycles overall) as float samples
        self.loops = {}
        for mode, hz in pitches.items():
            count = Fraction(hz * self.CHUNK, self.RATE).denominator
            samples = [amp * math.sin(2 * math.pi * hz * i / self.RATE)
                       for i in range(count * self.CHUNK)]
            self.loops[mode] = [samples[k * self.CHUNK:(k + 1) * self.CHUNK] for k in range(count)]
        # steady chunks ready to write; only fades are computed on the fly
        self.loop_bytes = {mode: [array.array("h", (int(x) for x in c)).tobytes() for c in loop]
                           for mode, loop in self.loops.items()}
        self.pos = {mode: 0 for mode in pitches}
        self.ramp = [i / self.CHUNK for i in range(self.CHUNK)]

    def _chunk(self, mode, envelope=None):
        k = self.pos[mode] % len(self.loops[mode])
        self.pos[mode] += 1
        if envelope is None:
            return self.loop_bytes[mode][k]
        return array.array("h", (int(s * e) for s, e in zip(self.loops[mode][k], envelope))).tobytes()

    def start(self):
        if self.proc:
            threading.Thread(target=self._feed, daemon=True).start()

    def _feed(self):
        start = time.monotonic()
        written = 0
        was = None
        while True:
            due = (time.monotonic() - start + self.LEAD) * self.RATE
            if written >= due:
                time.sleep(0.002)
                continue
            mode = self.mode
            if mode == was:
                data = self._chunk(mode) if mode else self.silence
            elif mode is None:
                data = self._chunk(was, [1 - r for r in self.ramp])
            elif was is None:
                data = self._chunk(mode, self.ramp)
            else:
                # other pitch: cross-fade so neither tone cuts off mid-wave
                a = self._chunk(was, [1 - r for r in self.ramp])
                b = self._chunk(mode, self.ramp)
                data = array.array("h", (x + y for x, y in zip(
                    array.array("h", a), array.array("h", b)))).tobytes()
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                return
            was = mode
            written += self.CHUNK
            if written + self.RATE < due:  # fell far behind (suspend): don't catch up
                written = int(due)

    def close(self):
        if self.proc:
            self.proc.kill()


# ---------------------------------------------------------------- the trainer

class Trainer:
    def __init__(self, cfg, roles, tickrate, on_offset, on_wrong, on_wrong_state=None):
        self.cfg = cfg
        self.on_wrong_state = on_wrong_state
        self.wrong_active = None   # None, "wrong" or "none"
        self.roles = roles
        self.tickrate = tickrate
        self.on_offset = on_offset
        self.on_wrong = on_wrong
        self.held = {}             # code -> press time
        self.last_wheel = 0.0
        self.strafe_dir = 0        # -1 left, 1 right, 0 none
        self.strafe_changes = []   # (time, new strafe_dir)
        self.turn_dir = 0
        self.mouse_dir = 0
        self.run_sum = 0
        self.run_start = 0.0
        self.last_motion = 0.0
        self.pending = None        # (turn time, direction) waiting for its key switch
        self.wrong_since = None
        self.last_cue = None

    def has(self, role, code):
        return code in self.roles[role]

    # False while another window has focus or the game is in its menu (no map
    # loaded): keys and mouse are still tracked, so nothing sticks when you come
    # back, but nothing counts or sounds
    focused = True

    def jumping(self, now):
        if not self.focused:
            return False
        return (any(c in self.roles["jump"] for c in self.held)
                or ("wheel" in self.roles["jump"] and now - self.last_wheel < 0.1))

    def _strafe(self):
        """Direction of the most recently pressed strafe key still held."""
        best, when = 0, -1.0
        for code, t in self.held.items():
            for role, d in (("strafe_left", -1), ("strafe_right", 1)):
                if code in self.roles[role] and t > when:
                    best, when = d, t
        return best

    def _button_turn(self):
        best, when = 0, -1.0
        for code, t in self.held.items():
            for role, d in (("turn_left", -1), ("turn_right", 1)):
                if code in self.roles[role] and t > when:
                    best, when = d, t
        return best

    def _set_turn(self, direction, t):
        if direction == self.turn_dir or direction == 0:
            return
        self.turn_dir = direction
        if not self.jumping(t):
            self.pending = None
            return
        window = self.cfg["window"]
        if self.strafe_dir == direction:
            # key already switched: early (or same tick)
            for when, d in reversed(self.strafe_changes):
                if d == direction:
                    if t - when <= window:
                        self._offset(when - t)
                    break
            self.pending = None
        else:
            self.pending = (t, direction)

    def _offset(self, seconds):
        self.on_offset(round(seconds * self.tickrate))

    def feed(self, t, etype, code, value):
        if etype == EV_KEY:
            if value:
                self.held[code] = t
            else:
                self.held.pop(code, None)
            new = self._strafe()
            if new != self.strafe_dir:
                self.strafe_dir = new
                self.strafe_changes = self.strafe_changes[-15:] + [(t, new)]
                if self.pending and new == self.pending[1]:
                    if t - self.pending[0] <= self.cfg["window"]:
                        self._offset(t - self.pending[0])
                    self.pending = None
            button = self._button_turn()
            if button:
                self._set_turn(button, t)
            elif self.mouse_dir:
                self._set_turn(self.mouse_dir, t)
        elif etype == EV_REL and code == REL_WHEEL:
            self.last_wheel = t
        elif etype == EV_REL and code == REL_X and value:
            sign = 1 if value > 0 else -1
            self.last_motion = t
            if sign == self.mouse_dir:
                self.run_sum = 0
                return
            # a turn the other way counts from its first count, once it is more
            # than jitter: mouse_threshold counts without a long pause
            if self.run_sum == 0 or t - self.last_run_t > 0.05:
                self.run_sum, self.run_start = 0, t
            self.last_run_t = t
            self.run_sum += abs(value)
            if self.run_sum >= self.cfg["mouse_threshold"] or self.mouse_dir == 0:
                self.mouse_dir = sign
                self.run_sum = 0
                if not self._button_turn():
                    self._set_turn(sign, self.run_start)

    last_run_t = 0.0

    def _wrong_state(self, reason):
        if reason != self.wrong_active:
            self.wrong_active = reason
            if self.on_wrong_state:
                self.on_wrong_state(reason)

    def check(self, now):
        """Wrong-key cue; called often between events."""
        if self.pending and now - self.pending[0] > self.cfg["window"]:
            self.pending = None
        turning = self._button_turn() or (self.mouse_dir if now - self.last_motion < 0.03 else 0)
        reason = None
        if turning and self.jumping(now) and self.strafe_dir != turning:
            reason = "none" if self.strafe_dir == 0 else "wrong"
            if not self.cfg["cue_no_key" if reason == "none" else "cue_wrong_key"]:
                reason = None
        if reason is None:
            self.wrong_since, self.last_cue = None, None
            self._wrong_state(None)
            return
        if self.wrong_since is None:
            self.wrong_since = now
        if now - self.wrong_since < self.cfg["wrong_ticks"] / self.tickrate:
            return
        self._wrong_state(reason)
        # first cue once it has lasted wrong_ticks, then every `repeat` seconds
        # for as long as it stays wrong
        repeat = self.cfg["repeat"]
        if self.last_cue is None or (repeat > 0 and now - self.last_cue >= repeat):
            self.last_cue = now
            self.on_wrong()


# ---------------------------------------------------------------- focus

class FocusWatch:
    """Whether the game's window has focus, asked from Hyprland every 0.25 s.

    The input service sees every key and mouse movement on the desktop, so
    without this, scrolling in a browser while the game runs in the background
    would count as jumping. With follow_mouse = 2 the game keeps keyboard focus
    while the wheel scrolls the window under the cursor, so the cursor must be
    inside the game's window too. Other desktops have no common way to ask, so
    there the game counts as focused."""
    INTERVAL = 0.25

    def __init__(self, process):
        self.process = process
        self.focused = True
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        self.path = os.path.join(runtime, "hypr", sig, ".socket.sock") if runtime and sig else None
        if self.path and os.path.exists(self.path):
            threading.Thread(target=self._watch, daemon=True).start()

    def _ask(self, command):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(self.path)
            s.sendall(command)
            data = b""
            while chunk := s.recv(65536):
                data += chunk
        import json
        return json.loads(data or b"{}") or {}

    def _check(self):
        window = self._ask(b"j/activewindow")
        with open(f"/proc/{window.get('pid')}/comm") as f:
            if f.read().strip() != self.process:
                return False
        cursor = self._ask(b"j/cursorpos")
        (x, y), (w, h) = window["at"], window["size"]
        return x <= cursor["x"] < x + w and y <= cursor["y"] < y + h

    def _watch(self):
        while True:
            try:
                self.focused = self._check()
            except (OSError, ValueError, TypeError, KeyError):
                self.focused = False
            time.sleep(self.INTERVAL)


# ---------------------------------------------------------------- running

def connect():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCKET)
    return s


# console lines that mean a map was entered or left; the game starts in its menu
_MAP_IN = re.compile(r"Host_NewGame on map |^Connected to |^Map: ")
_MAP_OUT = re.compile(r"Host_ShutdownServer|Host_Shutdown\b|^Disconnect")


def map_state(line):
    """True when the line means a map was entered, False when left, else None."""
    if _MAP_IN.search(line):
        return True
    if _MAP_OUT.search(line):
        return False
    return None


def map_tickrate(cfg, game, line):
    # Momentum: "Host_NewGame on map <name>"; CS:S on joining a server: "Map: <name>"
    m = re.search(r"on map (\S+)|^Map: (\S+)", line)
    if not m:
        return None
    name = (m.group(1) or m.group(2)).lower()
    for prefix, rate in cfg["map_tickrates"].items():
        if name.startswith(prefix.lower()):
            return rate
    return cfg["tickrate"].get(game, 66)


def run(game):
    cfg = settings(game)
    game_dir = yawcore.find_game(game)
    if not cfg["enabled"] or game_dir is None:
        return 0
    roles, unseen = read_binds(game_dir)
    if unseen:
        print(f"trainer: keys the input service does not forward: {', '.join(unseen)}", flush=True)
    write_atlas(cfg)
    overlay = Overlay()
    overlay.hide()

    try:
        sock = connect()
    except OSError as e:
        print(f"trainer: no input service ({e}); install it from Source Yaw", flush=True)
        return 1

    shown_until = 0.0
    showing = cfg["show_offset"]

    def offset(ticks):
        nonlocal shown_until
        if not showing:
            return
        target = cfg["target"]
        row = 0 if ticks < target else 1 if ticks == target else 2
        overlay.show(f"{ticks:+d}" if ticks else "0", row, cfg["x"], cfg["y"])
        shown_until = time.monotonic() + cfg["hide_after"]

    tone = Tone(cfg["tone_volume"], cfg["tone_pitch"]) if cfg["sound"] == "tone" else None
    if tone:
        tone.start()

    def wrong():
        if cfg["sound"] and not tone:
            yawcore.play_sound(game_dir, cfg["sound"], game)

    def wrong_state(reason):
        if tone:
            tone.mode = reason

    trainer = Trainer(cfg, roles, cfg["tickrate"].get(game, 66), offset, wrong, wrong_state)
    focus = FocusWatch(yawcore.GAMES[game]["process"])
    log_path = game_dir / "yaw.log"
    log = open(log_path, errors="replace") if log_path.exists() else None
    if log:
        log.seek(0, os.SEEK_END)
    buf = b""
    # without a console log there is no way to tell, so everything counts
    in_map = log is None
    while True:
        ready, _, _ = select.select([sock], [], [], 0.002)
        if ready:
            data = sock.recv(4096)
            if not data:
                print("trainer: input service went away", flush=True)
                return 1
            buf += data
            while len(buf) >= EVENT.size:
                sec, usec, etype, code, value = EVENT.unpack_from(buf)
                buf = buf[EVENT.size:]
                trainer.feed(sec + usec / 1e6, etype, code, value)
        trainer.focused = focus.focused and in_map
        trainer.check(time.time())
        if shown_until and time.monotonic() > shown_until:
            overlay.hide()
            shown_until = 0.0
        if log:
            for line in log.readlines():
                if yawcore.OFFSETS_TOKEN in line:
                    showing = not showing
                    if not showing:
                        overlay.hide()
                        shown_until = 0.0
                    print(f"trainer: offset {'shown' if showing else 'hidden'}", flush=True)
                state = map_state(line)
                if state is not None and state != in_map:
                    in_map = state
                    print(f"trainer: {'in a map' if in_map else 'in the menu'}", flush=True)
                rate = map_tickrate(cfg, game, line)
                if rate:
                    trainer.tickrate = rate
                    print(f"trainer: {line.strip()} -> {rate} tick", flush=True)
        elif log_path.exists():
            log = open(log_path, errors="replace")
            in_map = False  # the game just started logging: it's in its menu


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in yawcore.GAMES:
        print(__doc__)
        return 2
    game = sys.argv[1]
    if "--binds" in sys.argv:
        game_dir = yawcore.find_game(game)
        roles, unseen = read_binds(game_dir)
        for role, codes in roles.items():
            print(f"{role:13} {sorted(codes, key=str)}")
        if unseen:
            print("not forwarded by the input service:", ", ".join(unseen))
        return 0
    try:
        return run(game)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
