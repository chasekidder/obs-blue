// stream-pilot's window layout, sized from the screen instead of fixed pixels.
//
// OBS fills the left 70% of the work area; Chromium's windows (the Facebook producer
// and the comments column) the right 30%, but never narrower than they allow. The
// desktop is not always 1920x1080: in situ the monitor is a 1600x900 Dell mirroring the
// NanoKVM, and whenever the KVM's HDMI dropped the desktop became 1600x900, where fixed
// 1344x1034 OBS geometry overflowed the screen. So the layout is applied when a window
// appears and whenever the screens change, and again once Plasma has settled: during a
// screen change the panel briefly gives up its strip, then takes it back without
// moving, so the work area right after the change is too tall. kwinrulesrc keeps the
// rest (no borders, always on top, ...).

const OBS_CLASS = "com.obsproject.Studio";
// Only OBS's main window: its dialogs (Settings, Properties) are normal windows too.
const OBS_MAIN_CAPTION = "- Profile:";
const BROWSER_CLASS = "chromium-browser";
const COLUMN_CLASS = "chrome-www.facebook.com__live_producer";
const OBS_SHARE = 0.7;
const SETTLE_MS = [1500, 4000];

function role(window) {
    if (!window.normalWindow) {
        return null;
    }
    const cls = String(window.resourceClass);
    if (cls === OBS_CLASS && String(window.caption).indexOf(OBS_MAIN_CAPTION) >= 0) {
        return "left";
    }
    if (cls === BROWSER_CLASS || cls.indexOf(COLUMN_CLASS) === 0) {
        return "right";
    }
    return null;
}

function rightWidth(area) {
    // At 1600x900 OBS gives up the difference rather than Chromium (at least 500 px
    // wide) running off the screen.
    let width = area.width - Math.round(area.width * OBS_SHARE);
    const windows = workspace.windowList();
    for (let i = 0; i < windows.length; i++) {
        if (role(windows[i]) === "right") {
            width = Math.max(width, Math.ceil(windows[i].minSize.width));
        }
    }
    return width;
}

function place(window) {
    const side = role(window);
    if (side === null) {
        return;
    }
    const area = workspace.clientArea(KWin.MaximizeArea, window.output, workspace.currentDesktop);
    const left = area.width - rightWidth(area);
    const t = side === "left"
        ? {x: area.x, y: area.y, width: left, height: area.height}
        : {x: area.x + left, y: area.y, width: area.width - left, height: area.height};
    const g = window.frameGeometry;
    if (g.x !== t.x || g.y !== t.y || g.width !== t.width || g.height !== t.height) {
        console.info("stream-pilot layout: " + window.resourceClass + " -> "
                     + t.x + "," + t.y + " " + t.width + "x" + t.height);
        window.frameGeometry = t;
    }
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

function watch(window) {
    place(window);
    window.captionChanged.connect(() => place(window));  // OBS sets its caption after mapping
    window.outputChanged.connect(() => place(window));
}

workspace.windowAdded.connect(watch);
workspace.screensChanged.connect(screensChanged);
workspace.virtualScreenGeometryChanged.connect(screensChanged);
const existing = workspace.windowList();
for (let i = 0; i < existing.length; i++) {
    watch(existing[i]);
}
