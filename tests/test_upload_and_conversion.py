"""Document upload, validation and automatic PDF page conversion."""

import io
import math

import fitz
import pytest
from PIL import Image

from app import config
from tests.conftest import make_image, make_pdf, stored_document_count, upload

# Three pages with different widths so the page order can be verified from the image sizes.
PAGE_SIZES = ((300, 400), (400, 500), (500, 600))


def expected_pixels(points, dpi):
    return math.ceil(points * dpi / 72)


@pytest.mark.parametrize("dpi", [50, 75, 150, 200, 250, 300])
def test_pdf_pages_converted_with_selected_dpi(client, dpi):
    response = upload(client, "multi_page.pdf", make_pdf(PAGE_SIZES), dpi=dpi)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["success"] is True
    document = body["data"]
    assert document["page_count"] == 3
    assert document["conversion"]["dpi"] == dpi
    assert document["conversion"]["status"] == "completed"
    assert document["document_kind"] == "pdf"

    pages = client.get(f"/api/documents/{document['document_id']}/pages").json()["data"]
    assert pages["dpi"] == dpi
    assert [page["page_number"] for page in pages["pages"]] == [1, 2, 3]

    for page, (width_pt, height_pt) in zip(pages["pages"], PAGE_SIZES):
        assert page["dpi"] == dpi
        assert abs(page["width"] - expected_pixels(width_pt, dpi)) <= 1
        assert abs(page["height"] - expected_pixels(height_pt, dpi)) <= 1

        image_response = client.get(page["image_url"])
        assert image_response.status_code == 200
        assert image_response.headers["content-type"] == "image/png"
        with Image.open(io.BytesIO(image_response.content)) as image:
            assert image.size == (page["width"], page["height"])


def test_dpi_defaults_to_75_when_omitted(client):
    response = upload(client, "default_dpi.pdf", make_pdf())
    assert response.status_code == 201, response.text
    assert response.json()["data"]["conversion"]["dpi"] == 75


@pytest.mark.parametrize("dpi", ["49", "301", "0", "abc", "75.5", "", "1e2", "-75"])
def test_invalid_dpi_rejected_and_nothing_stored(client, dpi):
    before = stored_document_count()
    response = client.post(
        "/api/documents/upload",
        files={"file": ("bad_dpi.pdf", make_pdf(), "application/pdf")},
        data={"dpi": dpi},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "INVALID_DPI"
    assert "50" in body["error"]["message"] and "300" in body["error"]["message"]
    assert stored_document_count() == before


def test_unsupported_extension_rejected(client):
    response = upload(client, "notes.txt", b"plain text")
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"


def test_content_must_match_extension(client):
    response = upload(client, "fake.pdf", make_image("PNG"))
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "FILE_TYPE_MISMATCH"


def test_unknown_content_rejected(client):
    response = upload(client, "random.png", b"this is not an image at all")
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "INVALID_FILE_CONTENT"


def test_corrupted_pdf_rejected(client):
    before = stored_document_count()
    response = upload(client, "broken.pdf", b"%PDF-1.7\n" + b"\x00garbage" * 200)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CORRUPTED_PDF"
    assert stored_document_count() == before


def test_corrupted_image_rejected(client):
    response = upload(client, "broken.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CORRUPTED_IMAGE"


def test_password_protected_pdf_rejected(client):
    document = fitz.open()
    document.new_page()
    data = document.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="secret")
    document.close()
    response = upload(client, "protected.pdf", data)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PASSWORD_PROTECTED_PDF"


def test_empty_file_rejected(client):
    response = upload(client, "empty.pdf", b"")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "EMPTY_FILE"


def test_missing_file_rejected(client):
    response = client.post("/api/documents/upload", data={"dpi": "75"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "NO_FILE"


def test_multiple_files_rejected(client):
    pdf = make_pdf()
    response = client.post(
        "/api/documents/upload",
        files=[("file", ("one.pdf", pdf, "application/pdf")), ("file", ("two.pdf", pdf, "application/pdf"))],
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "MULTIPLE_FILES"


def test_no_file_size_limit(client):
    # 60 MB: larger than the old 50 MB limit. Data after %%EOF is ignored by PDF readers.
    large_pdf = make_pdf() + b"\n%" + b"0" * (60 * 1024 * 1024)
    response = upload(client, "large.pdf", large_pdf, dpi=50)
    assert response.status_code == 201, response.text[:300]
    document = response.json()["data"]
    assert document["file_size_bytes"] == len(large_pdf)
    assert document["page_count"] == 1


def test_no_page_count_limit(client):
    response = upload(client, "many_pages.pdf", make_pdf(((100, 100),) * 400), dpi=50)
    assert response.status_code == 201, response.text[:300]
    assert response.json()["data"]["page_count"] == 400


def test_page_too_large_for_dpi_is_rejected_and_cleaned_up(client):
    before = stored_document_count()
    response = upload(client, "poster.pdf", make_pdf(((5000, 5000),)), dpi=300)
    assert response.status_code == 413
    body = response.json()
    assert body["error"]["code"] == "PAGE_TOO_LARGE"
    assert "DPI or lower" in body["error"]["message"]
    assert stored_document_count() == before


def test_unsafe_filename_is_only_kept_as_display_name(client):
    response = upload(client, "..\\..\\windows\\evil<name>.pdf", make_pdf())
    assert response.status_code == 201, response.text
    assert response.json()["data"]["original_filename"] == "evilname.pdf"
    response = upload(client, "../../etc/passwd.pdf", make_pdf())
    assert response.status_code == 201, response.text
    document_id = response.json()["data"]["document_id"]
    assert response.json()["data"]["original_filename"] == "passwd.pdf"
    original = config.DOCUMENTS_DIR / document_id / "original" / "original.pdf"
    assert original.is_file()


def test_png_upload_creates_one_page_at_native_resolution(client):
    response = upload(client, "scan.png", make_image("PNG", size=(640, 480)))
    assert response.status_code == 201, response.text
    document = response.json()["data"]
    assert document["document_kind"] == "image"
    assert document["page_count"] == 1
    assert document["conversion"]["dpi"] is None
    page = client.get(f"/api/documents/{document['document_id']}/pages").json()["data"]["pages"][0]
    assert (page["width"], page["height"]) == (640, 480)


@pytest.mark.parametrize("filename,image_format", [("photo.jpg", "JPEG"), ("photo.jpeg", "JPEG"), ("scan.bmp", "BMP")])
def test_other_image_formats(client, filename, image_format):
    response = upload(client, filename, make_image(image_format))
    assert response.status_code == 201, response.text
    assert response.json()["data"]["page_count"] == 1


def test_transparent_png_converted(client):
    response = upload(client, "transparent.png", make_image("PNG", mode="RGBA"))
    assert response.status_code == 201, response.text


def test_multi_frame_tiff_creates_one_page_per_frame(client):
    response = upload(client, "scan.tiff", make_image("TIFF", frames=3))
    assert response.status_code == 201, response.text
    assert response.json()["data"]["page_count"] == 3


def test_documents_do_not_overwrite_each_other(client):
    first = upload(client, "same_name.pdf", make_pdf(((300, 300),)), dpi=75).json()["data"]
    second = upload(client, "same_name.pdf", make_pdf(((600, 600),)), dpi=75).json()["data"]
    assert first["document_id"] != second["document_id"]
    first_page = client.get(f"/api/documents/{first['document_id']}/pages").json()["data"]["pages"][0]
    second_page = client.get(f"/api/documents/{second['document_id']}/pages").json()["data"]["pages"][0]
    assert first_page["width"] != second_page["width"]
