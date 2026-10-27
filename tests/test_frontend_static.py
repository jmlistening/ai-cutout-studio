# coding: utf-8

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "static" / "index.html"
JS = ROOT / "static" / "app.js"
CSS = ROOT / "static" / "style.css"


def test_every_js_element_id_exists_in_html():
    html = HTML.read_text(encoding="utf-8")
    js = JS.read_text(encoding="utf-8")

    html_ids = set(re.findall(r'\bid="([^"]+)"', html))
    js_ids = set(re.findall(r'getElementById\("([^"]+)"\)', js))

    missing = sorted(js_ids - html_ids)
    assert not missing, f"app.js 引用了不存在的 id: {missing}"


def test_static_assets_referenced_by_html_exist():
    html = HTML.read_text(encoding="utf-8")
    for path in re.findall(r'(?:href|src)="/static/([^"]+)"', html):
        assert (ROOT / "static" / path).is_file(), f"缺少静态文件 {path}"


def test_view_switch_has_all_modes():
    html = HTML.read_text(encoding="utf-8")
    for view in ("compare", "result", "source"):
        assert f'data-view="{view}"' in html


def test_css_hides_hidden_elements():
    css = CSS.read_text(encoding="utf-8")
    assert "[hidden]" in css and "display: none !important" in css


def test_no_raw_control_characters_in_frontend():
    for path in (HTML, JS, CSS):
        text = path.read_text(encoding="utf-8")
        bad = [hex(ord(ch)) for ch in text if ord(ch) < 32 and ch not in "\n\r\t"]
        assert not bad, f"{path.name} 含控制字符: {bad}"


def test_scene_options_and_mediakit_key_field():
    html = HTML.read_text(encoding="utf-8")
    js = JS.read_text(encoding="utf-8")
    for scene in ("general", "human", "product"):
        assert f'value="{scene}"' in html
    assert 'name="scene"' in html
    assert 'id="keyInput"' in html
    assert "akInput" not in js and "refine" not in js


def test_queue_arrow_key_navigation_contract():
    js = JS.read_text(encoding="utf-8")
    assert '"ArrowDown"' in js and '"ArrowUp"' in js
    assert "stepSelection(" in js
    assert "isEditableTarget(" in js
    assert "isSliderTarget(" in js
    assert 'closest(\'[role="slider"]\')' in js
    assert "els.settingsDialog.open" in js
