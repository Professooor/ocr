"""Templates render, use base.html, and load shared plus page-specific assets."""

import re

import pytest

from app import config

PAGES = {
    "/": ("upload", "css/upload.css", "js/upload.js"),
    "/review": ("review", "css/review.css", "js/review.js"),
    "/review/" + "0" * 32: ("review", "css/review.css", "js/review.js"),
}


@pytest.mark.parametrize("path", PAGES)
def test_page_renders_with_shared_and_page_assets(client, path):
    page_name, page_css, page_js = PAGES[path]
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text

    assert f'data-page="{page_name}"' in html
    assert 'class="app-header"' in html and 'class="app-footer"' in html

    stylesheets = re.findall(r'<link rel="stylesheet" href="([^"]+)"', html)
    scripts = re.findall(r'<script src="([^"]+)"', html)
    assert [s.split("?")[0] for s in stylesheets] == ["/static/css/base.css", f"/static/{page_css}"]
    assert [s.split("?")[0] for s in scripts] == ["/static/js/base.js", f"/static/{page_js}"]

    for asset in stylesheets + scripts:
        asset_response = client.get(asset)
        assert asset_response.status_code == 200, asset


def test_upload_page_dpi_input_uses_backend_rules(client):
    html = client.get("/").text
    dpi_input = re.search(r'<input[^>]*id="pdf-dpi"[^>]*>', html, re.S).group(0)
    assert 'min="50"' in dpi_input
    assert 'max="300"' in dpi_input
    assert 'value="75"' in dpi_input
    assert 'step="1"' in dpi_input
    file_input = re.search(r'<input[^>]*id="document-file"[^>]*>', html, re.S).group(0)
    assert "multiple" not in file_input


def test_dpi_slider_uses_backend_rules(client):
    html = client.get("/").text
    slider = re.search(r'<input[^>]*id="pdf-dpi-range"[^>]*>', html, re.S).group(0)
    assert 'type="range"' in slider
    assert 'min="50"' in slider and 'max="300"' in slider and 'value="75"' in slider


def test_frontend_is_vanilla_and_offline():
    """No frameworks or CDN resources: every asset is served by the application itself."""
    external = re.compile(r"(https?:)?//(?!www\.w3\.org/2000/svg)[a-z0-9.-]+\.[a-z]{2,}", re.I)
    frameworks = re.compile(r"\b(jquery|react|vue|angular|bootstrap|tailwind)\b", re.I)
    files = (
        list(config.TEMPLATES_DIR.glob("*.html"))
        + list(config.STATIC_DIR.rglob("*.css"))
        + list(config.STATIC_DIR.rglob("*.js"))
    )
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not external.search(text), f"External URL in {path.name}: {external.search(text).group(0)}"
        assert not frameworks.search(text), f"Framework reference in {path.name}"


def test_upload_page_has_no_size_limit(client):
    html = client.get("/").text
    assert " MB" not in html and "data-max-upload" not in html
    upload_js = (config.STATIC_DIR / "js" / "upload.js").read_text(encoding="utf-8")
    assert "maxBytes" not in upload_js


def test_export_buttons_present(client):
    for path in ("/", "/review/" + "0" * 32):
        html = client.get(path).text
        assert 'id="export-excel-button"' in html and "Export to Excel" in html
    base_js = (config.STATIC_DIR / "js" / "base.js").read_text(encoding="utf-8")
    assert "downloadFile" in base_js


def test_footer_shows_ocr_engine(client):
    html = client.get("/").text
    assert "PaddleOCR" in html and "app-footer__engine" in html


def test_review_page_escapes_document_id(client):
    response = client.get('/review/"><script>alert(1)</script>')
    assert response.status_code in (200, 404)
    assert "<script>alert(1)</script>" not in response.text


def test_every_page_template_extends_base():
    for template in config.TEMPLATES_DIR.glob("*.html"):
        if template.name == "base.html":
            continue
        assert template.read_text(encoding="utf-8").startswith('{% extends "base.html" %}'), template.name


def test_every_page_has_its_own_css_and_js():
    for template in config.TEMPLATES_DIR.glob("*.html"):
        if template.name == "base.html":
            continue
        name = template.stem
        assert (config.STATIC_DIR / "css" / f"{name}.css").is_file(), name
        assert (config.STATIC_DIR / "js" / f"{name}.js").is_file(), name


def test_base_js_has_no_page_specific_element_ids():
    base_js = (config.STATIC_DIR / "js" / "base.js").read_text(encoding="utf-8")
    assert "getElementById" not in base_js


def test_no_old_dpi_limit_in_frontend():
    for path in list(config.STATIC_DIR.rglob("*.js")) + list(config.TEMPLATES_DIR.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        assert 'max="75"' not in text and "maxDpi = 75" not in text, path.name
