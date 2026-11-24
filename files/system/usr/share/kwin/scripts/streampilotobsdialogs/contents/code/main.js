// OBS opens a modal "Failed to connect ... check your stream key" box whenever a stream
// start fails. On the stream PC that is almost always Facebook refusing the persistent
// key for about a minute after the previous stream stopped: the key is fine, and
// stream-pilot retries by itself. The message would mislead the host, who reads the
// real state on the Stream Deck's status key (SETUP, LIVE, RECONNECT, or ERROR with the
// failed step), so the box is closed as soon as it appears.

const OBS_CLASS = "com.obsproject.Studio";
const CAPTION = "Failed to connect";

function closeIfConnectFailure(window) {
    if (window.resourceClass === OBS_CLASS && window.caption === CAPTION) {
        print("stream-pilot: closing OBS's \"" + CAPTION + "\" dialog");
        window.closeWindow();
    }
}

function watch(window) {
    if (window.resourceClass !== OBS_CLASS) {
        return;
    }
    closeIfConnectFailure(window);
    window.captionChanged.connect(() => closeIfConnectFailure(window));
}

workspace.windowAdded.connect(watch);
const windows = workspace.windowList();
for (let i = 0; i < windows.length; i++) {
    watch(windows[i]);
}
