"""All Facebook-specific knowledge: page text, roles, CSS and URL patterns.

This is the ONLY module allowed to contain such strings. Everything else in
stream-pilot matches through these constants, via Playwright role/text
locators (never pixel coordinates or obfuscated class names).

Everything here is pure (no I/O, no Playwright import).
"""

from __future__ import annotations

import re

# ---- Reachability -----------------------------------------------------------

# Any HTTP answer below 500 from here means "Facebook is reachable" (see CdpBrowser.http_reachable).
REACHABILITY_URL = "https://www.facebook.com/"

# ---- Producer page text (case-insensitive) ---------------------------------

GO_LIVE = re.compile(r"^\s*go live\s*$", re.I)
# The button that starts setting up a live on the producer homepage. Seen on the
# stream PC (2026-11-21, "Create live video" page): its "Go live" option's
# button is "Set up live video". Separate from GO_LIVE so the final go-live
# click stays strict.
GO_LIVE_START = re.compile(r"^\s*(go live|set up live video)\s*$", re.I)
# The producer's step tracker ("1 / 3": Connect video source, Complete post details,
# Go live) has an always-enabled "Go live" step button before the real Go live
# button, which stays disabled until Facebook receives the stream. Seen on the
# stream PC (2026-11-21). The real one is the last "Go live" button on the page.
GO_LIVE_IS_LAST_MATCH = True
STREAMING_SOFTWARE = re.compile(r"streaming software", re.I)
ADVANCED_SETTINGS = re.compile(r"advanced settings", re.I)
PERSISTENT_KEY = re.compile(r"persistent stream key", re.I)
END_LIVE_WHEN_STREAM_STOPS = re.compile(r"end live video.*(stream|encoder).*stop", re.I)
END_LIVE_VIDEO = re.compile(r"^\s*end live video\s*$", re.I)
INTERSTITIAL_CONFIRM = re.compile(r"^\s*(go live|go live now|continue|skip)\s*$", re.I)
PREVIEW_LOST_TEXT = re.compile(
    r"(stream (is )?(disconnected|offline|not (yet )?(detected|connected)))|(lost connection)", re.I
)

# ---- CSS -------------------------------------------------------------------

LOGIN_FORM_SELECTOR = "input[name='email'], input[name='pass'], form[action*='login']"
# URL paths of Facebook's login flow beyond the form, where a reload would lose the
# host's progress. Seen on the stream PC (2026-11-21): the form is shown on
# /live/producer/ itself, then 2FA at /two_step_verification/authentication/.
LOGIN_FLOW_PATH_PREFIXES = ("/login", "/two_step_verification/", "/checkpoint/")
# The label keeps out other articles on the dashboard (e.g. a docked Messenger chat).
COMMENT_ARTICLE_SELECTOR = "[role='article'][aria-label^='Comment by']"

# ---- Value / URL patterns --------------------------------------------------

STREAM_KEY_RE = re.compile(r"^FB-[A-Za-z0-9_-]{16,}$")
# Only ever send the stream to Facebook. The setup page showed
# rtmps://live-api-s.facebook.com:443/rtmp/ on the stream PC (2026-11-21).
SERVER_URL_RE = re.compile(
    r"^rtmps://([a-z0-9-]+\.)+(fbcdn\.net|facebook\.com)(:443)?/rtmp/?$", re.I
)
# The 2026 producer puts its pages under /live/producer/v2/<id>/ (seen on the stream PC,
# 2026-11-21, during setup; that the id is the live video id still needs a real live).
LIVE_ID_IN_URL_RE = re.compile(r"/live/producer/(?:v2/)?(?:dashboard/)?(\d{6,})")
COMMENT_ID_IN_HREF_RE = re.compile(r"[?&]comment_id=(\d+)")
# A reply's permalink carries its parent's comment_id as well; the reply's own id wins.
REPLY_COMMENT_ID_IN_HREF_RE = re.compile(r"[?&]reply_comment_id=(\d+)")
COMMENT_LABEL_RE = re.compile(
    r"^Comment by (?P<author>.+?)(?: (?:(?:about )?(?:\d+|an?|a few) \w+ ago|just now|now))?$", re.I
)

_MASKED_RE = re.compile(r"•|\*{2,}|hidden", re.I)


# ---- Pure parsing ----------------------------------------------------------


def _is_masked(value: str) -> bool:
    return bool(_MASKED_RE.search(value))


def pick_stream_key(values: list[str]) -> str | None:
    """First stripped, unmasked value that looks like a stream key."""
    for raw in values:
        value = (raw or "").strip()
        if not value or _is_masked(value):
            continue
        if STREAM_KEY_RE.match(value):
            return value
    return None


def pick_server_url(values: list[str]) -> str | None:
    """First stripped value that looks like an RTMPS server URL, ending in exactly '/rtmp/'."""
    for raw in values:
        value = (raw or "").strip()
        if value and SERVER_URL_RE.match(value):
            return value.rstrip("/") + "/"
    return None


def live_id_from_url(url: str) -> str | None:
    """Live video id from `/live/producer/[v2/][dashboard/]<id>/...` URLs."""
    m = LIVE_ID_IN_URL_RE.search(url or "")
    return m.group(1) if m else None


def parse_comment_label(label: str) -> str | None:
    """Author name from an aria-label like 'Comment by Jane Doe 5 minutes ago'."""
    m = COMMENT_LABEL_RE.match((label or "").strip())
    if not m:
        return None
    author = m.group("author").strip()
    return author or None


def comment_id_from_href(href: str | None) -> str | None:
    """Comment id from a permalink: `reply_comment_id=` for a reply, else `comment_id=`."""
    m = REPLY_COMMENT_ID_IN_HREF_RE.search(href or "") or COMMENT_ID_IN_HREF_RE.search(href or "")
    return m.group(1) if m else None


# ---- Python regex -> JS regex literal (for the in-page watcher) ------------

_JS_FLAGS = ((re.IGNORECASE, "i"), (re.MULTILINE, "m"), (re.DOTALL, "s"))
_LEADING_INLINE_FLAGS = re.compile(r"^\(\?[aiLmsux]+\)")


def js_regex_parts(rx: re.Pattern) -> tuple[str, str]:
    """Return (body, flags) of an equivalent JS regex for simple Python patterns.

    Handles: `(?P<n>...)` -> `(?<n>...)`, leading inline flags / compile flags
    i, m, s -> JS flags, unescaped '/' -> '\\/'. Raises ValueError for
    constructs with no JS equivalent (backrefs by name, \\A, \\Z, atomic groups,
    comments, conditionals, scoped flag groups, verbose/ASCII/bytes patterns).
    """
    if not isinstance(rx.pattern, str):
        raise ValueError("bytes patterns are unsupported")
    allowed = re.UNICODE | sum(f for f, _ in _JS_FLAGS)
    if rx.flags & ~allowed:
        raise ValueError(f"unsupported regex flags: {rx.flags & ~allowed:#x}")
    flags = "".join(j for f, j in _JS_FLAGS if rx.flags & f)

    pat = _LEADING_INLINE_FLAGS.sub("", rx.pattern)  # already reflected in rx.flags
    out: list[str] = []
    in_class = False
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "\\":
            if i + 1 >= n:
                raise ValueError("dangling backslash")
            nxt = pat[i + 1]
            if nxt in "AZ":
                raise ValueError(f"unsupported escape \\{nxt}")
            out.append("\\/" if nxt == "/" else c + nxt)
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
            out.append("\\/" if c == "/" else c)
            i += 1
            continue
        if c == "[":
            in_class = True
            out.append(c)
            i += 1
            # a leading ']' (after optional '^') is a literal inside the class
            if i < n and pat[i] == "^":
                out.append("^")
                i += 1
            if i < n and pat[i] == "]":
                out.append("\\]")
                i += 1
            continue
        if c == "(" and pat.startswith("(?", i):
            rest = pat[i + 2 :]
            if rest.startswith("P<"):
                out.append("(?<")
                i += 4
                continue
            if rest[:1] in (":", "=", "!") or rest[:2] in ("<=", "<!"):
                out.append("(?")
                i += 2
                continue
            raise ValueError(f"unsupported group construct: {pat[i:i + 4]!r}")
        out.append("\\/" if c == "/" else c)
        i += 1
    return "".join(out), flags


def js_regex_source(rx: re.Pattern) -> str:
    """JS regex literal source (e.g. '/^a(?<x>b)$/i') equivalent to a simple Python regex."""
    body, flags = js_regex_parts(rx)
    return f"/{body}/{flags}"


# ==== Producer-flow additions (fb/producer.py) ===============================
# Playwright role names, attributes, dialog/title handling and DOM queries used
# by the producer flows. Kept here so producer.py holds no Facebook knowledge.

ROLE_BUTTON = "button"
ROLE_RADIO = "radio"
ROLE_SWITCH = "switch"
ROLE_CHECKBOX = "checkbox"
ROLE_DIALOG = "dialog"
ROLE_TEXTBOX = "textbox"

ATTR_ARIA_CHECKED = "aria-checked"
ATTR_ARIA_DISABLED = "aria-disabled"
ATTR_ARIA_EXPANDED = "aria-expanded"

# Textbox (role) whose accessible name marks it as the post title in the go-live interstitial.
TITLE_FIELD = re.compile(r"title", re.I)
# Marks a control the interstitial insists on. Only *textboxes* matching this are ever filled
# (with DEFAULT_LIVE_TITLE, when empty); other required controls are skipped and logged.
REQUIRED_FIELD_SELECTOR = "[required], [aria-required='true']"
# Neutral title typed into an empty title/required textbox on the go-live interstitial.
DEFAULT_LIVE_TITLE = "Live"

# Elements whose value/text may hold the server URL or stream key.
VALUE_ELEMENTS_SELECTOR = "input, textarea"
TEXT_ELEMENTS_SELECTOR = "[data-testid], code, span"
MAX_TEXT_ELEMENTS = 2000


def group_name_pattern(group_name: str) -> re.Pattern:
    """Case-insensitive, literal (regex-escaped) match for the configured Group name.

    Raises ValueError for an empty name (it would match every page). Matching is by substring:
    "Foo Group" also matches "Foo Group 2".
    """
    name = group_name.strip()
    if not name:
        raise ValueError("group name must not be empty")
    return re.compile(re.escape(name), re.I)


# ---- Comment watcher (rendered into fb/watcher.js by fb/comments.py) -------

# The 2026 producer has no comments-only page (the old /live/producer/dashboard/<id>/COMMENTS/
# popout renders blank): the chat column is the live's dashboard, whose Comments module the
# watcher pins over the whole window. stream-pilot appends this hash marker to the column's
# URL (Facebook keeps it) so the column is never confused with the main producer tab, which
# shows the same dashboard during go-live.
COMMENTS_URL_MARKER = "#stream-pilot-comments"


def popout_url_fragment(live_id: str) -> str:
    """Appended to the comments URL; identifies the comments column of ``live_id``."""
    return f"{COMMENTS_URL_MARKER}/{live_id}/"


def is_popout_url(url: str) -> bool:
    """True if ``url`` is a comments column page (of any live)."""
    return COMMENTS_URL_MARKER in url


# The dashboard's Comments module: the nearest ancestor of its heading that also holds
# the comment box. The watcher pins it over the window (it is off-screen in a narrow one).
COMMENTS_MODULE_HEADING_SELECTOR = "h1, h2, [role='heading']"
COMMENTS_MODULE_HEADING_TEXT = "Comments"
COMMENTS_MODULE_ANCHOR_SELECTOR = "[role='textbox']"
# Hidden in the pinned module: minimizing it would collapse the list the watcher reads.
COMMENTS_MODULE_HIDE_SELECTOR = "[aria-label='Minimize comments']"
# Uncapped in the pinned module: the dashboard sizes each comment with max-width:
# fit-content for its narrow original column, so in the chat window the comments only
# filled about half the width (seen on the stream PC, 2026-11-24).
COMMENTS_MODULE_UNCAP_SELECTOR = "div"


COMMENT_LABEL_ATTR = "aria-label"
# The commenter's profile link inside a comment (first one whose text is the author).
COMMENT_AUTHOR_LINK_SELECTOR = "a[role='link']"
# Permalink carrying the comment id; excluded from the comment text.
COMMENT_PERMALINK_SELECTOR = "a[href*='comment_id=']"
# The comment's controls (Like, Reply, Pin, See thread, ...); excluded from the comment text.
COMMENT_CONTROLS_SELECTOR = "[role='button'], [role='list']"
# Timestamp elements (also excluded from the comment text) and where their value lives.
COMMENT_TIME_SELECTOR = "abbr[data-utime], time[datetime]"
COMMENT_TIME_UTIME_ATTR = "data-utime"
COMMENT_TIME_DATETIME_ATTR = "datetime"


# ---- Post details: the live's title (fb/producer.py set_title) ---------------

# The 2026 producer's "Add post details" section opens the post composer with this
# button; once details are saved it reads "Edit Post Details". Seen on the stream PC
# (2026-11-24).
POST_DETAILS_BUTTON = re.compile(
    r"^\s*(what[’']s your live video about\?|edit post details)\s*$", re.I
)
# The composer ("Create post") has a "Title (optional)" textbox, a "Description"
# textbox and a "Save" button.
POST_TITLE_FIELD = re.compile(r"^\s*title\b", re.I)
POST_DETAILS_SAVE = re.compile(r"^\s*save\s*$", re.I)
# Its close button (used when filling the title failed, so it cannot cover Go live).
POST_COMPOSER_CLOSE = re.compile(r"^\s*close composer dialog\s*$", re.I)
