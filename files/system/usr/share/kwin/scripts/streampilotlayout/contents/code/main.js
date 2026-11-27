// stream-pilot's window layout, sized from the screen instead of fixed pixels.
//
// The Facebook comments column fills the left 30% of the work area (never narrower
// than it allows), OBS the right 70%: the host reads the chat on the side of the
// monitor nearest to her. The desktop is not always 1920x1080: in situ the monitor is
// a 1600x900 Dell mirroring the NanoKVM, and whenever the KVM's HDMI dropped the
// desktop became 1600x900, where fixed 1344x1034 OBS geometry overflowed the screen.
// So the layout is applied when a window appears and whenever the screens change, and
// again once Plasma has settled: during a screen change the panel briefly gives up its
// strip, then takes it back without moving, so the work area right after the change
// is too tall.
//
// The host wins: a window she moved or resized herself is only fitted again when it
// no longer fits the screen. Chromium's ordinary windows are hers too: only the first
// one of each Chromium start (stream-pilot's own, with the producer page) is put in
// the column's place, once, when it opens. While stream-pilot is paused this script
// is unloaded (KWin reconfigure loads it again).

const OBS_CLASS = "com.obsproject.Studio";
// Only OBS's main window: its dialogs (Settings, Properties) are normal windows too.
const OBS_MAIN_CAPTION = "- Profile:";
const BROWSER_CLASS = "chromium-browser";
const COLUMN_CLASS = "chrome-www.facebook.com__live_producer";
const OBS_SHARE = 0.7;
const SETTLE_MS = [1500, 4000];

const placedBrowserPids = {};
const hostPlaced = new Set();

function role(window) {
    if (!window.normalWindow) {
        return null;
    }
    const cls = String(window.resourceClass);
    if (cls === OBS_CLASS && String(window.caption).indexOf(OBS_MAIN_CAPTION) >= 0) {
        return "right";
    }
    if (cls.indexOf(COLUMN_CLASS) === 0) {
        return "left";
    }
    return null;
}

function workArea(window) {
    return workspace.clientArea(KWin.MaximizeArea, window.output, workspace.currentDesktop);
}

function leftWidth(area) {
    // At 1600x900 OBS gives up the difference rather than the column (at least 500 px
    // wide) running off the screen.
    let width = area.width - Math.round(area.width * OBS_SHARE);
    const windows = workspace.windowList();
    for (let i = 0; i < windows.length; i++) {
        if (role(windows[i]) === "left") {
            width = Math.max(width, Math.ceil(windows[i].minSize.width));
        }
    }
    return width;
}

function target(window, side) {
    const area = workArea(window);
    const left = leftWidth(area);
    return side === "left"
        ? {x: area.x, y: area.y, width: left, height: area.height}
        : {x: area.x + left, y: area.y, width: area.width - left, height: area.height};
}

function fits(window) {
    const a = workArea(window);
    const g = window.frameGeometry;
    return g.x >= a.x && g.y >= a.y && g.x + g.width <= a.x + a.width && g.y + g.height <= a.y + a.height;
}

function apply(window, t) {
    const g = window.frameGeometry;
    if (g.x !== t.x || g.y !== t.y || g.width !== t.width || g.height !== t.height) {
        console.info("stream-pilot layout: " + window.resourceClass + " -> "
                     + t.x + "," + t.y + " " + t.width + "x" + t.height);
        window.frameGeometry = t;
    }
}

function place(window) {
    const side = role(window);
    if (side === null || (hostPlaced.has(window) && fits(window))) {
        return;
    }
    hostPlaced.delete(window);
    apply(window, target(window, side));
}

function placeAll() {
    const windows = workspace.windowList();
    for (let i = 0; i < windows.length; i++) {
        place(windows[i]);
    }
}

const settleTimers = SETTLE_MS.map((ms) => {
    const timer = new QTimer();
    timer.singleShot = true;
    timer.interval = ms;
    timer.timeout.connect(placeAll);
    return timer;
});

function screensChanged() {
    placeAll();
    settleTimers.forEach((timer) => timer.start());
}

function placeFirstBrowserWindow(window) {
    if (!window.normalWindow || String(window.resourceClass) !== BROWSER_CLASS) {
        return;
    }
    if (placedBrowserPids[window.pid]) {
        return;
    }
    placedBrowserPids[window.pid] = true;
    apply(window, target(window, "left"));
}

function watch(window) {
    place(window);
    window.captionChanged.connect(() => place(window));  // OBS sets its caption after mapping
    window.outputChanged.connect(() => place(window));
    window.interactiveMoveResizeFinished.connect(() => hostPlaced.add(window));
    window.closed.connect(() => hostPlaced.delete(window));
}

workspace.windowAdded.connect((window) => {
    placeFirstBrowserWindow(window);
    watch(window);
});
workspace.screensChanged.connect(screensChanged);
workspace.virtualScreenGeometryChanged.connect(screensChanged);
const existing = workspace.windowList();
for (let i = 0; i < existing.length; i++) {
    if (String(existing[i].resourceClass) === BROWSER_CLASS) {
        placedBrowserPids[existing[i].pid] = true;  // already open: the host may have moved it
    }
    watch(existing[i]);
}
