"""Pure-parsing tests for fb/selectors.py.

Test data convention: fake Facebook-looking values live in
tests/fixtures/fb/values.json (the secret grep excludes tests/fixtures), never
inline in this file.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from stream_pilot.fb import selectors as S

VALUES = json.loads((Path(__file__).parent / "fixtures" / "fb" / "values.json").read_text())
KEY = VALUES["stream_key"]
SERVER = VALUES["server_url"]
LIVE_ID = VALUES["live_id"]
COMMENT_ID = VALUES["comment_id"]


# ---- pick_stream_key -------------------------------------------------------


def test_pick_stream_key_from_mixed_list():
    assert S.pick_stream_key(["", "Stream key", f"  {KEY}\n", SERVER]) == KEY


def test_pick_stream_key_ignores_masked_values():
    masked = ["••••••••••••••••••••", "FB-••••••••••••••••••••", "FB-********************", "hidden", "Hidden key"]
    assert S.pick_stream_key(masked) is None
    assert S.pick_stream_key([*masked, KEY]) == KEY


def test_pick_stream_key_ignores_masked_even_if_it_contains_key_chars():
    assert S.pick_stream_key([f"{KEY}••", f"{KEY} (hidden)", f"{KEY}**"]) is None


def test_pick_stream_key_none_when_absent():
    assert S.pick_stream_key([]) is None
    assert S.pick_stream_key(["foo", "FB-short", SERVER]) is None


def test_pick_stream_key_returns_first_match():
    other = KEY[:-4] + "ZZZZ"
    assert S.pick_stream_key([other, KEY]) == other


def test_stream_key_re_is_anchored():
    assert S.STREAM_KEY_RE.match(KEY)
    assert not S.STREAM_KEY_RE.match(f"x{KEY}")
    assert not S.STREAM_KEY_RE.match(f"{KEY} trailing")


# ---- pick_server_url -------------------------------------------------------


def test_pick_server_url_from_mixed_list():
    assert S.pick_server_url([KEY, "Server URL", f" {SERVER} "]) == SERVER


@pytest.mark.parametrize(
    "raw",
    [
        "rtmps://edgetee-upload-test.xx.fbcdn.net:443/rtmp",
        "rtmps://edgetee-upload-test.xx.fbcdn.net:443/rtmp/",
        "rtmps://edgetee-upload-test.xx.fbcdn.net/rtmp",
    ],
)
def test_pick_server_url_normalizes_trailing_slash(raw):
    out = S.pick_server_url([raw])
    assert out is not None
    assert out.endswith("/rtmp/") and not out.endswith("//")
    assert out.startswith("rtmps://edgetee-upload-test.xx.fbcdn.net")


def test_pick_server_url_rejects_other_hosts_and_schemes():
    assert S.pick_server_url(["rtmps://evil.example.com:443/rtmp/"]) is None
    assert S.pick_server_url(["rtmp://edgetee-upload-test.xx.fbcdn.net:443/rtmp/"]) is None
    assert S.pick_server_url(["rtmps://edgetee-upload-test.xx.fbcdn.net:443/other/"]) is None
    assert S.pick_server_url([]) is None


def test_pick_server_url_is_case_insensitive():
    assert S.pick_server_url(["RTMPS://Edge.XX.FBCDN.NET:443/RTMP"]) is not None


def test_pick_server_url_accepts_facebooks_live_api_ingest():
    # The setup page's Server URL as seen on the stream PC (2026-11-21).
    assert S.pick_server_url(["rtmps://live-api-s.facebook.com:443/rtmp/"]) == (
        "rtmps://live-api-s.facebook.com:443/rtmp/"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "rtmps://facebook.com.evil.example/rtmp/",
        "rtmps://evilfacebook.com:443/rtmp/",
        "rtmps://live-api-s.facebook.com.evil.example:443/rtmp/",
        "rtmps://fbcdn.net.evil.example/rtmp/",
        "rtmps://live-api-s.facebook.com:8443/rtmp/",
        "rtmps://user@live-api-s.facebook.com/rtmp/",
    ],
)
def test_pick_server_url_rejects_look_alike_hosts(raw):
    assert S.pick_server_url([raw]) is None


# ---- live_id_from_url ------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.facebook.com/live/producer/{LIVE_ID}",
        f"https://www.facebook.com/live/producer/{LIVE_ID}/",
        f"https://www.facebook.com/live/producer/{LIVE_ID}?ref=x&foo=1",
        f"https://www.facebook.com/live/producer/dashboard/{LIVE_ID}/COMMENTS",
        f"https://www.facebook.com/live/producer/dashboard/{LIVE_ID}/?tab=1",
        f"/live/producer/dashboard/{LIVE_ID}",
    ],
)
def test_live_id_from_url(url):
    assert S.live_id_from_url(url) == LIVE_ID


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://www.facebook.com/live/producer/",
        "https://www.facebook.com/live/producer/dashboard/",
        "https://www.facebook.com/live/producer/123",
        "https://www.facebook.com/groups/foo",
    ],
)
def test_live_id_from_url_none(url):
    assert S.live_id_from_url(url) is None


# ---- comments --------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "author"),
    [
        ("Comment by Jane Doe", "Jane Doe"),
        ("Comment by Jane Doe 5 minutes ago", "Jane Doe"),
        ("Comment by Jane Doe a minute ago", "Jane Doe"),
        ("Comment by Jane Doe just now", "Jane Doe"),
        ("Comment by Jane Doe now", "Jane Doe"),
        ("Comment by Jane Doe 2 hours ago", "Jane Doe"),
        ("comment by Zoë O'Neil-Smith 1 week ago", "Zoë O'Neil-Smith"),
    ],
)
def test_parse_comment_label(label, author):
    assert S.parse_comment_label(label) == author


@pytest.mark.parametrize("label", ["", "Reply by Jane", "Jane Doe", "Comment by "])
def test_parse_comment_label_none(label):
    assert S.parse_comment_label(label) is None


def test_parse_comment_label_strips_whitespace():
    assert S.parse_comment_label("  Comment by Jane Doe  ") == "Jane Doe"


def test_comment_id_from_href():
    href = f"https://www.facebook.com/groups/1/posts/2/?comment_id={COMMENT_ID}&reply_comment_id=5"
    assert S.comment_id_from_href(href) == COMMENT_ID
    assert S.comment_id_from_href(f"/x?a=1&comment_id={COMMENT_ID}") == COMMENT_ID


def test_comment_id_from_href_none():
    assert S.comment_id_from_href("") is None
    assert S.comment_id_from_href(None) is None
    assert S.comment_id_from_href("https://www.facebook.com/x?reply_comment_id=5") is None


# ---- case-insensitive regexes ----------------------------------------------


@pytest.mark.parametrize(
    ("rx", "text"),
    [
        (S.GO_LIVE, "Go Live"),
        (S.GO_LIVE, "  GO LIVE  "),
        (S.GO_LIVE_START, "Go live"),
        (S.GO_LIVE_START, "Set up live video"),
        (S.STREAMING_SOFTWARE, "Streaming Software"),
        (S.ADVANCED_SETTINGS, "ADVANCED SETTINGS"),
        (S.PERSISTENT_KEY, "Use a Persistent Stream Key"),
        (S.END_LIVE_WHEN_STREAM_STOPS, "End live video when the stream stops"),
        (S.END_LIVE_WHEN_STREAM_STOPS, "END LIVE VIDEO WHEN ENCODER STOPS"),
        (S.END_LIVE_VIDEO, "End Live Video"),
        (S.INTERSTITIAL_CONFIRM, "Go live now"),
        (S.INTERSTITIAL_CONFIRM, " CONTINUE "),
        (S.INTERSTITIAL_CONFIRM, "Skip"),
        (S.PREVIEW_LOST_TEXT, "Stream is disconnected"),
        (S.PREVIEW_LOST_TEXT, "STREAM OFFLINE"),
        (S.PREVIEW_LOST_TEXT, "Stream not yet detected"),
        (S.PREVIEW_LOST_TEXT, "Stream not connected"),
        (S.PREVIEW_LOST_TEXT, "Lost connection to encoder"),
    ],
)
def test_regexes_match_case_insensitively(rx, text):
    assert rx.search(text)


@pytest.mark.parametrize(
    ("rx", "text"),
    [
        (S.GO_LIVE, "Go live now"),
        (S.GO_LIVE, "Going live"),
        (S.GO_LIVE, "Set up live video"),  # the final go-live click stays strict
        (S.GO_LIVE_START, "Create event"),
        (S.GO_LIVE_START, "Create live video event"),
        (S.GO_LIVE_START, "Going live"),
        (S.END_LIVE_VIDEO, "End live video when stream stops"),
        (S.INTERSTITIAL_CONFIRM, "Go live later"),
        (S.PREVIEW_LOST_TEXT, "Stream is healthy"),
    ],
)
def test_regexes_reject(rx, text):
    assert not rx.search(text)


def test_end_live_when_stream_stops_does_not_match_plain_end_live_video():
    assert not S.END_LIVE_WHEN_STREAM_STOPS.search("End live video")


def test_selector_constants():
    assert "input[name='email']" in S.LOGIN_FORM_SELECTOR
    assert "form[action*='login']" in S.LOGIN_FORM_SELECTOR
    assert S.COMMENT_ARTICLE_SELECTOR == "[role='article']"
    assert all(
        rx.flags & re.I
        for rx in (
            S.GO_LIVE, S.GO_LIVE_START, S.STREAMING_SOFTWARE, S.ADVANCED_SETTINGS, S.PERSISTENT_KEY,
            S.END_LIVE_WHEN_STREAM_STOPS, S.END_LIVE_VIDEO, S.INTERSTITIAL_CONFIRM,
            S.SERVER_URL_RE, S.COMMENT_LABEL_RE, S.PREVIEW_LOST_TEXT,
        )
    )


# ---- js_regex_source -------------------------------------------------------


def test_js_regex_source_comment_label():
    lit = S.js_regex_source(S.COMMENT_LABEL_RE)
    assert lit.startswith("/^Comment by (?<author>.+?)")
    assert "(?P<" not in lit
    assert lit.endswith("$/i")


def test_js_regex_source_comment_id():
    assert S.js_regex_source(S.COMMENT_ID_IN_HREF_RE) == r"/[?&]comment_id=(\d+)/"


def test_js_regex_parts():
    body, flags = S.js_regex_parts(S.COMMENT_ID_IN_HREF_RE)
    assert (body, flags) == (r"[?&]comment_id=(\d+)", "")
    assert S.js_regex_parts(S.GO_LIVE)[1] == "i"


def test_js_regex_flags_mapping():
    assert S.js_regex_parts(re.compile("a", re.I | re.M | re.S))[1] == "ims"
    # Leading inline flags are folded into rx.flags, so they just become JS flags.
    assert S.js_regex_parts(re.compile("(?i)abc")) == ("abc", "i")


def test_js_regex_escapes_slash():
    assert S.js_regex_parts(re.compile("a/b"))[0] == r"a\/b"
    assert S.js_regex_parts(re.compile("a\\/b"))[0] == r"a\/b"


def test_js_regex_slash_in_class_not_double_escaped():
    body = S.js_regex_parts(re.compile("[/x]"))[0]
    assert re.fullmatch(r"\[\\?/x\]", body)


@pytest.mark.parametrize(
    "pattern",
    [r"(?P<n>a)(?P=n)", r"\Aabc", r"abc\Z", r"(?>abc)", r"(?#comment)", r"(a)?(?(1)b|c)", r"a(?i:b)"],
)
def test_js_regex_unsupported_raises(pattern):
    with pytest.raises(ValueError):
        S.js_regex_source(re.compile(pattern))


def test_js_regex_unsupported_flags_raise():
    with pytest.raises(ValueError):
        S.js_regex_source(re.compile("a", re.X))
    with pytest.raises(ValueError):
        S.js_regex_source(re.compile(b"a"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_regex_source_is_valid_and_equivalent_in_node():
    label = "Comment by Jane Doe 5 minutes ago"
    href = f"/x?comment_id={COMMENT_ID}"
    script = (
        f"const a={S.js_regex_source(S.COMMENT_LABEL_RE)};"
        f"const b={S.js_regex_source(S.COMMENT_ID_IN_HREF_RE)};"
        f"const c={S.js_regex_source(S.STREAM_KEY_RE)};"
        f"const d={S.js_regex_source(S.LIVE_ID_IN_URL_RE)};"
        f"console.log(JSON.stringify([a.exec({label!r}).groups.author,"
        f"b.exec({href!r})[1], c.test({KEY!r}), d.exec('/live/producer/dashboard/{LIVE_ID}/x')[1]]));"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20, check=True)
    assert json.loads(out.stdout) == ["Jane Doe", COMMENT_ID, True, LIVE_ID]


def test_group_name_pattern_is_literal_and_case_insensitive():
    rx = S.group_name_pattern(" Test (Resale) Group+ ")
    assert rx.search("Posting to test (resale) group+ now")
    assert not rx.search("Test Resale Group")


def test_group_name_pattern_rejects_an_empty_name():
    for blank in ("", "   ", "\n\t"):
        with pytest.raises(ValueError):
            S.group_name_pattern(blank)


def test_title_field_matches_title_labels_only():
    for label in ("Add title", "Title", "Live video TITLE (required)"):
        assert S.TITLE_FIELD.search(label), label
    assert not S.TITLE_FIELD.search("Description")
    assert S.DEFAULT_LIVE_TITLE.strip()
    # required-field selector is a plain CSS selector list, not a regex
    assert "[required]" in S.REQUIRED_FIELD_SELECTOR


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.facebook.com/two_step_verification/authentication/?encrypted_context=x", True),
        ("https://www.facebook.com/login/?next=https%3A%2F%2Fwww.facebook.com%2Flive%2Fproducer", True),
        ("https://www.facebook.com/login.php", True),
        ("https://www.facebook.com/checkpoint/?next", True),
        ("https://www.facebook.com/live/producer/?actor=1&entry_point=group&target_id=2", False),
        ("https://www.facebook.com/groups/2423682404705025", False),
        ("https://www.facebook.com/", False),
        ("about:blank", False),
    ],
)
def test_is_login_flow_url(url, expected):
    from stream_pilot.fb.producer import is_login_flow_url

    assert is_login_flow_url(url) is expected

