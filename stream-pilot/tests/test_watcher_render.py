"""Host (non-browser) tests for rendering fb/watcher.js from selectors.py."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from stream_pilot.fb import selectors as S
from stream_pilot.fb.comments import render_watcher, watcher_template


def test_template_is_shipped_package_data_and_uses_placeholders():
    tpl = watcher_template()
    assert "__ARTICLE_SELECTOR__" in tpl and "__LABEL_RE__" in tpl and "__COMMENT_ID_RE__" in tpl


def test_rendered_script_has_no_leftover_placeholders():
    assert not re.findall(r"__[A-Z][A-Z0-9_]*__", render_watcher())


def test_unknown_placeholder_is_an_error():
    with pytest.raises(RuntimeError, match="__NOPE__"):
        render_watcher("const x = __NOPE__;")


def test_js_regex_literals_equal_selectors_output():
    script = render_watcher()
    assert f"const LABEL_RE = {S.js_regex_source(S.COMMENT_LABEL_RE)};" in script
    assert f"const COMMENT_ID_RE = {S.js_regex_source(S.COMMENT_ID_IN_HREF_RE)};" in script


def test_selectors_are_injected_as_js_string_literals():
    script = render_watcher()
    for name, value in {
        "ARTICLE_SELECTOR": S.COMMENT_ARTICLE_SELECTOR,
        "LABEL_ATTR": S.COMMENT_LABEL_ATTR,
        "AUTHOR_LINK_SELECTOR": S.COMMENT_AUTHOR_LINK_SELECTOR,
        "PERMALINK_SELECTOR": S.COMMENT_PERMALINK_SELECTOR,
        "TIME_SELECTOR": S.COMMENT_TIME_SELECTOR,
        "TIME_UTIME_ATTR": S.COMMENT_TIME_UTIME_ATTR,
        "TIME_DATETIME_ATTR": S.COMMENT_TIME_DATETIME_ATTR,
        "COMMENTS_PATH": S.COMMENTS_PATH_MARKER,
    }.items():
        assert f"const {name} = {json.dumps(value)};" in script


def test_watcher_template_holds_no_facebook_markup_literals():
    """The only Facebook-specific text in the template lives behind placeholders."""
    tpl = watcher_template()
    for needle in ("role=", "comment_id=", "aria-label", "data-utime", "/COMMENTS", "Comment by", "[role"):
        code = "\n".join(line for line in tpl.splitlines() if not line.lstrip().startswith("//"))
        assert needle not in code, needle


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_rendered_script_passes_node_check(tmp_path):
    path = tmp_path / "watcher.js"
    path.write_text(render_watcher())
    subprocess.run(["node", "--check", str(path)], check=True, capture_output=True, timeout=30)
