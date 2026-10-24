from __future__ import annotations

import argparse
import base64
import bisect
import fcntl
import hashlib
import io
import json
import math
import os
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import tty
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

try:
    import tomllib
except ImportError:
    sys.exit("lyr: needs Python 3.11 or newer")

try:
    from PIL import Image, ImageDraw, ImageFont

    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

try:
    import mutagen

    HAVE_MUTAGEN = True
except Exception:
    HAVE_MUTAGEN = False

__version__ = "1.1.0"
APP = "lyr"
UA = f"{APP}/{__version__} (terminal lyrics viewer)"
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), APP)
ART_DIR = os.path.join(CACHE_DIR, "art")
DEFAULT_FONT = "Helvetica:bold"
FIELDS = [
    "playerName",
    "status",
    "position",
    "mpris:length",
    "artist",
    "title",
    "album",
    "xesam:url",
    "mpris:artUrl",
]
FMT = "\t".join("{{%s}}" % f for f in FIELDS)

THEMES = [
    ("cover", (0, 0, 0), (0, 0, 0)),
    ("aurora", (137, 180, 250), (203, 166, 247)),
    ("sunset", (250, 179, 135), (243, 139, 168)),
    ("mint", (148, 226, 213), (166, 227, 161)),
    ("rose", (245, 194, 231), (242, 205, 205)),
    ("mono", (255, 255, 255), (185, 185, 185)),
]

ALIGNS = ["center", "left", "right"]
GRAY_LEVELS = [0.12, 0.18, 0.26, 0.34, 0.42, 0.5, 0.65, 0.8, 1.0]
ACCENT_LEVELS = [0.85, 0.6]
IMG_SCALE = [1.8, 2.6, 3.5]
GFX_DELETE_PLACEMENTS = "\x1b_Ga=d,d=a,q=2\x1b\\"
GFX_DELETE_ALL = "\x1b_Ga=d,d=A,q=2\x1b\\"
MAX_SIZE = 3
RESET = "\x1b[0m"

KEYMAP = {
    "\x1b[A": "up",
    "\x1b[B": "down",
    "\x1bOA": "up",
    "\x1bOB": "down",
    "\x1b[5~": "pgup",
    "\x1b[6~": "pgdn",
    "\x1b[H": "home",
    "\x1b[F": "end",
}


def config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP, "config.toml")


def toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return json.dumps(str(v))


def dump_toml(d: dict) -> str:
    lines = [f"{k} = {toml_value(v)}" for k, v in d.items() if not isinstance(v, dict)]
    for k, v in d.items():
        if isinstance(v, dict):
            lines += ["", f"[{k}]"] + [f"{a} = {toml_value(b)}" for a, b in v.items()]
    return "\n".join(lines) + "\n"


def load_config() -> dict:
    try:
        with open(config_path(), "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(d: dict) -> None:
    try:
        path = config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(dump_toml(d))
        os.replace(tmp, path)
    except OSError:
        pass


def prune_cache(max_age_days: int = 60) -> None:
    cutoff = time.time() - max_age_days * 86400
    for folder in (CACHE_DIR, ART_DIR):
        try:
            for name in os.listdir(folder):
                path = os.path.join(folder, name)
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
        except OSError:
            pass


def wcw(s: str) -> int:
    return sum(
        0 if unicodedata.combining(c) else (2 if unicodedata.east_asian_width(c) in "WF" else 1)
        for c in s
    )


def trunc(s: str, w: int) -> str:
    if wcw(s) <= w:
        return s
    out = ""
    for c in s:
        if wcw(out + c) > w - 1:
            break
        out += c
    return out + "…"


def wrap(text: str, width: int) -> list[str]:
    if wcw(text) <= width:
        return [text]
    rows, cur = [], ""
    for word in text.split(" "):
        cand = word if not cur else cur + " " + word
        if wcw(cand) <= width:
            cur = cand
            continue
        if cur:
            rows.append(cur)
        while wcw(word) > width:
            k = len(word)
            while k > 1 and wcw(word[:k]) > width:
                k -= 1
            rows.append(word[:k])
            word = word[k:]
        cur = word
    if cur:
        rows.append(cur)
    return rows or [""]


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def mix(c1, c2, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(lerp(a, b, t)) for a, b in zip(c1, c2))


def fg(c, bold=False) -> str:
    return f"\x1b[{'1;' if bold else ''}38;2;{c[0]};{c[1]};{c[2]}m"


def gray(h: float):
    g = int(48 + 192 * max(0.0, min(1.0, h)))
    return (g, g, g)


def fmt_time(s: float) -> str:
    s = int(max(0, s))
    return f"{s // 60}:{s % 60:02d}"


def detect_text_sizing(mode: str) -> bool:
    if mode == "on":
        return True
    if mode == "off" or os.environ.get("TMUX"):
        return False
    return bool(os.environ.get("KITTY_WINDOW_ID")) or "kitty" in os.environ.get("TERM", "")


TS_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
OFFSET_RE = re.compile(r"^\[offset:\s*([+-]?\d+)\]", re.I)
ENH_RE = re.compile(r"<\d{1,3}:\d{2}(?:[.:]\d{1,3})?>")


@dataclass
class Lyrics:
    lines: list
    synced: bool
    source: str
    instrumental: bool = False


def parse_lrc(text: str) -> list:
    out, offset = [], 0.0
    for raw in text.splitlines():
        raw = raw.strip()
        m = OFFSET_RE.match(raw)
        if m:
            offset = int(m.group(1)) / 1000.0
            continue
        pos, stamps = 0, []
        while True:
            m = TS_RE.match(raw, pos)
            if not m:
                break
            frac = float("0." + m.group(3)) if m.group(3) else 0.0
            stamps.append(int(m.group(1)) * 60 + int(m.group(2)) + frac)
            pos = m.end()
        if not stamps:
            continue
        txt = ENH_RE.sub("", raw[pos:]).strip()
        for t in stamps:
            out.append((max(0.0, t - offset), txt))
    out.sort(key=lambda x: x[0])
    tidy = []
    for t, txt in out:
        txt = txt or "♪"
        if txt == "♪" and tidy and tidy[-1][1] == "♪":
            continue
        tidy.append((t, txt))
    return tidy


def plain_to_lines(text: str, length: float) -> list:
    lines, prev_blank = [], True
    for line in (x.strip() for x in text.splitlines()):
        if not line and prev_blank:
            continue
        lines.append(line)
        prev_blank = not line
    while lines and not lines[-1]:
        lines.pop()
    n = len(lines)
    length = length or 200.0
    t0, t1 = length * 0.06, length * 0.94
    return [(t0 + (t1 - t0) * i / max(1, n - 1), line) for i, line in enumerate(lines)]


def text_to_lyrics(text: str, length: float, source: str) -> Lyrics | None:
    if not text.strip():
        return None
    lines = parse_lrc(text)
    if len(lines) >= 3:
        return Lyrics(lines, True, source)
    return Lyrics(plain_to_lines(text, length), False, source)


def http_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
            return None
        raise


def clean_title(t: str) -> str:
    t = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", t)
    t = re.sub(r"\s+-\s+.*(remaster|live|version|edit|mono|stereo|mix).*$", "", t, flags=re.I)
    return t.strip() or t


def primary_artist(a: str) -> str:
    return re.split(r",|&| feat\.?| ft\.?| x | and ", a, maxsplit=1, flags=re.I)[0].strip() or a


def pick_best(results, dur):
    best, best_score = None, None
    for r in results or []:
        if not (r.get("syncedLyrics") or r.get("plainLyrics") or r.get("instrumental")):
            continue
        diff = abs((r.get("duration") or 0) - dur) if dur else 0
        if dur and diff > 8:
            continue
        score = (0 if r.get("syncedLyrics") else 1000) + diff
        if best_score is None or score < best_score:
            best, best_score = r, score
    return best


def lrclib_lookup(artist: str, title: str, album: str, dur: float):
    base = "https://lrclib.net/api/"
    query = {"artist_name": artist, "track_name": title}
    if album:
        query["album_name"] = album
    if dur:
        query["duration"] = int(round(dur))
    if album and dur:
        found = http_json(base + "get?" + urllib.parse.urlencode(query))
        if found:
            return found
    first, cleaned = primary_artist(artist), clean_title(title)
    tried = set()
    for a, t in [(artist, title), (first, cleaned), (first, title)]:
        if (a, t) in tried:
            continue
        tried.add((a, t))
        params = urllib.parse.urlencode({"track_name": t, "artist_name": a})
        best = pick_best(http_json(base + "search?" + params), dur)
        if best:
            return best
    params = urllib.parse.urlencode({"q": f"{first} {cleaned}"})
    return pick_best(http_json(base + "search?" + params), dur)


def from_lrclib(data: dict, length: float):
    if data.get("syncedLyrics"):
        lines = parse_lrc(data["syncedLyrics"])
        if lines:
            return Lyrics(lines, True, "lrclib")
    if data.get("instrumental"):
        return Lyrics([], False, "lrclib", instrumental=True)
    if data.get("plainLyrics"):
        return Lyrics(plain_to_lines(data["plainLyrics"], length), False, "lrclib")
    return None


def cache_path(info: dict) -> str:
    key = f"{info['artist']}|{info['title']}|{info['album']}|{int(info['length'])}"
    return os.path.join(CACHE_DIR, hashlib.sha1(key.encode()).hexdigest() + ".json")


def player_text(info: dict) -> str:
    cmd = ["playerctl"]
    if info["player"]:
        cmd += ["-p", info["player"]]
    cmd += ["metadata", "xesam:asText"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def local_path(info: dict) -> str:
    url = info.get("url") or ""
    if not url.startswith("file://"):
        return ""
    return urllib.parse.unquote(urllib.parse.urlparse(url).path)


def local_lrc(info: dict) -> Lyrics | None:
    path = local_path(info)
    if not path:
        return None
    try:
        with open(os.path.splitext(path)[0] + ".lrc", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return None
    return text_to_lyrics(text, info["length"], "local .lrc")


def embedded_text(path: str) -> str:
    try:
        audio = mutagen.File(path)
    except Exception:
        return ""
    tags = getattr(audio, "tags", None)
    if not tags:
        return ""
    found = []
    try:
        if hasattr(tags, "getall"):
            for frame in tags.getall("SYLT"):
                if getattr(frame, "format", 2) == 2:
                    found.append(
                        "\n".join(
                            f"[{ms // 60000:02d}:{(ms % 60000) / 1000:05.2f}]{text.strip()}"
                            for text, ms in frame.text
                        )
                    )
            for frame in tags.getall("USLT"):
                found.append(frame.text)
        else:
            for key in ("SYNCEDLYRICS", "LYRICS", "UNSYNCEDLYRICS", "\xa9lyr"):
                for value in tags.get(key) or []:
                    found.append(value if isinstance(value, str) else value.decode("utf-8", "ignore"))
    except Exception:
        return ""
    found = [t for t in found if t and t.strip()]
    for text in found:
        if len(parse_lrc(text)) >= 3:
            return text
    return found[0] if found else ""


def embedded_lyrics(info: dict) -> Lyrics | None:
    path = local_path(info)
    if HAVE_MUTAGEN and path:
        found = text_to_lyrics(embedded_text(path), info["length"], "embedded lyrics")
        if found:
            return found
    return text_to_lyrics(player_text(info), info["length"], "player metadata")


def lrclib_result(info: dict, use_cache: bool = True) -> Lyrics | None:
    path = cache_path(info)
    if use_cache and os.path.exists(path):
        try:
            with open(path) as f:
                cached = from_lrclib(json.load(f), info["length"])
            if cached:
                cached.source = "lrclib (cached)"
                return cached
        except (OSError, ValueError):
            pass
    data = lrclib_lookup(info["artist"], info["title"], info["album"], info["length"])
    if not data:
        return None
    result = from_lrclib(data, info["length"])
    if result:
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            keep = {k: data.get(k) for k in ("syncedLyrics", "plainLyrics", "instrumental")}
            with open(path, "w") as f:
                json.dump(keep, f)
        except OSError:
            pass
    return result


def resolve(info: dict, prefer_remote: bool, use_cache: bool = True) -> Lyrics | None:
    """Resolve lyrics for a track.

    Default precedence (prefer_remote=False):
        local .lrc  ->  embedded  ->  lrclib
    Remote-first precedence (prefer_remote=True):
        lrclib (no cache)  ->  local .lrc  ->  embedded
    """
    if prefer_remote:
        try:
            remote = lrclib_result(info, use_cache=False)
        except Exception:
            remote = None
        if remote is not None:
            return remote
        # Fall back to local sources if lrclib gave us nothing.
        fallback = None
        for finder in (local_lrc, embedded_lyrics):
            found = finder(info)
            if found is None:
                continue
            if found.synced:
                return found
            if fallback is None:
                fallback = found
        return fallback

    fallback = None
    for finder in (local_lrc, embedded_lyrics):
        found = finder(info)
        if found is None:
            continue
        if found.synced:
            return found
        if fallback is None:
            fallback = found
    try:
        remote = lrclib_result(info, use_cache=use_cache)
    except Exception:
        if fallback is None:
            raise
        remote = None
    if remote is not None and (remote.synced or remote.instrumental or fallback is None):
        return remote
    return fallback


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.has_player = False
        self.status = "Stopped"
        self.pos = 0.0
        self.t = time.monotonic()
        self.key = None
        self.info = {"player": "", "artist": "", "title": "", "album": "", "length": 0.0, "url": ""}
        self.gen = 0
        self.lyrics = None
        self.lyr_state = "idle"
        self.art_url = ""
        self.art_gen = 0
        self.cover = None
        # Tracks the track-key for which the user explicitly asked for lrclib.
        self.remote_key = None

    def now_pos(self, now: float) -> float:
        p = self.pos + ((now - self.t) if self.status == "Playing" else 0.0)
        return max(0.0, p)


def start_fetch(st: State, info: dict, prefer_remote: bool = False, use_cache: bool = True):
    with st.lock:
        st.gen += 1
        gen = st.gen
        st.lyrics = None
        st.lyr_state = "loading" if info["title"] else "none"
    if not info["title"]:
        return

    def work():
        try:
            res = resolve(info, prefer_remote=prefer_remote, use_cache=use_cache)
            state = "ready" if res and (res.lines or res.instrumental) else "none"
        except Exception:
            res, state = None, "error"
        with st.lock:
            if st.gen == gen:
                st.lyrics, st.lyr_state = res, state

    threading.Thread(target=work, daemon=True).start()


@dataclass
class Cover:
    img: object
    c1: tuple
    c2: tuple


def tidy_color(rgb):
    import colorsys

    h, s, v = colorsys.rgb_to_hsv(*(x / 255 for x in rgb))
    if s >= 0.12:
        s = max(0.35, min(0.85, s))
    v = max(v, 0.8)
    return tuple(int(x * 255) for x in colorsys.hsv_to_rgb(h, s, v))


def cover_palette(img):
    import colorsys

    q = img.resize((64, 64)).quantize(colors=10)
    pal = q.getpalette()
    cols = [(n, tuple(pal[i * 3 : i * 3 + 3])) for n, i in q.getcolors()]

    def hsv(rgb):
        return colorsys.rgb_to_hsv(*(x / 255 for x in rgb))

    def score(item):
        n, rgb = item
        _, s, v = hsv(rgb)
        return (n**0.5) * (0.25 + s) * (0.2 + v)

    ranked = sorted(cols, key=score, reverse=True)
    first = ranked[0][1]
    h1, s1, v1 = hsv(first)
    second = None
    for _, rgb in ranked[1:]:
        dh = abs(hsv(rgb)[0] - h1)
        if min(dh, 1 - dh) > 0.07 and hsv(rgb)[2] > 0.2:
            second = rgb
            break
    if second is None:
        second = tuple(int(x * 255) for x in colorsys.hsv_to_rgb((h1 + 0.08) % 1, s1, v1))
    return tidy_color(first), tidy_color(second)


def load_cover(url: str):
    if url.startswith("file://"):
        with open(urllib.parse.unquote(urllib.parse.urlparse(url).path), "rb") as f:
            data = f.read()
    else:
        os.makedirs(ART_DIR, exist_ok=True)
        path = os.path.join(ART_DIR, hashlib.sha1(url.encode()).hexdigest())
        if os.path.exists(path):
            with open(path, "rb") as f:
                data = f.read()
        else:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = r.read(8_000_000)
            with open(path, "wb") as f:
                f.write(data)
    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size
    s = min(w, h)
    x0, y0 = (w - s) // 2, (h - s) // 2
    resample = getattr(Image, "Resampling", Image).LANCZOS
    img = img.crop((x0, y0, x0 + s, y0 + s)).resize((256, 256), resample)
    c1, c2 = cover_palette(img)
    return Cover(img, c1, c2)


def start_art(st: State, url: str):
    with st.lock:
        st.art_gen += 1
        gen = st.art_gen
        if not url or not HAVE_PIL:
            st.cover = None
    if not url or not HAVE_PIL:
        return

    def work():
        try:
            cov = load_cover(url)
        except Exception:
            cov = None
        with st.lock:
            if st.art_gen == gen:
                st.cover = cov

    threading.Thread(target=work, daemon=True).start()


def query_player(player):
    cmd = ["playerctl"] + (["-p", player] if player else ["-a"]) + ["metadata", "--format", FMT]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    best, best_rank = None, 9
    for line in r.stdout.splitlines():
        p = line.split("\t")
        if len(p) < 9:
            continue
        try:
            pos = float(p[2] or 0) / 1e6
            length = float(p[3] or 0) / 1e6
        except ValueError:
            pos = length = 0.0
        d = {
            "player": p[0],
            "status": p[1],
            "pos": pos,
            "length": length,
            "artist": p[4],
            "title": p[5],
            "album": p[6],
            "url": p[7],
            "art": p[8].strip(),
        }
        rank = {"Playing": 0, "Paused": 1}.get(d["status"], 2)
        if rank < best_rank:
            best, best_rank = d, rank
    return best


def poller(st: State, player, stop: threading.Event):
    while not stop.is_set():
        d = query_player(player)
        now = time.monotonic()
        changed = False
        info = None
        art_changed = False
        with st.lock:
            if d is None:
                st.has_player = False
            else:
                st.has_player = True
                art_changed = d["art"] != st.art_url
                st.art_url = d["art"]
                key = (d["artist"], d["title"], d["album"], round(d["length"]))
                changed = key != st.key
                pos = d["pos"]
                if changed:
                    st.key = key
                    st.info = {
                        k: d[k] for k in ("player", "artist", "title", "album", "length", "url")
                    }
                    info = dict(st.info)
                else:
                    st.info["player"] = d["player"]
                    pred = st.now_pos(now)
                    if st.status == "Playing" and d["status"] == "Playing" and abs(pos - pred) < 0.3:
                        pos = pred + (pos - pred) * 0.5
                st.pos, st.t, st.status = pos, now, d["status"]
        if changed and info:
            with st.lock:
                prefer_remote = st.remote_key == st.key
            start_fetch(st, info, prefer_remote=prefer_remote)
        if art_changed and d is not None:
            start_art(st, d["art"])
        stop.wait(0.4)


def detect_graphics(mode: str) -> bool:
    if mode == "on":
        return True
    if mode == "off" or os.environ.get("TMUX"):
        return False
    term = os.environ.get("TERM", "")
    prog = os.environ.get("TERM_PROGRAM", "").lower()
    return (
        bool(os.environ.get("KITTY_WINDOW_ID"))
        or "kitty" in term
        or "ghostty" in term
        or prog in ("ghostty", "wezterm")
    )


def cell_px():
    try:
        packed = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        rows, cols, xp, yp = struct.unpack("HHHH", packed)
        if rows and cols and xp and yp:
            return xp / cols, yp / rows
    except (OSError, ValueError):
        pass
    return None


@dataclass
class FontInfo:
    path: str
    index: int
    family: str
    matched: bool


def find_font(spec):
    if spec and os.path.isfile(spec):
        return FontInfo(spec, 0, os.path.basename(spec), True)
    query = spec or DEFAULT_FONT
    try:
        r = subprocess.run(
            ["fc-match", "-f", "%{file}\t%{index}\t%{family}", query],
            capture_output=True,
            text=True,
            timeout=3,
        )
        path, idx, fam = r.stdout.split("\t", 2)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if not path:
        return None
    wanted = query.split(":")[0].strip().lower()
    return FontInfo(path, int(idx or 0), fam.split(",")[0], wanted in fam.lower())


def list_fonts() -> list:
    try:
        r = subprocess.run(
            ["fc-list", ":", "family"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    names = set()
    for line in r.stdout.splitlines():
        name = line.split(",")[0].strip()
        if name:
            names.add(name)
    return sorted(names, key=str.lower)


def wrap_px(font, text: str, max_w: int) -> list:
    rows, cur = [], ""
    for word in text.split(" "):
        cand = word if not cur else cur + " " + word
        if cur and font.getlength(cand) > max_w:
            rows.append(cur)
            cur = word
        else:
            cur = cand
    if cur:
        rows.append(cur)
    return rows or [""]


def row_x(align: str, w: int, rw: int) -> int:
    if align == "center":
        return (w - rw) // 2
    return w - rw - 2 if align == "right" else 2


def parse_keys(data: str) -> list:
    keys, i = [], 0
    while i < len(data):
        ch = data[i]
        if ch == "\x1b":
            if data[i + 1 : i + 2] in ("[", "O"):
                j = i + 2
                while j < len(data) and not ("@" <= data[j] <= "~"):
                    j += 1
                name = KEYMAP.get(data[i : j + 1])
                if name:
                    keys.append(name)
                i = j + 1
            else:
                keys.append("esc")
                i += 1
        else:
            keys.append(ch)
            i += 1
    return keys


class ImageText:
    def __init__(self, font_info: FontInfo):
        self.fi = font_info
        self.cache = {}
        self.next_id = 1000
        self.sig = None
        self.lyr_ref = None
        self.layout = None
        self.view = 0.0
        self.heat = []
        self.align = "center"

    def flush(self) -> str:
        seq = "".join(f"\x1b_Ga=d,d=I,i={iid},q=2\x1b\\" for iid in self.cache.values())
        self.cache.clear()
        self.sig = None
        return seq

    def render(self, i, kind, level, c1, c2) -> bytes:
        _, line_h, blocks, _ = self.layout
        font = self.layout[0]
        rows, widths, w, h = blocks[i]
        mask = Image.new("L", (w, h), 0)
        d = ImageDraw.Draw(mask)
        for j, txt in enumerate(rows):
            d.text((row_x(self.align, w, widths[j]), j * line_h), txt, font=font, fill=255)
        if kind == "g":
            img = Image.new("RGBA", (w, h), gray(level) + (255,))
        else:
            tint = 1.0 if kind == "s" else max(0.0, (level - 0.45) / 0.55)
            base = gray(level)
            strip = Image.new("RGB", (w, 1))
            strip.putdata(
                [mix(base, mix(c1, c2, x / max(1, w - 1)), tint) for x in range(w)]
            )
            img = strip.resize((w, h), 0).convert("RGBA")
        img.putalpha(mask)
        buf = io.BytesIO()
        img.save(buf, "PNG", compress_level=1)
        return buf.getvalue()

    def get(self, i, kind, level, c1, c2, out, t0):
        key = (i, kind, level)
        iid = self.cache.get(key)
        if iid:
            return iid
        if kind == "g" and time.perf_counter() - t0 > 0.014:
            near = [
                (abs(l - level), v)
                for (ii, k, l), v in self.cache.items()
                if ii == i and k == "g"
            ]
            if near:
                return min(near)[1]
        b64 = base64.standard_b64encode(self.render(i, kind, level, c1, c2)).decode()
        iid = self.next_id
        self.next_id += 1
        chunks = [b64[k : k + 4096] for k in range(0, len(b64), 4096)] or [""]
        for n, c in enumerate(chunks):
            more = 1 if n < len(chunks) - 1 else 0
            head = f"a=t,f=100,i={iid},q=2,m={more}" if n == 0 else f"m={more}"
            out.append(f"\x1b_G{head};{c}\x1b\\")
        self.cache[key] = iid
        return iid

    def draw(self, ui, lyr, pos, dt, W, H, a_top, a_bot, c1, c2) -> str:
        out = []
        cwf, chf = cell_px() or (10.0, 21.0)
        cw, ch = int(round(cwf)), int(round(chf))
        font_px = int(ch * IMG_SCALE[ui.size - 1])
        Wpx = W * cw
        lines = lyr.lines
        n = len(lines)
        times = [t for t, _ in lines]
        sig = (W, H, cw, ch, font_px, ui.align, c1, c2, a_top, self.fi.path, self.fi.index)
        fresh = False
        if sig != self.sig or lyr is not self.lyr_ref:
            out.append(self.flush())
            font = ImageFont.truetype(self.fi.path, font_px, index=self.fi.index)
            max_w = int(Wpx * (0.84 if ui.align == "center" else 0.78))
            line_h, gap = int(font_px * 1.3), int(font_px * 0.55)
            blocks, ys, y = [], [], 0
            for _, tx in lines:
                rows = wrap_px(font, tx.replace("♪", "· · ·"), max_w)
                widths = [int(font.getlength(r)) for r in rows]
                w, h = max(max(widths), 1) + 4, len(rows) * line_h
                blocks.append((rows, widths, w, h))
                ys.append(y)
                y += h + gap
            self.layout = (font, line_h, blocks, ys)
            self.sig, self.lyr_ref, self.align = sig, lyr, ui.align
            fresh = True
        font, line_h, blocks, ys = self.layout

        idx = bisect.bisect_right(times, pos) - 1
        cur = max(idx, 0)
        target = ys[cur] + blocks[cur][3] / 2.0

        def heat_target(i):
            d = i - idx
            if d == 0:
                return 1.0
            return 0.5 * 0.68 ** (d - 1) if d > 0 else 0.34 * 0.6 ** (-d - 1)

        if fresh:
            self.view = target
            self.heat = [heat_target(i) for i in range(n)]
        else:
            if abs(target - self.view) > ch * 25:
                self.view = target
            else:
                self.view += (target - self.view) * (1 - math.exp(-dt * 9))
            k = 1 - math.exp(-dt * 10)
            for i in range(n):
                self.heat[i] += (heat_target(i) - self.heat[i]) * k

        if len(self.cache) > 150:
            for key in sorted(self.cache, key=lambda kk: -abs(kk[0] - idx)):
                if len(self.cache) <= 110 or abs(key[0] - idx) <= 15:
                    break
                out.append(f"\x1b_Ga=d,d=I,i={self.cache.pop(key)},q=2\x1b\\")

        area_top, area_bot = a_top * ch, a_bot * ch
        center = (area_top + area_bot) / 2.0
        margin = cw * max(4, W // 14)
        t0 = time.perf_counter()
        pid = [1]

        def place(iid, ax, ay, sx, sy, sw, sh, z):
            ax, ay, sx, sy, sw, sh = (int(round(v)) for v in (ax, ay, sx, sy, sw, sh))
            if ay < area_top:
                cut = area_top - ay
                sy, sh, ay = sy + cut, sh - cut, area_top
            if ay + sh > area_bot:
                sh = area_bot - ay
            if sh <= 0 or sw <= 0 or ax < 0 or ax >= Wpx:
                return
            row, yo = divmod(ay, ch)
            col, xo = divmod(ax, cw)
            out.append(
                f"\x1b[{row + 1};{col + 1}H\x1b_Ga=p,i={iid},p={pid[0]},"
                f"x={sx},y={sy},w={sw},h={sh},X={xo},Y={yo},z={z},C=1,q=2\x1b\\"
            )
            pid[0] += 1

        for i, (rows, widths, w, h) in enumerate(blocks):
            top = center - self.view + ys[i]
            if top + h <= area_top or top >= area_bot:
                continue
            ax = {"left": margin, "right": Wpx - w - margin}.get(ui.align, (Wpx - w) / 2.0)
            heat = self.heat[i]
            tint = 0.0 if i >= idx else (heat - 0.45) / 0.55
            if i != idx and tint > 0.12:
                kind, level = "a", min(ACCENT_LEVELS, key=lambda l: abs(l - heat))
            else:
                kind, level = "g", min(GRAY_LEVELS, key=lambda l: abs(l - heat))
            place(self.get(i, kind, level, c1, c2, out, t0), ax, top, 0, 0, w, h, 0)
            if i == idx:
                total_w = sum(widths)
                if lyr.synced:
                    t_a = times[i]
                    t_b = times[i + 1] if i + 1 < n else t_a + 4.0
                    total_chars = sum(len(r) for r in rows)
                    wipe = max(0.3, min((t_b - t_a) * 0.9, 0.5 + 0.07 * total_chars))
                    remaining = max(0.0, min(1.0, (pos - t_a) / wipe)) * total_w
                else:
                    remaining = float(total_w)
                if remaining > 0:
                    sid = self.get(i, "s", 0, c1, c2, out, 0.0)
                    for j, rw in enumerate(widths):
                        take = min(rw, remaining)
                        if take > 0:
                            rx = row_x(ui.align, w, rw)
                            place(sid, ax + rx, top + j * line_h, rx, j * line_h, take, line_h, 1)
                        remaining -= rw

        if idx + 1 < n and time.perf_counter() - t0 < 0.012:
            self.get(idx + 1, "g", 1.0, c1, c2, out, t0)
            if time.perf_counter() - t0 < 0.02:
                self.get(idx + 1, "s", 0, c1, c2, out, t0)
        return "".join(out)


class UI:
    def __init__(
        self,
        st: State,
        theme: str,
        offset: float,
        size: int = 1,
        proto: bool = False,
        align: str = "center",
        image: bool = False,
        img=None,
        img_reason: str = "",
        gfx: bool = False,
        font_spec: str = DEFAULT_FONT,
    ):
        self.st = st
        self.gfx = gfx
        self._cover_sent = None
        self._cover_t = 0.0
        self.align = align if align in ALIGNS else "center"
        self.img = img
        self.img_reason = img_reason
        self.font_spec = font_spec
        self.want_image = bool(image)
        self.mode = "image" if (image and img) else "text"
        self.help = False
        self.font_menu = False
        self.fonts = None
        self.font_sel = 0
        self.note = None
        if image and not img:
            self.toast(img_reason or "image mode unavailable here", 8)
        elif self.mode == "image" and not img.fi.matched:
            wanted = font_spec.split(":")[0]
            self.toast(f"Image mode · font '{wanted}' not found, using {img.fi.family} (f to choose)", 7)
        self._img_cleanup = False
        names = [t[0] for t in THEMES]
        self.theme_i = names.index(theme) if theme in names else 0
        self.offset = offset
        self.size = max(1, min(MAX_SIZE, size))
        self.proto = proto
        self.view = 0.0
        self.heat = []
        self._lyr = None
        self._lay = None
        self._size = None

    def theme(self):
        name, a, b = THEMES[self.theme_i]
        if name == "cover":
            with self.st.lock:
                cov = self.st.cover
            return (name, cov.c1, cov.c2) if cov else THEMES[1]
        return name, a, b

    def cover_png_seq(self, cover, S: int) -> str:
        sup = 4
        resample = getattr(Image, "Resampling", Image).LANCZOS
        im = cover.img.resize((S, S), resample).convert("RGBA")
        mask = Image.new("L", (S * sup, S * sup), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, S * sup - 1, S * sup - 1), radius=max(4, S // 9) * sup, fill=255
        )
        im.putalpha(mask.resize((S, S), resample))
        buf = io.BytesIO()
        im.save(buf, "PNG", compress_level=1)
        b64 = base64.standard_b64encode(buf.getvalue()).decode()
        chunks = [b64[k : k + 4096] for k in range(0, len(b64), 4096)]
        seq = []
        for n, c in enumerate(chunks):
            more = 1 if n < len(chunks) - 1 else 0
            head = f"a=t,f=100,i=999,q=2,m={more}" if n == 0 else f"m={more}"
            seq.append(f"\x1b_G{head};{c}\x1b\\")
        return "".join(seq)

    @staticmethod
    def cover_blocks(cover, cols: int, rows: int) -> list:
        resample = getattr(Image, "Resampling", Image).LANCZOS
        small = cover.img.resize((cols, rows * 2), resample)
        px = small.load()
        out = []
        for r in range(rows):
            line = []
            for c in range(cols):
                t, b = px[c, 2 * r], px[c, 2 * r + 1]
                line.append(
                    f"\x1b[38;2;{t[0]};{t[1]};{t[2]}m\x1b[48;2;{b[0]};{b[1]};{b[2]}m▀"
                )
            out.append("".join(line) + RESET)
        return out

    def pad(self, W: int, w: int) -> int:
        margin = max(4, W // 14)
        if self.align == "left":
            return margin
        if self.align == "right":
            return max(0, W - w - margin)
        return max(0, (W - w) // 2)

    def cleanup(self) -> str:
        return GFX_DELETE_ALL if (self.img or self.gfx) else ""

    def toast(self, text: str, secs: float = 2.5):
        self.note = (text, time.monotonic() + secs)

    def settings(self) -> dict:
        return {
            "theme": THEMES[self.theme_i][0],
            "align": self.align,
            "size": self.size,
            "image": self.want_image,
            "font": self.font_spec,
        }

    def save(self):
        cfg = load_config()
        cfg.update(self.settings())
        save_config(cfg)

    def sync_text(self) -> str:
        if abs(self.offset) < 0.05:
            return "on time"
        return f"{abs(self.offset):.1f}s {'sooner' if self.offset > 0 else 'later'}"

    def current_font(self) -> str:
        return self.img.fi.family if self.img else self.font_spec.split(":")[0]

    def open_font_menu(self):
        if self.fonts is None:
            self.fonts = list_fonts()
        current = self.current_font()
        self.font_sel = self.fonts.index(current) if current in self.fonts else 0
        self.font_menu = True
        self.help = False

    def apply_font(self):
        self.font_menu = False
        if not self.fonts:
            return
        spec = f"{self.fonts[self.font_sel]}:bold"
        info = find_font(spec)
        if info is None:
            self.toast("couldn't load that font", 4)
            return
        self.font_spec = spec
        if self.img:
            self.img.fi = info
        elif self.gfx:
            self.img = ImageText(info)
            self.img_reason = ""
        self._lyr = None
        suffix = "" if self.mode == "image" else " (shown in image mode, press i)"
        self.toast(f"Font: {info.family}{suffix}", 4)

    def font_key(self, key: str):
        names = self.fonts or []
        n = len(names)
        if key == "esc":
            self.font_menu = False
        elif key in ("\r", "\n"):
            self.apply_font()
        elif key == "up":
            self.font_sel = max(0, self.font_sel - 1)
        elif key == "down":
            self.font_sel = min(max(0, n - 1), self.font_sel + 1)
        elif key == "pgup":
            self.font_sel = max(0, self.font_sel - 10)
        elif key == "pgdn":
            self.font_sel = min(max(0, n - 1), self.font_sel + 10)
        elif key == "home":
            self.font_sel = 0
        elif key == "end":
            self.font_sel = max(0, n - 1)
        elif len(key) == 1 and key.isprintable() and n:
            for step in range(1, n + 1):
                j = (self.font_sel + step) % n
                if names[j][:1].lower() == key.lower():
                    self.font_sel = j
                    break

    def handle_keys(self, data: str) -> bool:
        before = self.settings()
        for key in parse_keys(data):
            if self.font_menu:
                self.font_key(key)
            elif key in ("q", "Q"):
                self.save()
                return True
            elif key == "esc":
                self.help = False
            elif key in ("?", "h", "H"):
                self.help = not self.help
            elif key == "f":
                self.open_font_menu()
            elif key == "t":
                self.theme_i = (self.theme_i + 1) % len(THEMES)
                self.toast("Theme: " + THEMES[self.theme_i][0])
            elif key == "[":
                self.offset += 0.1
                self.toast("Lyrics timing: " + self.sync_text())
            elif key == "]":
                self.offset -= 0.1
                self.toast("Lyrics timing: " + self.sync_text())
            elif key == "0":
                self.offset = 0.0
                self.toast("Lyrics timing reset")
            elif key == "a":
                self.align = ALIGNS[(ALIGNS.index(self.align) + 1) % len(ALIGNS)]
                self._lyr = None
                self.toast("Lyrics position: " + self.align)
            elif key == "i":
                if self.mode == "image":
                    self.mode, self._img_cleanup, self._lyr = "text", True, None
                    self.want_image = False
                    self.toast("Image mode off")
                elif self.img:
                    self.mode, self._lyr = "image", None
                    self.want_image = True
                    note = "" if self.img.fi.matched else " (requested font not found)"
                    self.toast(f"Image mode on · font: {self.img.fi.family}{note}", 4)
                else:
                    self.toast(self.img_reason or "image mode unavailable here", 6)
            elif key in ("+", "="):
                self.size = min(MAX_SIZE, self.size + 1)
                self.toast(f"Lyrics size: {self.size} of {MAX_SIZE}")
            elif key in ("-", "_"):
                self.size = max(1, self.size - 1)
                self.toast(f"Lyrics size: {self.size} of {MAX_SIZE}")
            elif key == "r":
                with self.st.lock:
                    info = dict(self.st.info)
                start_fetch(self.st, info, prefer_remote=False, use_cache=True)
                self.toast("Searching for lyrics again…")
            elif key == "R":
                with self.st.lock:
                    info = dict(self.st.info)
                    self.st.remote_key = self.st.key
                start_fetch(self.st, info, prefer_remote=True, use_cache=False)
                self.toast("Fetching lyrics from lrclib…")
        if self.settings() != before:
            self.save()
        return False

    def draw_help(self, rows, W, a_top, a_bot, c1, c2):
        if self.mode == "image":
            img_now = "on"
        elif self.img:
            img_now = "off"
        else:
            img_now = "not available in this terminal"
        items = [
            ("t", "Change colour theme", THEMES[self.theme_i][0]),
            ("a", "Move lyrics: centre / left / right", self.align),
            ("+  -", "Make lyrics bigger / smaller", f"{self.size} of {MAX_SIZE}"),
            ("f", "Choose the lyrics font (image mode)", self.current_font()),
            ("i", "Draw lyrics as pictures (smoother, nicer font)", img_now),
            ("[", "Lyrics too late?  Show them sooner", ""),
            ("]", "Lyrics too early?  Show them later", ""),
            ("0", "Reset lyrics timing", self.sync_text()),
            ("r", "Search for lyrics again (local first)", ""),
            ("R", "Force lyrics from lrclib (skip local)", ""),
            ("?", "Show / hide this help", ""),
            ("q", "Quit", ""),
        ]
        keyw = 5
        descw = max(wcw(d) for _, d, _ in items)
        noww = max(wcw(n) for _, _, n in items)
        bw = min(W - 4, keyw + descw + (3 + noww if noww else 0))
        descw = max(8, min(descw, bw - keyw - (3 + noww if noww else 0)))
        light, dim = (205, 205, 215), (120, 120, 132)
        lines = []
        for k, d, n in items:
            d = trunc(d, descw)
            row = fg(c2, True) + k.ljust(keyw) + fg(light) + d + " " * (descw - wcw(d))
            if n:
                row += "   " + fg(c1) + trunc(n, 24)
            lines.append((row + RESET, keyw + descw + (3 + min(wcw(n), 24) if n else 0)))
        title = ("Controls", "Your choices are saved for next time")
        a_h = a_bot - a_top
        if a_h >= len(lines) + 4:
            lines = (
                [(fg(c2, True) + title[0] + RESET, len(title[0])), ("", 0)]
                + lines
                + [("", 0), (fg(dim) + title[1] + RESET, len(title[1]))]
            )
        lines = lines[: max(1, a_h)]
        start = a_top + max(0, (a_h - len(lines)) // 2)
        block_w = max(w for _, w in lines)
        pad = max(0, (W - block_w) // 2)
        for i, (text, _) in enumerate(lines):
            if text:
                rows[start + i] = " " * pad + text

    def draw_font_menu(self, rows, W, a_top, a_bot, c1, c2):
        light, dim = (205, 205, 215), (120, 120, 132)
        area = a_bot - a_top
        names = self.fonts or []
        if not names:
            text = "no fonts found (is fontconfig installed?)"
            rows[a_top + area // 2] = (
                " " * max(0, (W - wcw(text)) // 2) + fg(dim) + text + RESET
            )
            return
        visible = max(1, area - 3)
        first = max(0, min(self.font_sel - visible // 2, len(names) - visible))
        shown = names[first : first + visible]
        width = max(28, min(W - 4, max(wcw(n) for n in shown) + 6))
        current = self.img.fi.family if self.img else ""
        note = (
            f"{self.font_sel + 1} of {len(names)}"
            if self.mode == "image"
            else f"{self.font_sel + 1} of {len(names)} · shown in image mode"
        )
        lines = [fg(c2, True) + "Lyrics font" + fg(dim) + "   " + trunc(note, width - 14) + RESET]
        for i, name in enumerate(shown, start=first):
            label = trunc(name, width - 4)
            mark = "  ●" if name == current else ""
            if i == self.font_sel:
                lines.append(fg(c2, True) + "› " + label + fg(c1) + mark + RESET)
            else:
                lines.append(fg(light) + "  " + label + fg(c1) + mark + RESET)
        lines += ["", fg(dim) + trunc("↑↓ move · letters jump · Enter apply · Esc back", width) + RESET]
        lines = lines[: max(1, area)]
        start = a_top + max(0, (area - len(lines)) // 2)
        pad = max(0, (W - width) // 2)
        for i, text in enumerate(lines):
            if text:
                rows[start + i] = " " * pad + text

    def frame(self, dt: float) -> str:
        st = self.st
        W, H = shutil.get_terminal_size((80, 24))
        with st.lock:
            has, status, info = st.has_player, st.status, dict(st.info)
            lyr, lstate = st.lyrics, st.lyr_state
            cover = st.cover
            pos_raw = st.now_pos(time.monotonic())
        pos = pos_raw + self.offset
        theme_name, c1, c2 = self.theme()

        resized = self._size != (W, H)
        flush_seq = ""
        if resized:
            self._cover_sent = None
            if self.img:
                flush_seq = self.img.flush()

        rows = [""] * H
        full = H >= 12

        cover_rows = 5
        cover_on = bool(cover) and has and bool(info["title"]) and H >= 18 and W >= 50
        cover_col = max(3, W // 14)
        cover_cols = tx = S = 0
        if cover_on:
            if self.gfx:
                cwf, chf = cell_px() or (10.0, 21.0)
                S = cover_rows * int(round(chf))
                cover_cols = int(math.ceil(S / max(1, int(round(cwf)))))
            else:
                cover_cols = cover_rows * 2
            tx = cover_col + cover_cols + 2
        a_top = (7 if cover_on else 4) if full else 0
        a_bot = H - 4 if full else H - 1
        a_h = max(1, a_bot - a_top)

        img_seq = ""

        def put(r, ansi, plain_w, aligned=False):
            if 0 <= r < H:
                p = self.pad(W, plain_w) if aligned else max(0, (W - plain_w) // 2)
                rows[r] = " " * p + ansi

        def msg(text, col=(150, 150, 160)):
            text = trunc(text, W - 4)
            put(a_top + a_h // 2, fg(col) + text + RESET, wcw(text))

        if full and has and info["title"]:
            icon = "▶" if status == "Playing" else "⏸"
            sub = " · ".join(x for x in (info["artist"], info["album"]) if x)
            if cover_on:
                room = W - tx - 3
                title = trunc(info["title"], room)
                sub = trunc(f"{icon}  {sub}", room)
                blocks = None if self.gfx else self.cover_blocks(cover, cover_cols, cover_rows)
                for k in range(cover_rows):
                    left = " " * cover_col + (blocks[k] if blocks else " " * cover_cols) + "  "
                    if blocks is None:
                        left = " " * tx
                    text = ""
                    if k == 1:
                        text = fg(c2, True) + title + RESET
                    elif k == 2:
                        text = fg((140, 140, 152)) + sub + RESET
                    rows[1 + k] = left + text
            else:
                title = trunc(info["title"], W - 6)
                put(1, fg(c2, True) + title + RESET, wcw(title), True)
                sub = trunc(f"{icon}  {sub}", W - 6)
                put(2, fg((140, 140, 152)) + sub + RESET, wcw(sub), True)

        drawing = False
        if self.font_menu:
            self.draw_font_menu(rows, W, a_top, a_bot, c1, c2)
        elif self.help:
            self.draw_help(rows, W, a_top, a_bot, c1, c2)
        elif not has:
            msg("waiting for a player…  (start Spotify, mpv, a browser tab, …)")
        elif not info["title"]:
            msg("nothing playing")
        elif lstate in ("loading", "idle"):
            msg("searching lyrics" + "." * (int(time.monotonic() * 3) % 4))
        elif lstate == "error":
            msg("couldn't reach lrclib — check your connection (r to retry)")
        elif lyr is None or (not lyr.lines and not lyr.instrumental):
            msg("no lyrics found")
        elif lyr.instrumental:
            msg("♪   instrumental   ♪", c2)
        else:
            drawing = True
            used_img = False
            if self.mode == "image" and self.img:
                try:
                    img_seq = self.img.draw(self, lyr, pos, dt, W, H, a_top, a_bot, c1, c2)
                    used_img = True
                except Exception as e:
                    self.mode, self._img_cleanup, self._lyr = "text", True, None
                    self.toast(f"image mode failed ({type(e).__name__}: {e})", 8)
            if not used_img:
                self.draw_lyrics(rows, W, H, lyr, pos, dt, a_top, a_bot, a_h, c1, c2)
        if not drawing:
            self._lyr = None

        if full and has and info["title"]:
            length = info["length"]
            if length > 0:
                bw = max(10, min(W - 16, 60))
                frac = max(0.0, min(1.0, pos_raw / length))
                head = min(bw - 1, int(frac * bw))
                bar = ""
                for i in range(bw):
                    if i < head:
                        bar += fg(mix(c1, c2, i / max(1, bw - 1))) + "━"
                    elif i == head:
                        bar += fg(c2, True) + "●"
                    else:
                        bar += fg((62, 62, 72)) + "─"
                a = fmt_time(pos_raw)
                b = fmt_time(length)
                line = fg((140, 140, 152)) + a + " " + bar + fg((140, 140, 152)) + " " + b + RESET
                put(H - 3, line, len(a) + len(b) + bw + 2)
        if full:
            if self.note and time.monotonic() < self.note[1]:
                text, col = self.note[0], c2
            else:
                parts = []
                if lstate == "ready" and lyr is not None and lyr.lines:
                    parts.append(lyr.source + ("" if lyr.synced else " · not synced, timing estimated"))
                if self.font_menu:
                    parts.append("press Esc to go back")
                else:
                    parts.append("press ? to close help" if self.help else "press ? for help")
                text, col = "   ·   ".join(parts), (112, 112, 124)
            text = trunc(text, W - 2)
            put(H - 1, fg(col) + text + RESET, wcw(text))

        out = ["\x1b[?2026h"]
        if self._size != (W, H):
            out.append("\x1b[2J")
            self._size = (W, H)
        if drawing and self.mode == "text" and self.proto and self.size > 1:
            for r in range(H):
                out.append(f"\x1b[{r + 1};1H\x1b[2K")
            for r in range(H):
                if rows[r]:
                    out.append(f"\x1b[{r + 1};1H{rows[r]}{RESET}")
        else:
            for r in range(H):
                out.append(f"\x1b[{r + 1};1H{rows[r]}{RESET}\x1b[K")
        if self.gfx or self.img:
            gseq = [GFX_DELETE_PLACEMENTS, flush_seq]
            if self._img_cleanup:
                if self.img:
                    gseq.append(self.img.flush())
                self._img_cleanup = False
            gseq.append(img_seq)
            if cover_on and self.gfx:
                key = (id(cover), S)
                nowm = time.monotonic()
                if self._cover_sent != key or nowm - self._cover_t > 20:
                    gseq.append(self.cover_png_seq(cover, S))
                    self._cover_sent, self._cover_t = key, nowm
                gseq.append(f"\x1b[2;{cover_col + 1}H\x1b_Ga=p,i=999,p=999,z=0,C=1,q=2\x1b\\")
            out.append("".join(gseq))
        out.append("\x1b[?2026l")
        return "".join(out)

    def draw_lyrics(self, rows, W, H, lyr, pos, dt, a_top, a_bot, a_h, c1, c2):
        lines = lyr.lines
        n = len(lines)
        times = [t for t, _ in lines]
        idx = bisect.bisect_right(times, pos) - 1
        s = self.size if self.proto else 1
        spaced = self.size > 1 and not self.proto
        unit = s if not spaced else 2
        gap = 1 if self.size == 1 else 2
        avail = W - 6 if self.align == "center" else W - 2 * max(4, W // 14)
        wrapw = max(6, min(avail // unit, 76))
        lkey = (wrapw, s, spaced, gap)

        if self._lyr is not lyr or self._lay[0] != lkey:
            blocks = [wrap(tx, wrapw) for _, tx in lines]
            ys, y = [], 0
            for b in blocks:
                ys.append(y)
                y += len(b) * s + gap
            self._lay = (lkey, blocks, ys)
            fresh = True
        else:
            fresh = False
        _, blocks, ys = self._lay

        cur = max(idx, 0)
        target = ys[cur] + (len(blocks[cur]) * s - 1) / 2.0

        def heat_target(i):
            d = i - idx
            if d == 0:
                return 1.0
            if d > 0:
                return 0.5 * 0.68 ** (d - 1)
            return 0.34 * 0.6 ** (-d - 1)

        if fresh or self._lyr is not lyr:
            self.view = target
            self.heat = [heat_target(i) for i in range(n)]
            self._lyr = lyr
        else:
            if abs(target - self.view) > 25:
                self.view = target
            else:
                self.view += (target - self.view) * (1 - math.exp(-dt * 9))
            k = 1 - math.exp(-dt * 10)
            for i in range(n):
                self.heat[i] += (heat_target(i) - self.heat[i]) * k

        center = a_top + (a_h - 1) / 2.0
        for i, block in enumerate(blocks):
            top = int(round(center - self.view + ys[i]))
            if top + len(block) * s <= a_top or top >= a_bot:
                continue
            heat = self.heat[i]
            total = sum(len(r) for r in block)
            is_cur = i == idx
            if is_cur:
                if lyr.synced:
                    t0 = times[i]
                    t1 = times[i + 1] if i + 1 < n else t0 + 4.0
                    wipe = max(0.3, min((t1 - t0) * 0.9, 0.5 + 0.07 * total))
                    nfill = max(0.0, min(1.0, (pos - t0) / wipe)) * total
                else:
                    nfill = float(total)
            tint = 0.0 if i > idx else max(0.0, (heat - 0.45) / 0.55)

            k = 0
            for ri, text in enumerate(block):
                r = top + ri * s
                if r < a_top or r + s > a_bot:
                    k += len(text)
                    continue
                buf = []
                last = len(text) - 1
                for ci, ch in enumerate(text):
                    ccol = mix(c1, c2, k / max(1, total - 1))
                    base = gray(heat)
                    m = max(0.0, min(1.0, nfill - k)) if is_cur else tint
                    tok = ch
                    if s > 1:
                        tok = f"\x1b]66;s={s};{ch}\x07"
                    elif spaced and ci < last:
                        tok = ch + " "
                    buf.append(fg(mix(base, ccol, m), is_cur) + tok)
                    k += 1
                if s > 1:
                    w = wcw(text) * s
                else:
                    w = wcw(text) * 2 - 1 if spaced else wcw(text)
                rows[r] = " " * self.pad(W, w) + "".join(buf) + RESET


EPILOG = """\
inside the app:  press ? for the controls (theme, size, position, font, image mode, timing).
                 press r to re-search (local first), R to force lrclib (skips local sources).
files:           ~/.config/lyr/config.toml  your saved settings
                 ~/.cache/lyr/              downloaded lyrics and cover art
lyrics order:    .lrc next to the song file, lyrics embedded in the file (mutagen, or the
                 player's own metadata), then lrclib.net.  Pressing R skips straight to
                 lrclib and re-downloads (ignoring the cache) for the current track.
"""


def main():
    ap = argparse.ArgumentParser(
        prog=APP,
        description="Live, synced lyrics for whatever is playing (MPRIS + lrclib.net).",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument(
        "-p", "--player", help="playerctl player name, e.g. spotify (default: whichever is playing)"
    )
    ap.add_argument(
        "--theme",
        default=None,
        choices=[t[0] for t in THEMES],
        help="colour theme; 'cover' (default) takes its colours from the album art",
    )
    ap.add_argument(
        "--offset", type=float, default=0.0, help="shift lyrics earlier by N seconds (can be negative)"
    )
    ap.add_argument(
        "--fps", type=int, default=None, metavar="N", help="frames per second, 5-120 (default: 30)"
    )
    ap.add_argument(
        "-s",
        "--size",
        type=int,
        default=None,
        choices=range(1, MAX_SIZE + 1),
        help="lyric size 1-3 (live: - and +). Real scaling needs kitty >= 0.40",
    )
    ap.add_argument(
        "--text-sizing",
        choices=["auto", "on", "off"],
        default="auto",
        help="force kitty text-sizing protocol on/off (default: auto-detect)",
    )
    ap.add_argument("--align", choices=ALIGNS, default=None, help="lyric alignment (live: a)")
    ap.add_argument(
        "--image",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="render lyrics as images via the kitty graphics protocol (live: i); needs Pillow",
    )
    ap.add_argument(
        "--reset-settings",
        action="store_true",
        help="ignore and clear the saved settings in config.toml",
    )
    ap.add_argument(
        "--font",
        help='font for image mode: path or fontconfig name (live: f; default "Helvetica:bold")',
    )
    ap.add_argument(
        "--graphics",
        choices=["auto", "on", "off"],
        default="auto",
        help="force kitty graphics protocol on/off (default: auto-detect)",
    )
    args = ap.parse_args()

    if not shutil.which("playerctl"):
        sys.exit("lyr: playerctl not found. Install it with: sudo pacman -S playerctl")
    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        sys.exit("lyr: needs an interactive terminal")
    threading.Thread(target=prune_cache, daemon=True).start()

    if args.reset_settings:
        save_config({})
    cfg = {} if args.reset_settings else load_config()
    names = [t[0] for t in THEMES]
    theme = args.theme or (cfg.get("theme") if cfg.get("theme") in names else "cover")
    align = args.align or (cfg.get("align") if cfg.get("align") in ALIGNS else "center")
    size = args.size or (cfg.get("size") if cfg.get("size") in range(1, MAX_SIZE + 1) else 1)
    want_image = args.image if args.image is not None else bool(cfg.get("image", False))
    cfg_font = cfg.get("font")
    font_spec = args.font or (cfg_font if isinstance(cfg_font, str) and cfg_font else DEFAULT_FONT)
    fps = args.fps or cfg.get("fps", 30)
    if not isinstance(fps, int) or isinstance(fps, bool):
        fps = 30

    st = State()
    stop = threading.Event()
    threading.Thread(target=poller, args=(st, args.player, stop), daemon=True).start()
    img, reason = None, ""
    graphics_ok = detect_graphics(args.graphics)
    gfx = graphics_ok and HAVE_PIL
    if not graphics_ok:
        reason = "image mode needs a kitty-graphics terminal (kitty, ghostty, wezterm); try --graphics on"
    elif not HAVE_PIL:
        reason = "image mode needs Pillow: sudo pacman -S python-pillow"
    else:
        info = find_font(font_spec)
        if info:
            img = ImageText(info)
        else:
            reason = "couldn't resolve a font (is fontconfig installed?) - press f or pass --font"
    ui = UI(
        st,
        theme,
        args.offset,
        size,
        detect_text_sizing(args.text_sizing),
        align,
        want_image,
        img,
        reason,
        gfx,
        font_spec,
    )
    ui.save()

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: sys.exit(0))
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    out = sys.stdout
    frame_t = 1.0 / max(5, min(120, fps))
    try:
        tty.setcbreak(fd)
        out.write("\x1b[?1049h\x1b[?25l\x1b[2J")
        out.flush()
        last = time.monotonic()
        while True:
            now = time.monotonic()
            dt = min(0.1, now - last)
            last = now
            out.write(ui.frame(dt))
            out.flush()
            timeout = max(0.0, frame_t - (time.monotonic() - now))
            ready, _, _ = select.select([fd], [], [], timeout)
            if ready:
                data = os.read(fd, 32).decode(errors="ignore")
                if ui.handle_keys(data):
                    break
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        stop.set()
        ui.save()
        try:
            out.write(ui.cleanup() + "\x1b[0m\x1b[?25h\x1b[?1049l")
            out.flush()
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except (OSError, ValueError, termios.error):
            pass


if __name__ == "__main__":
    main()
