"""
Stock Ticker Display  -  Waveshare ESP32-C6-LCD-1.47 (ST7789 172x320)
=====================================================================
Features
  * AP setup mode  : first boot (or bad WiFi) -> board hosts hotspot
                     "StockTicker-Setup", open http://192.168.4.1 to enter
                     your WiFi + tickers from a phone browser.
  * STA run mode   : connects to your router, shows its LAN IP on screen.
  * Web config     : while running, browse to the IP shown on screen to
                     edit tickers / delays / wifi any time. Saves to flash.
  * Persistent     : config.json lives in flash -> survives power loss.
  * Decoupled fetch: background refresh of ALL tickers every refresh_interval;
                     screen rotates cached prices every rotate_delay so we
                     never hit API rate limits. Quotes come from Yahoo Finance
                     (no API key needed).
  * Tap / button   : tap the screen (Touch board) or press the BOOT button.
                     After dim_timeout idle the screen dims to dim_brightness;
                     a tap wakes it to full brightness; tapping again opens an
                     on-device CONFIG screen showing the IP to edit settings.

No SD card required. Pins match Waveshare's official board definition.
"""

import json
import time
import gc
import network
import socket
import _thread
from machine import Pin, SPI, PWM, SoftI2C, reset

import st7789py as st7789
import vga2_bold_16x32 as bigfont
import vga1_8x16 as smallfont

try:
    import urequests as requests
except ImportError:
    import requests

# ----------------------------------------------------------- board pins
LCD_MOSI = 6
LCD_SCLK = 7
LCD_CS = 14
LCD_DC = 15
LCD_RST = 21
LCD_BL = 22
SD_CS = 4
X_OFFSET = 34
Y_OFFSET = 0

# Capacitive-touch controller (only present on the *Touch* board variant).
# We auto-detect it at boot; if absent we fall back to the BOOT button,
# which acts as the "tap" in all the touch interactions below.
TP_SDA = 18
TP_SCL = 19
TP_RST = 20
TP_INT = 21
TOUCH_ADDR = 0x63          # AXS5106L
BOOT_BTN = 9               # onboard BOOT button (active-low) = tap fallback

BLACK = st7789.BLACK
WHITE = st7789.WHITE
GREEN = st7789.color565(0, 220, 0)
RED = st7789.color565(255, 40, 40)
GREY = st7789.color565(140, 140, 140)
YELLOW = st7789.color565(255, 210, 0)
CYAN = st7789.color565(0, 200, 230)
# UI palette for the redesigned screens
NAVY = st7789.color565(14, 22, 46)      # header band
DGREEN = st7789.color565(0, 78, 32)     # up panel background
DRED = st7789.color565(96, 16, 16)      # down panel background
DOTOFF = st7789.color565(55, 60, 70)    # inactive page dot

CONFIG_PATH = "/config.json"
AP_SSID = "StockTicker-Setup"
AP_PASSWORD = "ticker123"          # >=8 chars; change if you like

DEFAULT_CONFIG = {
    "wifi": {"ssid": "", "password": ""},
    "tickers": ["AAPL", "MSFT", "NVDA", "TSLA", "SPY"],
    "rotate_delay": 5,
    "refresh_interval": 60,      # seconds between refreshes while market is open
    "closed_interval": 600,      # slower refresh while market is closed
    "brightness": 100,
    "dim_timeout": 300,     # seconds of no-tap before the screen dims
    "dim_brightness": 20,   # brightness % while dimmed
    "rotation": 0,          # 0/2 = portrait, 1/3 = landscape
    "title": "STOCK TICKER",    # boot splash title
    "subtitle": "",             # boot splash subtitle (optional)
    "splash_secs": 3,           # how long the boot splash stays up
}

# US-equity market sessions (label + badge color), set by fetcher / read by UI.
EPOCH_OFFSET = 946684800   # seconds between 1970 (Unix) and 2000 (MicroPython) epochs
NTP_SYNCED = False

# shared state (read by display loop, written by web server / fetcher)
STATE = {
    "cfg": None,
    "cache": {},        # symbol -> (price, pct, chg, session)
    "ip": "",
    "mode": "boot",     # boot | ap | sta
    "session": "REGULAR",
    "reboot": False,
}
LOCK = _thread.allocate_lock()


# ----------------------------------------------------------- config I/O
def load_config():
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    # merge defaults so missing keys never crash us
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    if "wifi" not in merged or not isinstance(merged["wifi"], dict):
        merged["wifi"] = dict(DEFAULT_CONFIG["wifi"])
    return merged


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f)


# ----------------------------------------------------------- time / market
def sync_time():
    """Sync the RTC over NTP so we can tell which US-market session we're in."""
    global NTP_SYNCED
    try:
        import ntptime
        ntptime.host = "pool.ntp.org"
        ntptime.settime()
        NTP_SYNCED = True
        print("ntp synced, unix =", now_unix())
    except Exception as e:
        NTP_SYNCED = False
        print("ntp err", e)
    return NTP_SYNCED


def now_unix():
    """Current Unix epoch (seconds), or None if the clock isn't NTP-synced."""
    if not NTP_SYNCED:
        return None
    return time.time() + EPOCH_OFFSET


def classify_session(now, ctp):
    """Map the current time onto Yahoo's pre/regular/post trading windows.
    Returns one of REGULAR / PRE / POST / CLOSED."""
    if now is None or not ctp:
        return "REGULAR"      # no clock -> behave like normal hours
    for name, label in (("regular", "REGULAR"), ("pre", "PRE"), ("post", "POST")):
        p = ctp.get(name) or {}
        s, e = p.get("start"), p.get("end")
        if s and e and s <= now < e:
            return label
    return "CLOSED"


# ----------------------------------------------------------- display
# 172x320 is not a built-in size in the driver, so we supply the rotation table
# ourselves. Tuple = (madctl, width, height, xstart, ystart, needs_swap).
# The 172-wide panel sits centered in the 240-col controller -> 34px col offset.
C6_172x320_ROTATIONS = (
    (0x00, 172, 320, 34, 0, False),   # 0: portrait
    (0x60, 320, 172, 0, 34, False),   # 1: landscape
    (0xc0, 172, 320, 34, 0, False),   # 2: inverted portrait
    (0xa0, 320, 172, 0, 34, False),   # 3: inverted landscape
)

BL = None      # backlight PWM handle (set by init_display, used by set_brightness)


def set_brightness(pct):
    if BL is None:
        return
    pct = max(0, min(100, int(pct)))
    BL.duty_u16(int(pct / 100 * 65535))


def init_display(cfg):
    global BL
    Pin(SD_CS, Pin.OUT).value(1)   # park TF-card CS high (official board init)
    spi = SPI(1, baudrate=40_000_000, polarity=0, phase=0,
              sck=Pin(LCD_SCLK), mosi=Pin(LCD_MOSI))
    tft = st7789.ST7789(
        spi, 172, 320,
        reset=Pin(LCD_RST, Pin.OUT),
        dc=Pin(LCD_DC, Pin.OUT),
        cs=Pin(LCD_CS, Pin.OUT),
        rotation=cfg.get("rotation", 0),
        custom_rotations=C6_172x320_ROTATIONS,
    )
    BL = PWM(Pin(LCD_BL))
    BL.freq(1000)
    set_brightness(cfg.get("brightness", 100))
    tft.fill(BLACK)
    return tft


def _center(tft, font, msg, y, color, bg=BLACK):
    w = len(msg) * font.WIDTH
    x = max(0, (tft.width - w) // 2)
    tft.text(font, msg, x, y, color, bg)


def _center_in(tft, font, msg, x0, w, y, color, bg=BLACK):
    """Center text horizontally inside the [x0, x0+w) band."""
    tw = len(msg) * font.WIDTH
    x = x0 + max(0, (w - tw) // 2)
    tft.text(font, msg, x, y, color, bg)


def _header(tft, title, accent=CYAN, h=50):
    """Top title band used by every screen for a consistent look."""
    tft.fill_rect(0, 0, tft.width, h, NAVY)
    _center(tft, bigfont, title, (h - bigfont.HEIGHT) // 2, WHITE, NAVY)
    tft.fill_rect(0, h, tft.width, 4, accent)


def _dots(tft, pos, total, y):
    """Page indicator: one little bar per ticker, current one highlighted."""
    if total <= 1:
        return
    shown = min(total, 8)
    pos = pos % shown
    step = 16
    span = shown * step - (step - 10)
    x0 = max(0, (tft.width - span) // 2)
    for i in range(shown):
        tft.fill_rect(x0 + i * step, y, 10, 6, CYAN if i == pos else DOTOFF)


def _sparkline(tft, series, x, y, w, h, color, base=None):
    """Draw a simple intraday trend line inside the box (x,y,w,h).
    `base` (e.g. previous close) is drawn as a dotted reference line."""
    tft.rect(x, y, w, h, st7789.color565(40, 46, 60))   # subtle frame
    pts = [v for v in (series or []) if v is not None]
    n = len(pts)
    if n < 2:
        _center_in(tft, smallfont, "no chart", x, w, y + (h - smallfont.HEIGHT) // 2, GREY)
        return
    lo = min(pts)
    hi = max(pts)
    if base is not None:
        lo = min(lo, base)
        hi = max(hi, base)
    rng = hi - lo
    if rng <= 0:
        rng = 1.0
    ix, iy, iw, ih = x + 2, y + 2, w - 4, h - 4

    def px(i):
        return ix + int(i * (iw - 1) / (n - 1))

    def py(v):
        return iy + int((hi - v) * (ih - 1) / rng)

    # dotted baseline at the previous close
    if base is not None and lo <= base <= hi:
        yb = py(base)
        xx = ix
        while xx < ix + iw:
            tft.pixel(xx, yb, st7789.color565(90, 90, 100))
            xx += 4

    # the trend itself (2px thick for legibility)
    prevx, prevy = px(0), py(pts[0])
    for i in range(1, n):
        cx, cy = px(i), py(pts[i])
        tft.line(prevx, prevy, cx, cy, color)
        tft.line(prevx, prevy + 1, cx, cy + 1, color)
        prevx, prevy = cx, cy


def _wrap(text, max_chars):
    """Greedy word-wrap into lines of at most max_chars characters."""
    words = text.split(" ")
    lines, cur = [], ""
    for w in words:
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= max_chars:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def show_splash(tft, title, subtitle=""):
    """Configurable boot title screen. Big centered title (wrapped to fit),
    accent rule, optional subtitle, all orientation-aware."""
    tft.fill(BLACK)
    W, H = tft.width, tft.height
    title = (title or "STOCK TICKER").strip()
    # pick the largest font whose wrapped title fits in <=3 lines
    big_cols = max(1, W // bigfont.WIDTH)
    big_lines = _wrap(title, big_cols)
    if len(big_lines) <= 3:
        font, lines = bigfont, big_lines
    else:
        font = smallfont
        lines = _wrap(title, max(1, W // smallfont.WIDTH))[:4]

    block_h = len(lines) * (font.HEIGHT + 4)
    has_sub = bool((subtitle or "").strip())
    total_h = block_h + (4 + 24 if has_sub else 0)
    y = max(8, (H - total_h) // 2)

    for ln in lines:
        _center(tft, font, ln, y, CYAN)
        y += font.HEIGHT + 4

    # accent rule under the title
    rule_w = min(W - 24, 200)
    tft.fill_rect((W - rule_w) // 2, y + 2, rule_w, 4, GREEN)
    y += 12

    if has_sub:
        for ln in _wrap(subtitle.strip(), max(1, (W - 16) // smallfont.WIDTH))[:2]:
            _center(tft, smallfont, ln, y, GREY)
            y += smallfont.HEIGHT + 2


def show_message(tft, lines):
    """lines = list of (text, color)."""
    tft.fill(BLACK)
    _header(tft, "TICKER", CYAN)
    y = 70 if tft.height < 200 else 130
    for txt, col in lines:
        _center(tft, smallfont, txt, y, col)
        y += 24


def show_setup_screen(tft):
    tft.fill(BLACK)
    _header(tft, "SETUP", YELLOW)
    land = tft.height < 200
    if land:
        _center(tft, smallfont, "Join Wi-Fi  " + AP_SSID, 70, CYAN)
        _center(tft, smallfont, "pass: " + AP_PASSWORD, 92, GREY)
        _center(tft, smallfont, "then open  192.168.4.1", 124, WHITE)
        return
    _center(tft, smallfont, "1. Join Wi-Fi", 76, WHITE)
    tft.fill_rect(10, 98, 152, 56, NAVY)
    _center(tft, smallfont, AP_SSID, 110, CYAN, NAVY)
    _center(tft, smallfont, "pass: " + AP_PASSWORD, 132, GREY, NAVY)
    _center(tft, smallfont, "2. Open in browser", 182, WHITE)
    tft.fill_rect(10, 204, 152, 28, NAVY)
    _center(tft, smallfont, "192.168.4.1", 210, CYAN, NAVY)
    _center(tft, smallfont, "to set Wi-Fi + stocks", 252, GREY)


def show_config_screen(tft, ip, ssid=""):
    """On-device config screen (shown after a tap / BOOT press). The web
    server is already running, so this just points you to the IP to edit."""
    tft.fill(BLACK)
    _header(tft, "CONFIG", YELLOW)
    land = tft.height < 200
    if land:
        _center(tft, smallfont, "Open in browser", 68, WHITE)
        tft.fill_rect(8, 90, tft.width - 16, 28, NAVY)
        _center(tft, smallfont, ip or "no wifi", 96, CYAN, NAVY)
        _center(tft, smallfont, "press button to exit", 134, GREY)
        return
    _center(tft, smallfont, "Open in browser", 80, WHITE)
    tft.fill_rect(8, 104, 156, 28, NAVY)
    _center(tft, smallfont, ip or "no wifi", 110, CYAN, NAVY)
    if ssid:
        _center(tft, smallfont, "Wi-Fi: " + ssid, 154, GREY)
    _center(tft, smallfont, "edit stocks, wifi,", 196, WHITE)
    _center(tft, smallfont, "brightness & timing", 218, WHITE)
    tft.fill_rect(0, 286, 172, 28, NAVY)
    _center(tft, smallfont, "press button to exit", 292, GREY, NAVY)


def _fit_price(price):
    return "%.2f" % price


def draw_ticker(tft, symbol, data, pos=0, total=1, session="REGULAR"):
    tft.fill(BLACK)

    up = True
    if data is not None:
        pct = data[1] if len(data) > 1 else None
        up = (pct is None) or (pct >= 0)
    accent = GREEN if up else RED if data is not None else CYAN
    tri = "\x1e" if up else "\x1f"          # CP437 up/down triangles

    if tft.height < 200:
        _draw_ticker_landscape(tft, symbol, data, pos, total, session, up, accent, tri)
    else:
        _draw_ticker_portrait(tft, symbol, data, pos, total, session, up, accent, tri)


def _draw_ticker_portrait(tft, symbol, data, pos, total, session, up, accent, tri):
    W = tft.width
    _header(tft, symbol, accent)

    if data is None:
        tft.fill_rect(10, 150, W - 20, 40, NAVY)
        _center(tft, bigfont, "no data", 154, GREY, NAVY)
        _dots(tft, pos, total, 300)
        return

    price = data[0]
    pct = data[1] if len(data) > 1 else 0.0
    chg = data[2] if len(data) > 2 else None
    if pct is None:
        pct = 0.0

    price_str = _fit_price(price)
    if len(price_str) * bigfont.WIDTH <= W - 4:
        _center(tft, bigfont, price_str, 96, WHITE)
    else:
        _center(tft, smallfont, price_str, 104, WHITE)

    panel_bg = DGREEN if up else DRED
    tft.fill_rect(8, 168, W - 16, 76, panel_bg)
    _center(tft, bigfont, "%s%.2f%%" % (tri, abs(pct)), 176, WHITE, panel_bg)
    if chg is not None:
        _center(tft, smallfont, "%+.2f today" % chg, 218, WHITE, panel_bg)

    # intraday trend sparkline
    spark = data[4] if len(data) > 4 else None
    base = (price - chg) if (chg is not None) else None
    _sparkline(tft, spark, 8, 252, W - 16, 44, accent, base)

    _dots(tft, pos, total, 304)


def _draw_ticker_landscape(tft, symbol, data, pos, total, session, up, accent, tri):
    W = tft.width            # 320
    H = tft.height           # 172
    _header(tft, symbol, accent, h=36)

    if data is None:
        tft.fill_rect(20, 80, W - 40, 36, NAVY)
        _center(tft, bigfont, "no data", 84, GREY, NAVY)
        _dots(tft, pos, total, H - 12)
        return

    price = data[0]
    pct = data[1] if len(data) > 1 else 0.0
    chg = data[2] if len(data) > 2 else None
    if pct is None:
        pct = 0.0

    half = W // 2
    # top-left: price
    price_str = _fit_price(price)
    _center_in(tft, bigfont, price_str, 0, half, 58, WHITE)
    _center_in(tft, smallfont, "USD", 0, half, 92, GREY)

    # top-right: change panel
    panel_bg = DGREEN if up else DRED
    tft.fill_rect(half, 44, W - half - 6, 60, panel_bg)
    _center_in(tft, bigfont, "%s%.2f%%" % (tri, abs(pct)), half, W - half, 52, WHITE, panel_bg)
    if chg is not None:
        _center_in(tft, smallfont, "%+.2f today" % chg, half, W - half, 86, WHITE, panel_bg)

    # bottom: full-width intraday trend sparkline
    spark = data[4] if len(data) > 4 else None
    base = (price - chg) if (chg is not None) else None
    _sparkline(tft, spark, 6, 112, W - 12, 44, accent, base)

    _dots(tft, pos, total, H - 10)


# ----------------------------------------------------------- WiFi
def wifi_reset():
    """Put the WiFi driver into a known-clean state before (re)connecting.
    Avoids 'Wifi Internal State Error' after switching AP<->STA."""
    try:
        ap = network.WLAN(network.AP_IF)
        ap.active(False)
    except Exception:
        pass
    try:
        sta = network.WLAN(network.STA_IF)
        if sta.active():
            try:
                sta.disconnect()
            except Exception:
                pass
        sta.active(False)
    except Exception:
        pass
    time.sleep(0.3)


def start_sta(cfg, tft):
    ssid = cfg["wifi"].get("ssid", "")
    pwd = cfg["wifi"].get("password", "")
    if not ssid:
        return None
    wifi_reset()
    wlan = network.WLAN(network.STA_IF)
    try:
        wlan.active(True)
        if not wlan.isconnected():
            show_message(tft, [("Connecting WiFi", YELLOW), (ssid, CYAN)])
            wlan.connect(ssid, pwd)
            for _ in range(40):       # ~20s
                if wlan.isconnected():
                    break
                time.sleep(0.5)
    except Exception as e:
        print("STA connect error:", e)
        return None
    if wlan.isconnected():
        ip = wlan.ifconfig()[0]
        show_message(tft, [("WiFi OK", GREEN), (ip, CYAN)])
        sync_time()
        time.sleep(1.5)
        return ip
    return None


def start_ap(tft):
    wifi_reset()
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    try:
        ap.config(essid=AP_SSID, password=AP_PASSWORD, authmode=3)
    except Exception:
        ap.config(essid=AP_SSID, password=AP_PASSWORD)
    while not ap.active():
        time.sleep(0.2)
    show_setup_screen(tft)
    return ap.ifconfig()[0]      # 192.168.4.1


# ----------------------------------------------------------- data fetch
def _last_close(res):
    """Last non-null close in the 5m series = latest extended-hours price."""
    try:
        closes = res["indicators"]["quote"][0]["close"]
    except Exception:
        return None
    for v in reversed(closes):
        if v is not None:
            return v
    return None


def _spark_series(res, want=48):
    """Extract a compact intraday close series for the trend sparkline.
    Drops nulls and downsamples to at most `want` points (newest kept)."""
    try:
        closes = res["indicators"]["quote"][0]["close"]
    except Exception:
        return []
    vals = [v for v in closes if v is not None]
    n = len(vals)
    if n <= want:
        return vals
    step = n / float(want)
    out = []
    i = 0.0
    while int(i) < n and len(out) < want:
        out.append(vals[int(i)])
        i += step
    if out and out[-1] != vals[-1]:
        out[-1] = vals[-1]      # always keep the latest price
    return out


def fetch_yahoo(symbol):
    """Pull a live quote from Yahoo Finance (no API key needed), including
    pre-market / after-hours pricing.
    Returns (price, percent_change, change_amount, session, spark)."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s"
           "?interval=5m&range=1d&includePrePost=true" % symbol)
    r = None
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
        d = r.json()
        res = d["chart"]["result"][0]
        meta = res["meta"]
        reg = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose")
        if prev is None:
            prev = meta.get("previousClose")

        ctp = meta.get("currentTradingPeriod")
        session = classify_session(now_unix(), ctp)

        # During pre/post sessions, the freshest price is the last series close.
        price = reg
        if session in ("PRE", "POST"):
            ext = _last_close(res)
            if ext is not None:
                price = ext
        if price is None:
            price = reg
        if price is None:
            return None, None, None, session, []

        spark = _spark_series(res)

        pct = None
        chg = None
        if prev:
            chg = price - prev
            pct = chg / prev * 100.0
        return price, pct, chg, session, spark
    except Exception as e:
        print("fetch err", symbol, e)
        return None, None, None, "REGULAR", []
    finally:
        if r:
            try:
                r.close()
            except Exception:
                pass
        gc.collect()


def refresh_all():
    with LOCK:
        cfg = STATE["cfg"]
    session = "REGULAR"
    for sym in cfg["tickers"]:
        price, pct, chg, sess, spark = fetch_yahoo(sym)
        session = sess
        if price is not None:
            with LOCK:
                STATE["cache"][sym] = (price, pct, chg, sess, spark)
        gc.collect()
        time.sleep(0.3)
    with LOCK:
        STATE["session"] = session
    print("refreshed", session, {k: v[:4] for k, v in STATE["cache"].items()})
    return session


# ----------------------------------------------------------- input (tap / button)
def touch_init():
    """Detect the AXS5106L capacitive-touch controller (Touch board variant).
    Returns a SoftI2C handle if found, else None (non-touch board)."""
    try:
        rst = Pin(TP_RST, Pin.OUT)
        rst.value(0)
        time.sleep_ms(20)
        rst.value(1)
        time.sleep_ms(80)
        i2c = SoftI2C(scl=Pin(TP_SCL), sda=Pin(TP_SDA), freq=400000)
        if TOUCH_ADDR in i2c.scan():
            print("touch controller found (AXS5106L)")
            return i2c
    except Exception as e:
        print("touch init:", e)
    print("no touch controller -> using BOOT button for setup")
    return None


def touch_pressed(i2c):
    if i2c is None:
        return False
    try:
        d = i2c.readfrom_mem(TOUCH_ADDR, 0x01, 14)
        return d[1] > 0          # data[1] = number of active touch points
    except Exception:
        return False


# ----------------------------------------------------------- web config
PAGE = """HTTP/1.0 200 OK\r
Content-Type: text/html\r
\r
<!DOCTYPE html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<title>Stock Ticker Setup</title>
<style>body{font-family:sans-serif;background:#111;color:#eee;max-width:460px;margin:auto;padding:16px}
input{width:100%;padding:8px;margin:6px 0;box-sizing:border-box;background:#222;color:#eee;border:1px solid #444;border-radius:6px}
label{font-size:14px;color:#9cf}button{width:100%;padding:12px;margin-top:14px;background:#0a7;color:#fff;border:0;border-radius:6px;font-size:16px}
h2{color:#0cf}small{color:#888}</style></head><body>
<h2>Stock Ticker Setup</h2>
<form method=POST action=/save>
<label>WiFi name (SSID)</label><input name=ssid value="__SSID__">
<label>WiFi password</label><input name=pwd value="__PWD__" type=text>
<label>Tickers (comma separated)</label><input name=tickers value="__TICKERS__">
<label>Boot title</label><input name=title value="__TITLE__">
<label>Boot subtitle (optional)</label><input name=subtitle value="__SUBTITLE__">
<label>Splash seconds (0=skip)</label><input name=splash value="__SPLASH__">
<label>Rotate delay (sec per ticker)</label><input name=rotate value="__ROTATE__">
<label>Refresh interval (sec)</label><input name=refresh value="__REFRESH__">
<label>Closed-market refresh (sec)</label><input name=closed value="__CLOSED__">
<label>Screen orientation</label>
<select name=rotation style="width:100%;padding:8px;margin:6px 0;box-sizing:border-box;background:#222;color:#eee;border:1px solid #444;border-radius:6px">
<option value=0 __ROT0__>Portrait</option>
<option value=1 __ROT1__>Landscape</option>
<option value=2 __ROT2__>Portrait (flipped)</option>
<option value=3 __ROT3__>Landscape (flipped)</option>
</select>
<label>Brightness (0-100)</label><input name=bright value="__BRIGHT__">
<label>Dim after (sec, 0=never)</label><input name=dimsec value="__DIMSEC__">
<label>Dim brightness (0-100)</label><input name=dimbright value="__DIMBRIGHT__">
<button type=submit>Save &amp; Reboot</button>
</form><small>Saved to flash. Board reboots to apply.</small></body></html>
"""

DONE = """HTTP/1.0 200 OK\r
Content-Type: text/html\r
\r
<html><body style="font-family:sans-serif;background:#111;color:#0f0;text-align:center;padding-top:60px">
<h2>Saved. Rebooting...</h2></body></html>"""

# 302 redirect used to trigger the OS "Sign in to network" captive-portal popup.
REDIRECT = ("HTTP/1.0 302 Found\r\n"
            "Location: http://192.168.4.1/\r\n"
            "Content-Length: 0\r\n\r\n")


def _url_decode(s):
    s = s.replace("+", " ")
    out = ""
    i = 0
    while i < len(s):
        if s[i] == "%" and i + 2 < len(s):
            try:
                out += chr(int(s[i + 1:i + 3], 16))
                i += 3
                continue
            except Exception:
                pass
        out += s[i]
        i += 1
    return out


def _parse_post(body):
    d = {}
    for pair in body.split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            d[k] = _url_decode(v)
    return d


def _esc_attr(s):
    """Escape a string for safe use inside an HTML double-quoted attribute."""
    return (str(s).replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def render_page():
    with LOCK:
        cfg = STATE["cfg"]
    html = PAGE
    rot = int(cfg.get("rotation", 0))
    repl = {
        "__SSID__": cfg["wifi"].get("ssid", ""),
        "__PWD__": cfg["wifi"].get("password", ""),
        "__TICKERS__": ",".join(cfg.get("tickers", [])),
        "__TITLE__": _esc_attr(cfg.get("title", "STOCK TICKER")),
        "__SUBTITLE__": _esc_attr(cfg.get("subtitle", "")),
        "__SPLASH__": str(cfg.get("splash_secs", 3)),
        "__ROTATE__": str(cfg.get("rotate_delay", 5)),
        "__REFRESH__": str(cfg.get("refresh_interval", 60)),
        "__CLOSED__": str(cfg.get("closed_interval", 600)),
        "__BRIGHT__": str(cfg.get("brightness", 100)),
        "__DIMSEC__": str(cfg.get("dim_timeout", 300)),
        "__DIMBRIGHT__": str(cfg.get("dim_brightness", 20)),
        "__ROT0__": "selected" if rot == 0 else "",
        "__ROT1__": "selected" if rot == 1 else "",
        "__ROT2__": "selected" if rot == 2 else "",
        "__ROT3__": "selected" if rot == 3 else "",
    }
    for k, v in repl.items():
        html = html.replace(k, v)
    return html


def apply_form(form):
    with LOCK:
        cfg = dict(STATE["cfg"])
    cfg["wifi"] = {"ssid": form.get("ssid", "").strip(),
                   "password": form.get("pwd", "").strip()}
    tk = [t.strip().upper() for t in form.get("tickers", "").split(",") if t.strip()]
    if tk:
        cfg["tickers"] = tk
    cfg["title"] = form.get("title", "").strip()[:40] or "STOCK TICKER"
    cfg["subtitle"] = form.get("subtitle", "").strip()[:40]
    try:
        cfg["splash_secs"] = min(30, max(0, int(form.get("splash", 3))))
    except Exception:
        pass
    try:
        cfg["rotate_delay"] = max(1, int(form.get("rotate", 5)))
    except Exception:
        pass
    try:
        cfg["refresh_interval"] = max(15, int(form.get("refresh", 60)))
    except Exception:
        pass
    try:
        cfg["closed_interval"] = max(60, int(form.get("closed", 600)))
    except Exception:
        pass
    try:
        cfg["rotation"] = min(3, max(0, int(form.get("rotation", 0))))
    except Exception:
        pass
    try:
        cfg["brightness"] = min(100, max(5, int(form.get("bright", 100))))
    except Exception:
        pass
    try:
        cfg["dim_timeout"] = max(0, int(form.get("dimsec", 300)))
    except Exception:
        pass
    try:
        cfg["dim_brightness"] = min(100, max(0, int(form.get("dimbright", 20))))
    except Exception:
        pass
    save_config(cfg)
    with LOCK:
        STATE["cfg"] = cfg


def web_server(captive=False):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", 80))
    s.listen(4)
    print("web server on :80", "(captive)" if captive else "")
    while True:
        try:
            cl, addr = s.accept()
            cl.settimeout(3)
            req = b""
            try:
                while b"\r\n\r\n" not in req:
                    chunk = cl.recv(512)
                    if not chunk:
                        break
                    req += chunk
                # request line: "METHOD /path HTTP/1.1"
                first = req.split(b"\r\n", 1)[0].decode("utf-8", "replace")
                parts = first.split(" ")
                method = parts[0] if parts else "GET"
                path = parts[1] if len(parts) > 1 else "/"

                if method == "POST" and path.startswith("/save"):
                    head, _, rest = req.partition(b"\r\n\r\n")
                    clen = 0
                    for line in head.split(b"\r\n"):
                        if line.lower().startswith(b"content-length:"):
                            clen = int(line.split(b":")[1])
                    while len(rest) < clen:
                        rest += cl.recv(512)
                    form = _parse_post(rest.decode())
                    apply_form(form)
                    cl.send(DONE)
                    cl.close()
                    time.sleep(1)
                    with LOCK:
                        STATE["reboot"] = True
                    reset()
                elif path == "/" or path.startswith("/?"):
                    cl.send(render_page())
                elif captive:
                    # Any other path while in setup mode = an OS captive-portal
                    # probe (captive.apple.com, /generate_204, etc.) -> redirect
                    # so the phone auto-opens the config page.
                    cl.send(REDIRECT)
                else:
                    cl.send(render_page())
            finally:
                cl.close()
        except Exception as e:
            print("web err", e)


# ----------------------------------------------------------- captive DNS
def dns_server():
    """Tiny DNS responder: answer every A query with 192.168.4.1."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", 53))
    except Exception as e:
        print("dns bind err", e)
        return
    print("dns server on :53")
    ip = bytes([192, 168, 4, 1])
    while True:
        try:
            data, addr = s.recvfrom(512)
            if len(data) < 12:
                continue
            # Build a response: copy ID, set response flags, 1 question, 1 answer.
            resp = bytearray(data[:2])          # transaction ID
            resp += b"\x81\x80"                 # flags: standard response, no error
            resp += data[4:6]                   # QDCOUNT (echo)
            resp += b"\x00\x01"                 # ANCOUNT = 1
            resp += b"\x00\x00\x00\x00"         # NS=0, AR=0
            resp += data[12:]                   # original question section
            # Answer: name pointer to question, type A, class IN, TTL, RDLENGTH, RDATA
            resp += b"\xc0\x0c"                 # pointer to offset 12 (the qname)
            resp += b"\x00\x01\x00\x01"         # type A, class IN
            resp += b"\x00\x00\x00\x1e"         # TTL 30s
            resp += b"\x00\x04"                 # RDLENGTH = 4
            resp += ip
            s.sendto(resp, addr)
        except Exception as e:
            print("dns err", e)


# ----------------------------------------------------------- fetch thread
def fetch_loop():
    while True:
        with LOCK:
            cfg = STATE["cfg"]
            mode = STATE["mode"]
        session = STATE.get("session", "REGULAR")
        if mode == "sta":
            session = refresh_all() or session
        # Slow the polling right down once the US market is closed.
        if session == "CLOSED":
            interval = cfg.get("closed_interval", 600)
        else:
            interval = cfg.get("refresh_interval", 60)
        slept = 0
        while slept < interval:
            time.sleep(1)
            slept += 1


# ----------------------------------------------------------- main
def main():
    gc.collect()
    cfg = load_config()
    with LOCK:
        STATE["cfg"] = cfg
    tft = init_display(cfg)

    # configurable boot title screen
    show_splash(tft, cfg.get("title", "STOCK TICKER"), cfg.get("subtitle", ""))
    splash_secs = cfg.get("splash_secs", 3)
    try:
        splash_secs = max(0, int(splash_secs))
    except Exception:
        splash_secs = 3
    if splash_secs:
        time.sleep(splash_secs)

    touch = touch_init()      # SoftI2C handle or None (non-touch board)

    ip = start_sta(cfg, tft)
    if ip:
        with LOCK:
            STATE["ip"] = ip
            STATE["mode"] = "sta"
        refresh_all()
        _thread.start_new_thread(fetch_loop, ())
        _thread.start_new_thread(web_server, (False,))
        run_ticker_loop(tft, touch)
    else:
        # no/failed WiFi -> hotspot + captive portal for first-time setup
        ap_ip = start_ap(tft)
        with LOCK:
            STATE["ip"] = ap_ip
            STATE["mode"] = "ap"
        _thread.start_new_thread(dns_server, ())   # redirect all DNS -> 192.168.4.1
        web_server(captive=True)      # blocks; reboots on save


def run_ticker_loop(tft, touch=None):
    """Rotate tickers, with a tap-driven state machine:
        * idle for dim_timeout seconds      -> dim to dim_brightness
        * tap while dimmed                  -> wake to full brightness
        * tap while awake                   -> show CONFIG screen (with IP)
        * tap while on CONFIG screen        -> back to rotating tickers
    The IP is never shown during rotation, only on the CONFIG screen.
    "tap" = capacitive touch (Touch board) or the BOOT button (any board)."""
    btn = Pin(BOOT_BTN, Pin.IN, Pin.PULL_UP)
    idx = 0
    last_active = time.time()
    dimmed = False
    in_config = False
    prev_pressed = False
    next_draw = 0.0

    with LOCK:
        full_bri = STATE["cfg"].get("brightness", 100)
    set_brightness(full_bri)

    while True:
        with LOCK:
            cfg = STATE["cfg"]
            cache = dict(STATE["cache"])
            ip = STATE["ip"]
        tickers = cfg["tickers"]
        rotate_delay = cfg.get("rotate_delay", 5)
        full_bri = cfg.get("brightness", 100)
        dim_timeout = cfg.get("dim_timeout", 300)
        dim_bri = cfg.get("dim_brightness", 20)
        ssid = cfg["wifi"].get("ssid", "")
        now = time.time()

        # rising-edge detection so one physical tap = one event
        pressed = touch_pressed(touch) or btn.value() == 0
        tap = pressed and not prev_pressed
        prev_pressed = pressed

        if tap:
            if in_config:
                in_config = False        # leave config -> resume tickers
                dimmed = False
                last_active = now
                set_brightness(full_bri)
                next_draw = 0.0          # force immediate redraw
            elif dimmed:
                dimmed = False           # first tap just wakes the screen
                last_active = now
                set_brightness(full_bri)
            else:
                in_config = True         # awake tap -> open config page
                set_brightness(full_bri)
                show_config_screen(tft, ip, ssid)
            time.sleep(0.25)             # debounce
            continue

        if in_config:
            time.sleep(0.05)
            continue

        # auto-dim after inactivity
        if not dimmed and dim_timeout > 0 and (now - last_active) >= dim_timeout:
            dimmed = True
            set_brightness(dim_bri)

        # rotate tickers on schedule
        if now >= next_draw:
            n = len(tickers)
            sym = tickers[idx % n]
            item = cache.get(sym)
            sess = item[3] if (item and len(item) > 3) else STATE.get("session", "REGULAR")
            draw_ticker(tft, sym, item, idx % n, n, sess)
            idx += 1
            next_draw = now + rotate_delay
            gc.collect()

        time.sleep(0.05)


if __name__ == "__main__":
    main()
