"""DPI rules: minimum 50, maximum 300, default 75, whole numbers only."""

import pytest

from app import config
from app.errors import ServiceError
from app.services.document_validation_service import validate_dpi


def test_dpi_constants_match_requirements():
    assert config.PDF_DPI_MIN == 50
    assert config.PDF_DPI_MAX == 300
    assert config.PDF_DPI_DEFAULT == 75


@pytest.mark.parametrize("value", [50, 51, 60, 75, 100, 150, 199, 200, 250, 299, 300])
def test_valid_integer_dpi(value):
    assert validate_dpi(value) == value


@pytest.mark.parametrize("value", ["50", "75", "150", " 200 ", "300", "075"])
def test_valid_string_dpi(value):
    assert validate_dpi(value) == int(value.strip())


def test_missing_dpi_uses_default():
    assert validate_dpi(None) == 75


def test_missing_dpi_rejected_when_required():
    with pytest.raises(ServiceError) as error:
        validate_dpi(None, allow_missing=False)
    assert error.value.code == "INVALID_DPI"


@pytest.mark.parametrize(
    "value",
    [49, 0, -1, 301, 600, 1000, "49", "301", "", "   ", "abc", "75.5", "75.0", "1e2", "+75", "-75",
     "75 dpi", 75.0, 150.5, True, False, [75], {"dpi": 75}],
)
def test_invalid_dpi_rejected(value):
    with pytest.raises(ServiceError) as error:
        validate_dpi(value)
    assert error.value.code == "INVALID_DPI"
    assert error.value.status_code == 422
