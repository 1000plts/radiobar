#!/usr/bin/env python3
"""
RadioBar — a minimal macOS menubar internet radio player.
Requires: rumps, pyobjc (AppKit + AVFoundation). Audio plays via AVPlayer —
no external apps needed.
"""

import fcntl
import json
import os
import socket
import ssl
import threading
import time
import urllib.request
from urllib.parse import urlparse, quote
import rumps
import objc
import AppKit
import AVFoundation

# In a bundled .app the system CA certs aren't on the default search path, so
# every HTTPS request (station search, NTS metadata) fails cert verification.
# Point OpenSSL at certifi's bundle so urllib/ssl can verify certificates.
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("SSL_CERT_DIR", os.path.dirname(certifi.where()))
except Exception:
    certifi = None


# Python 3.14's stack-overflow guard trips the recursive json decoder on the
# small C stack PyInstaller gives worker threads ("Stack overflow (used 17 kB)").
# Give every new thread a generous stack so json.load of API responses is safe.
try:
    threading.stack_size(16 * 1024 * 1024)
except (ValueError, RuntimeError):
    pass


def https_context():
    """SSL context that verifies against certifi's CA bundle when available.

    A bundled .app has no system CA certs on the default path, so the default
    context can't verify anything; certifi supplies a known-good bundle.
    """
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()

CONFIG_PATH = os.path.expanduser("~/.radio_bar_config.json")

DEFAULT_STATIONS = [
    {"name": "NTS 1",          "url": "https://stream-relay-geo.ntslive.net/stream"},
    {"name": "NTS 2",          "url": "https://stream-relay-geo.ntslive.net/stream2"},
    {"name": "NTS Expansions", "url": "https://stream-mixtape-geo.ntslive.net/mixtape3"},
    {"name": "NTS Island time","url": "https://stream-mixtape-geo.ntslive.net/mixtape21"},
    {"name": "NOODS",          "url": "https://noods-radio.radiocult.fm/stream"},
    {"name": "Cashmere",       "url": "https://cashmereradio.out.airtime.pro:8000/cashmereradio_b"},
    {"name": "TOK FM",         "url": "https://radiostream.pl/tuba10-1.mp3"},
    {"name": "PR 1",           "url": "http://mp3.polskieradio.pl:8904/;"},
    {"name": "PR 2",           "url": "http://mp3.polskieradio.pl:8952/;"},
]


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {"stations": DEFAULT_STATIONS}


def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


MAX_STATIONS = 20
STATION_ROW_TYPE = "com.radiobar.station-row"  # pasteboard type for drag-reorder

MARQUEE_WIDTH = 24      # visible characters in the menubar title
MARQUEE_STEP_SECS = 0.4
MARQUEE_GAP = "   "     # spacing between end and wrapped-around start


def marquee_window(text, offset, width=MARQUEE_WIDTH):
    """Fixed-width sliding window over text; returns text as-is if it fits."""
    if len(text) <= width:
        return text
    looped = text + MARQUEE_GAP
    doubled = looped + looped
    start = offset % len(looped)
    return doubled[start:start + width]


NTS_LIVE_API = "https://www.nts.live/api/v2/live"
NTS_CHANNELS = {
    "https://stream-relay-geo.ntslive.net/stream": "1",
    "https://stream-relay-geo.ntslive.net/stream2": "2",
}


def nts_channel_for(url):
    """Return the NTS live channel ('1'/'2') for a stream URL, else None."""
    return NTS_CHANNELS.get(url.rstrip("/"))


def fetch_icy_title(url, timeout=6):
    """Read the current ICY StreamTitle from an internet radio stream.

    Speaks both HTTP and the legacy 'ICY 200 OK' Shoutcast dialect (which
    urllib rejects). Downloads one metadata interval (~16 KB) then closes.
    Returns the title string or None.
    """
    try:
        parts = urlparse(url)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            if parts.scheme == "https":
                sock = https_context().wrap_socket(sock, server_hostname=host)
            request = (
                f"GET {path} HTTP/1.0\r\nHost: {host}\r\n"
                "Icy-MetaData: 1\r\nUser-Agent: RadioBar\r\n\r\n"
            )
            sock.sendall(request.encode())
            stream = sock.makefile("rb")
            if b"200" not in stream.readline():
                return None
            metaint = None
            while True:
                line = stream.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                if line.lower().startswith(b"icy-metaint:"):
                    metaint = int(line.split(b":", 1)[1].strip())
            if not metaint or metaint > 512 * 1024:
                return None
            stream.read(metaint)               # skip one interval of audio
            length = stream.read(1)[0] * 16    # metadata block length
            if length == 0:
                return None
            block = stream.read(length).rstrip(b"\x00").decode("utf-8", "replace")
            for part in block.split(";"):
                if part.startswith("StreamTitle='"):
                    return part[len("StreamTitle='"):].rstrip("'") or None
            return None
        finally:
            sock.close()
    except Exception:
        return None


def fetch_nts_now(channel):
    """Fetch the current broadcast title for an NTS channel. Returns str or None."""
    try:
        with urllib.request.urlopen(NTS_LIVE_API, timeout=5, context=https_context()) as resp:
            data = json.load(resp)
        for ch in data.get("results", []):
            if str(ch.get("channel_name")) == channel:
                title = (ch.get("now") or {}).get("broadcast_title")
                return title or None
    except Exception:
        pass
    return None


RADIO_BROWSER_MIRRORS = [
    "https://de1.api.radio-browser.info",
    "https://de2.api.radio-browser.info",
]


def search_radio_browser(term, limit=15):
    """Search the open Radio Browser directory (community station index).

    Returns a list of {"name", "url", "detail"} dicts sorted by votes,
    or None if every mirror failed. Tries each mirror twice — the servers
    occasionally return transient 503s.
    """
    query = quote(term)
    for base in RADIO_BROWSER_MIRRORS * 2:
        try:
            request = urllib.request.Request(
                f"{base}/json/stations/search?name={query}&limit={limit}"
                "&order=votes&reverse=true&hidebroken=true",
                headers={"User-Agent": "RadioBar/1.0 (github.com/1000plts/radiobar)"},
            )
            with urllib.request.urlopen(request, timeout=6, context=https_context()) as resp:
                data = json.load(resp)
            results = []
            for s in data:
                url = (s.get("url_resolved") or s.get("url") or "").strip()
                name = (s.get("name") or "").strip()
                if not url or not name:
                    continue
                bitrate = s.get("bitrate") or 0
                detail = " · ".join(x for x in (
                    s.get("countrycode") or s.get("country") or "",
                    s.get("codec") or "",
                    f"{bitrate} kbps" if bitrate else "",
                ) if x)
                results.append({"name": name, "url": url, "detail": detail})
            return results
        except Exception:
            continue
    return None


class StationsPanelController(AppKit.NSObject):
    """Native 'Manage Stations' panel: station list, − to remove, fields + Add."""

    def initWithApp_(self, app):
        self = objc.super(StationsPanelController, self).init()
        if self is None:
            return None
        self.app = app
        self.panel = None
        self.results = []
        self.results_table = None  # created in _build_panel; datasource callbacks
        return self                # can fire before it exists

    def show(self):
        if self.panel is None:
            self._build_panel()
        self._reload()
        self.panel.center()
        self.panel.makeKeyAndOrderFront_(None)
        AppKit.NSApp.activateIgnoringOtherApps_(True)

    # ── UI construction ────────────────────────────────────────────────────

    def _build_panel(self):
        style = AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
        self.panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0, 0, 460, 560), style, AppKit.NSBackingStoreBuffered, False
        )
        self.panel.setTitle_("Manage Stations")
        self.panel.setFloatingPanel_(True)
        self.panel.setReleasedWhenClosed_(False)
        content = self.panel.contentView()

        self.table = AppKit.NSTableView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 420, 204))
        for identifier, title, width in (("name", "Name", 120), ("url", "URL", 280)):
            col = AppKit.NSTableColumn.alloc().initWithIdentifier_(identifier)
            col.setTitle_(title)
            col.setWidth_(width)
            col.setEditable_(False)
            self.table.addTableColumn_(col)
        self.table.setDataSource_(self)
        self.table.setAllowsMultipleSelection_(False)
        self.table.registerForDraggedTypes_([STATION_ROW_TYPE])
        self.table.setDraggingSourceOperationMask_forLocal_(AppKit.NSDragOperationMove, True)

        scroll = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(20, 336, 420, 204))
        scroll.setDocumentView_(self.table)
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(AppKit.NSBezelBorder)
        content.addSubview_(scroll)

        minus = AppKit.NSButton.buttonWithTitle_target_action_("−", self, "removeClicked:")
        minus.setFrame_(AppKit.NSMakeRect(20, 304, 32, 24))
        content.addSubview_(minus)

        self.count_label = AppKit.NSTextField.labelWithString_("")
        self.count_label.setFrame_(AppKit.NSMakeRect(300, 308, 140, 17))
        self.count_label.setAlignment_(AppKit.NSTextAlignmentRight)
        self.count_label.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        content.addSubview_(self.count_label)

        # ── Radio Browser directory search ─────────────────────────────────
        directory_label = AppKit.NSTextField.labelWithString_("Add from directory:")
        directory_label.setFrame_(AppKit.NSMakeRect(20, 274, 200, 17))
        content.addSubview_(directory_label)

        self.search_field = AppKit.NSTextField.alloc().initWithFrame_(
            AppKit.NSMakeRect(20, 240, 328, 24)
        )
        self.search_field.setPlaceholderString_("Search 50,000+ stations (Radio Browser)…")
        self.search_field.setTarget_(self)
        self.search_field.setAction_("searchClicked:")  # Return key searches
        content.addSubview_(self.search_field)

        search_button = AppKit.NSButton.buttonWithTitle_target_action_(
            "Search", self, "searchClicked:"
        )
        search_button.setFrame_(AppKit.NSMakeRect(354, 236, 86, 32))
        content.addSubview_(search_button)

        self.results_table = AppKit.NSTableView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, 420, 118)
        )
        for identifier, title, width in (("rname", "Station", 190), ("rdetail", "Details", 210)):
            col = AppKit.NSTableColumn.alloc().initWithIdentifier_(identifier)
            col.setTitle_(title)
            col.setWidth_(width)
            col.setEditable_(False)
            self.results_table.addTableColumn_(col)
        self.results_table.setDataSource_(self)
        self.results_table.setAllowsMultipleSelection_(False)
        self.results_table.setTarget_(self)
        self.results_table.setDoubleAction_("addSelectedResult:")
        results_scroll = AppKit.NSScrollView.alloc().initWithFrame_(
            AppKit.NSMakeRect(20, 112, 420, 118)
        )
        results_scroll.setDocumentView_(self.results_table)
        results_scroll.setHasVerticalScroller_(True)
        results_scroll.setBorderType_(AppKit.NSBezelBorder)
        content.addSubview_(results_scroll)

        add_selected = AppKit.NSButton.buttonWithTitle_target_action_(
            "+ Add Selected", self, "addSelectedResult:"
        )
        add_selected.setFrame_(AppKit.NSMakeRect(20, 74, 130, 28))
        content.addSubview_(add_selected)

        self.search_status = AppKit.NSTextField.labelWithString_("")
        self.search_status.setFrame_(AppKit.NSMakeRect(240, 80, 200, 17))
        self.search_status.setAlignment_(AppKit.NSTextAlignmentRight)
        self.search_status.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        content.addSubview_(self.search_status)

        self.name_field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(20, 40, 140, 24))
        self.name_field.setPlaceholderString_("Name")
        self.name_field.setDelegate_(self)
        content.addSubview_(self.name_field)

        self.url_field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(168, 40, 200, 24))
        self.url_field.setPlaceholderString_("Stream URL")
        self.url_field.setDelegate_(self)
        self.url_field.setTarget_(self)
        self.url_field.setAction_("addClicked:")  # Return key adds
        content.addSubview_(self.url_field)

        self.add_button = AppKit.NSButton.buttonWithTitle_target_action_("Add", self, "addClicked:")
        self.add_button.setFrame_(AppKit.NSMakeRect(376, 36, 64, 32))
        content.addSubview_(self.add_button)

    # ── State ──────────────────────────────────────────────────────────────

    def _stations(self):
        return self.app.config.get("stations", [])

    def _reload(self):
        self.table.reloadData()
        self.count_label.setStringValue_(f"{len(self._stations())} / {MAX_STATIONS} stations")
        self._update_add_enabled()

    def _update_add_enabled(self):
        name = self.name_field.stringValue().strip()
        url = self.url_field.stringValue().strip()
        ok = bool(name) and bool(url) and len(self._stations()) < MAX_STATIONS
        self.add_button.setEnabled_(ok)

    # ── Actions ────────────────────────────────────────────────────────────

    def addClicked_(self, sender):
        name = self.name_field.stringValue().strip()
        url = self.url_field.stringValue().strip()
        if not name or not url or len(self._stations()) >= MAX_STATIONS:
            AppKit.NSBeep()
            return
        if not url.lower().startswith(("http://", "https://")):
            AppKit.NSBeep()
            self.url_field.setStringValue_(url)
            return
        stations = self._stations()
        stations.append({"name": name, "url": url})
        self.app.config["stations"] = stations
        save_config(self.app.config)
        self.app._build_menu()
        self.name_field.setStringValue_("")
        self.url_field.setStringValue_("")
        self._reload()
        self.panel.makeFirstResponder_(self.name_field)

    def removeClicked_(self, sender):
        row = self.table.selectedRow()
        if row < 0:
            AppKit.NSBeep()
            return
        stations = self._stations()
        removed = stations.pop(row)
        if self.app.current_station is removed:
            self.app.stop(None)
        self.app.config["stations"] = stations
        save_config(self.app.config)
        self.app._build_menu()
        self._reload()

    # ── NSTableView data source ────────────────────────────────────────────

    def numberOfRowsInTableView_(self, table):
        if table is self.results_table:
            return len(self.results)
        return len(self._stations())

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        if table is self.results_table:
            result = self.results[row]
            return result["name"] if column.identifier() == "rname" else result["detail"]
        station = self._stations()[row]
        return station["name"] if column.identifier() == "name" else station["url"]

    # ── Directory search ───────────────────────────────────────────────────

    def searchClicked_(self, sender):
        term = self.search_field.stringValue().strip()
        if not term:
            return
        self.search_status.setStringValue_("Searching…")
        threading.Thread(target=self._run_search, args=(term,), daemon=True).start()

    def _run_search(self, term):
        with objc.autorelease_pool():
            results = search_radio_browser(term)
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(
                lambda: self._apply_results(results)
            )

    def _apply_results(self, results):
        if results is None:
            self.results = []
            self.search_status.setStringValue_("Search failed — try again")
        else:
            self.results = results
            self.search_status.setStringValue_(
                f"{len(results)} found" if results else "No results"
            )
        self.results_table.reloadData()

    def addSelectedResult_(self, sender):
        row = self.results_table.selectedRow()
        if row < 0:
            row = self.results_table.clickedRow()
        if row < 0 or row >= len(self.results):
            AppKit.NSBeep()
            return
        if len(self._stations()) >= MAX_STATIONS:
            AppKit.NSBeep()
            self.search_status.setStringValue_(f"Max {MAX_STATIONS} stations — remove one first")
            return
        result = self.results[row]
        stations = self._stations()
        stations.append({"name": result["name"], "url": result["url"]})
        self.app.config["stations"] = stations
        save_config(self.app.config)
        self.app._build_menu()
        self._reload()
        self.search_status.setStringValue_(f"Added: {result['name']}")

    # ── Drag-and-drop reordering ───────────────────────────────────────────

    def tableView_pasteboardWriterForRow_(self, table, row):
        if table is not self.table:
            return None  # search results are not draggable
        item = AppKit.NSPasteboardItem.alloc().init()
        item.setString_forType_(str(row), STATION_ROW_TYPE)
        return item

    def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table, info, row, operation):
        if operation == AppKit.NSTableViewDropOn:
            table.setDropRow_dropOperation_(row, AppKit.NSTableViewDropAbove)
        return AppKit.NSDragOperationMove

    def tableView_acceptDrop_row_dropOperation_(self, table, info, row, operation):
        value = info.draggingPasteboard().stringForType_(STATION_ROW_TYPE)
        if value is None:
            return False
        source = int(value)
        stations = self._stations()
        if not (0 <= source < len(stations)):
            return False
        moved = stations.pop(source)
        dest = row - 1 if source < row else row
        stations.insert(dest, moved)
        self.app.config["stations"] = stations
        save_config(self.app.config)
        self.app._build_menu()
        self._reload()
        return True

    # ── NSTextField delegate ───────────────────────────────────────────────

    def controlTextDidChange_(self, notification):
        self._update_add_enabled()


class StatusClickHandler(AppKit.NSObject):
    """Routes menubar clicks: left toggles play/pause, right opens the menu."""

    def initWithApp_(self, app):
        self = objc.super(StatusClickHandler, self).init()
        if self is None:
            return None
        self.app = app
        return self

    def statusClicked_(self, sender):
        event = AppKit.NSApp.currentEvent()
        right_click = event is not None and (
            event.type() == AppKit.NSEventTypeRightMouseUp
            or (event.modifierFlags() & AppKit.NSEventModifierFlagControl)
        )
        if right_click or not self.app.current_station:
            self.app.pop_menu()
        else:
            self.app.toggle_pause(None)


class RadioBarApp(rumps.App):
    def __init__(self):
        super().__init__("RadioBar", quit_button=None)
        AppKit.NSApplication.sharedApplication().setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        self.stations_panel = None
        self.config = load_config()
        self.player = None
        self.current_station = None
        self.paused = False
        self.meta_stop = None
        self._title_attrs = None
        self._nts_cache = {"time": 0.0, "label": None}
        self._icy_cache = {"time": 0.0, "label": None}
        self.marquee_prefix = ""        # pinned, always shown (play/pause icon)
        self.marquee_body = "RadioBar"  # scrolls after the prefix if too long
        self.marquee_offset = 0
        self.marquee_timer = rumps.Timer(self._tick_marquee, MARQUEE_STEP_SECS)
        self._fixed_length = None  # pixel width of the playing-state status item
        self._now_label = None
        self._click_handler = None
        self._rewire_timer = rumps.Timer(self._rewire_status_click, 0.5)
        self._rewire_timer.start()
        self._build_menu()

    # ── Menubar click handling ─────────────────────────────────────────────

    def _rewire_status_click(self, timer):
        """One-shot after launch: detach the menu so clicks reach us directly."""
        timer.stop()
        try:
            statusitem = self._nsapp.nsstatusitem
            statusitem.setMenu_(None)
            button = statusitem.button()
            self._click_handler = StatusClickHandler.alloc().initWithApp_(self)
            button.setTarget_(self._click_handler)
            button.setAction_("statusClicked:")
            button.sendActionOn_(
                AppKit.NSEventMaskLeftMouseUp | AppKit.NSEventMaskRightMouseUp
            )
        except Exception:
            pass  # fall back to default click-opens-menu behavior

    def pop_menu(self):
        """Show the dropdown menu programmatically (used for right-click)."""
        statusitem = self._nsapp.nsstatusitem
        statusitem.setMenu_(self.menu._menu)
        statusitem.button().performClick_(None)
        statusitem.setMenu_(None)

    def _update_marquee_from_state(self):
        if not self.current_station:
            prefix, body = "", "RadioBar"
        else:
            # ︎ forces text (not emoji) glyphs so both icons render same-width.
            # The icon is a pinned prefix so it stays visible while body scrolls.
            icon = "⏸︎" if self.paused else "▶︎"
            prefix = f"{icon} "
            body = self.current_station["name"]
            if self._now_label:
                body += f" · {self._now_label}"
        # May be called from the metadata poll thread; timer + AppKit need main.
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(
            lambda: self._set_marquee(prefix, body)
        )

    # ── Menubar title marquee ──────────────────────────────────────────────

    def _set_marquee(self, prefix, body):
        """Set the menubar title. The prefix (play/pause icon) is always shown;
        the body scrolls only when it's wider than the remaining width, else the
        title sits at its natural width (no padding, no empty space)."""
        if (prefix, body) != (self.marquee_prefix, self.marquee_body):
            self.marquee_prefix = prefix
            self.marquee_body = body
            self.marquee_offset = 0
        if len(body) > self._body_width():
            if not self.marquee_timer.is_alive():
                self.marquee_timer.start()
        else:
            if self.marquee_timer.is_alive():
                self.marquee_timer.stop()
        self._render_title()

    def _body_width(self):
        """Characters available for the scrolling body after the pinned prefix."""
        return max(6, MARQUEE_WIDTH - len(self.marquee_prefix))

    def _tick_marquee(self, _timer):
        self.marquee_offset += 1
        self._render_title()

    def _render_title(self):
        scrolling = len(self.marquee_body) > self._body_width()
        if scrolling:
            body = marquee_window(self.marquee_body, self.marquee_offset, self._body_width())
        else:
            body = self.marquee_body
        text = self.marquee_prefix + body
        try:
            if self._title_attrs is None:
                # Monospaced font keeps the sliding window a truly fixed width.
                font = AppKit.NSFont.monospacedSystemFontOfSize_weight_(
                    13, AppKit.NSFontWeightRegular
                )
                self._title_attrs = {AppKit.NSFontAttributeName: font}
            attributed = AppKit.NSAttributedString.alloc().initWithString_attributes_(
                text, self._title_attrs
            )
            statusitem = self._nsapp.nsstatusitem
            button = statusitem.button()
            button.setAlignment_(AppKit.NSTextAlignmentLeft)
            button.setAttributedTitle_(attributed)
            if scrolling:
                # Pin the item to the window width so scrolling text doesn't
                # resize the item or nudge neighbouring menu bar icons.
                if self._fixed_length is None:
                    sample = AppKit.NSAttributedString.alloc().initWithString_attributes_(
                        "M" * MARQUEE_WIDTH, self._title_attrs
                    )
                    self._fixed_length = sample.size().width + 10
                statusitem.setLength_(self._fixed_length)
            else:
                # Short titles: fit content exactly — no trailing empty space.
                statusitem.setLength_(AppKit.NSVariableStatusItemLength)
        except Exception:
            self.title = text  # fallback: plain proportional title

    # ── Menu construction ──────────────────────────────────────────────────

    def _build_menu(self):
        self.menu.clear()
        stations = self.config.get("stations", [])[:MAX_STATIONS]

        self.now_playing_item = rumps.MenuItem("—", callback=None)
        self.menu.add(self.now_playing_item)
        self.menu.add(rumps.MenuItem("⏸  Pause / Resume", callback=self.toggle_pause))
        self.menu.add(rumps.MenuItem("⏹  Stop", callback=self.stop))
        self.menu.add(rumps.separator)

        for station in stations:  # config order == menu order (reorder in panel)
            item = rumps.MenuItem(
                station["name"],
                callback=self._make_play_cb(station)
            )
            self.menu.add(item)
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("⚙️  Configure stations…", callback=self.open_config))
        self.menu.add(rumps.MenuItem("Quit RadioBar", callback=rumps.quit_application))

    def _make_play_cb(self, station):
        def cb(_):
            self.play(station)
        return cb

    # ── Playback ───────────────────────────────────────────────────────────

    def play(self, station):
        self.stop(None)
        self.current_station = station
        self.paused = False
        self._nts_cache = {"time": 0.0, "label": None}
        self._icy_cache = {"time": 0.0, "label": None}
        self._now_label = None
        self._update_marquee_from_state()
        self.now_playing_item.title = "Connecting…"

        url = AppKit.NSURL.URLWithString_(station["url"])
        self.player = AVFoundation.AVPlayer.playerWithURL_(url)
        self.player.play()

        self._start_meta_polling()

    def toggle_pause(self, _):
        if not self.player or not self.current_station:
            return
        if self.paused:
            self.player.play()
            self.paused = False
        else:
            self.player.pause()
            self.paused = True
        self._update_marquee_from_state()

    def stop(self, _):
        self._stop_meta_polling()
        if self.player:
            self.player.pause()
            self.player.replaceCurrentItemWithPlayerItem_(None)
            self.player = None
        self.current_station = None
        self.paused = False
        self._now_label = None
        self._update_marquee_from_state()
        self.now_playing_item.title = "—"

    # ── Metadata polling ───────────────────────────────────────────────────

    def _start_meta_polling(self):
        self._stop_meta_polling()
        self.meta_stop = threading.Event()
        thread = threading.Thread(
            target=self._meta_loop, args=(self.meta_stop,), daemon=True
        )
        thread.start()

    def _stop_meta_polling(self):
        if self.meta_stop:
            self.meta_stop.set()
            self.meta_stop = None

    def _meta_loop(self, stop):
        # One long-lived thread; each cycle gets its own autorelease pool so
        # ObjC objects created off the main thread are actually freed.
        while True:
            with objc.autorelease_pool():
                self._poll_meta_once()
            if stop.wait(10.0):
                return

    def _poll_meta_once(self):
        if not self.player:
            return
        label = self._nts_label()
        if label is None:
            label = self._icy_label()
        # Touch AppKit (menu item + status title) only on the main thread —
        # mutating a menu item's title off-thread crashes an open menu.
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(
            lambda: self._apply_now_playing(label)
        )

    def _apply_now_playing(self, label):
        if not self.player:
            return
        self._now_label = label
        self.now_playing_item.title = f"♫  {label}" if label else "♫  Playing"
        self._update_marquee_from_state()

    def _nts_label(self):
        """Current NTS show title if playing an NTS live channel, else None."""
        if not self.current_station:
            return None
        channel = nts_channel_for(self.current_station["url"])
        if not channel:
            return None
        if time.monotonic() - self._nts_cache["time"] > 60:
            self._nts_cache["label"] = fetch_nts_now(channel)
            self._nts_cache["time"] = time.monotonic()
        return self._nts_cache["label"]

    def _icy_label(self):
        """Track info from the stream's ICY metadata (cached 30s), or None."""
        if not self.current_station:
            return None
        if time.monotonic() - self._icy_cache["time"] > 30:
            self._icy_cache["label"] = fetch_icy_title(self.current_station["url"])
            self._icy_cache["time"] = time.monotonic()
        return self._icy_cache["label"]

    # ── Config panel ───────────────────────────────────────────────────────

    def open_config(self, _):
        if self.stations_panel is None:
            self.stations_panel = StationsPanelController.alloc().initWithApp_(self)
        self.stations_panel.show()


_instance_lock = None


def acquire_single_instance():
    """Hold an exclusive lock so only one RadioBar runs, however it's launched.

    The lock is a file in the home dir; flock is released automatically when
    this process exits. The fd is kept in a module global so it isn't closed
    (and the lock dropped) by garbage collection.
    """
    global _instance_lock
    _instance_lock = open(os.path.expanduser("~/.radio_bar.lock"), "w")
    try:
        fcntl.flock(_instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


if __name__ == "__main__":
    if os.environ.get("RADIOBAR_SELFTEST") == "1":
        # Frozen-build smoke test: does an HTTPS directory search work end to end
        # (SSL certs + JSON parse) on a worker thread, as the app runs it?
        import sys
        out = {}
        t = threading.Thread(target=lambda: out.__setitem__("hits", search_radio_browser("bbc")))
        t.start()
        t.join()
        hits = out.get("hits")
        sys.stderr.write(f"SELFTEST search -> {len(hits) if hits else 'FAILED'}\n")
        raise SystemExit(0 if hits else 1)
    if os.environ.get("RADIOBAR_PLAYTEST"):
        # Muted playback probe: report whether AVPlayer can load a URL (proves
        # whether ATS blocks http:// streams inside the bundle). Exits when done.
        import sys, Foundation
        url = os.environ["RADIOBAR_PLAYTEST"]
        player = AVFoundation.AVPlayer.playerWithURL_(
            Foundation.NSURL.URLWithString_(url))
        player.setVolume_(0.0)
        player.play()
        rl = Foundation.NSRunLoop.currentRunLoop()
        verdict = "TIMEOUT"
        for _ in range(30):
            rl.runMode_beforeDate_(Foundation.NSDefaultRunLoopMode,
                                   Foundation.NSDate.dateWithTimeIntervalSinceNow_(0.5))
            item = player.currentItem()
            if item is None:
                continue
            if item.status() == 1:      # ReadyToPlay
                verdict = "PLAYS"; break
            if item.status() == 2:      # Failed
                verdict = f"FAILED: {item.error().localizedDescription()}"; break
        sys.stderr.write(f"PLAYTEST {url} -> {verdict}\n")
        raise SystemExit(0 if verdict == "PLAYS" else 1)
    if not acquire_single_instance():
        # Another RadioBar is already running — quietly step aside.
        raise SystemExit(0)
    RadioBarApp().run()
