"""
Find invoice fields and line items in OCR results.

Input: OCR results of the pages (text lines with confidence and bounding box).
Output: values for vendor name, invoice number, invoice date, due date,
PO number, currency, subtotal, tax, total due, and the line items table.

How it works (plain rules, no machine learning):
- A field is found by its label ("Invoice Number", "Due Date", "Total", ...).
- The value is taken from the same line after the label, from the line to the
  right of the label, or from the line directly below it, using the box
  positions of the OCR lines.
- Line items are read from the table that follows a header row such as
  "Description / Qty / Rate / Amount"; each cell is assigned to a column by
  its horizontal position.

OCR and these rules can both be wrong, so every result carries its source line
and confidence, and the user can correct everything on the review screen.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from app import config

LOW_CONFIDENCE = 0.8
MAX_LINE_ITEMS = 500
# Raise when the rules change, so stored documents are read again (values the user saved are kept).
RULES_VERSION = 7

# Header fields, in the order of the review form: supplier, buyer, invoice references, amounts.
FIELD_KEYS = (
    "vendor_name",
    "supplier_ntn",
    "supplier_strn",
    "supplier_address",
    "buyer_name",
    "buyer_ntn",
    "buyer_strn",
    "buyer_address",
    "invoice_number",
    "invoice_date",
    "due_date",
    "fbr_invoice_number",
    "po_number",
    "dc_number",
    "gate_pass_number",
    "contract_number",
    "currency",
    "subtotal",
    "discount",
    "sales_tax_rate",
    "tax",
    "further_tax",
    "total_due",
    "total_quantity",
    "amount_in_words",
)

# Columns of one line item. "amount" is the value before tax.
LINE_ITEM_KEYS = (
    "description",
    "hs_code",
    "uom",
    "quantity",
    "rate",
    "amount",
    "tax_rate",
    "sales_tax",
    "further_tax",
    "discount",
    "amount_incl_tax",
)
LINE_TEXT_KEYS = ("description", "hs_code", "uom")
LINE_NUMBER_KEYS = tuple(key for key in LINE_ITEM_KEYS if key not in LINE_TEXT_KEYS)

# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
MONTH_NAMES = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
MONTH_NUMBERS = {name: number for number, name in enumerate(MONTH_NAMES.split("|"), start=1)}

ISO_DATE = re.compile(r"(?<!\d)(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})(?!\d)")
NUMERIC_DATE = re.compile(r"(?<!\d)(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4}|\d{2})(?!\d)")
DAY_MONTH_TEXT = re.compile(
    r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?[\s\-/.,]*(" + MONTH_NAMES + r")[a-z]*\.?[\s\-/.,]*(\d{4}|\d{2})(?!\d)",
    re.IGNORECASE,
)
MONTH_DAY_TEXT = re.compile(
    r"(?<![A-Za-z])(" + MONTH_NAMES + r")[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?[,.]?\s+(\d{4})(?!\d)",
    re.IGNORECASE,
)


def make_iso_date(year, month, day):
    """Return the date as YYYY-MM-DD, or None if it is not a real date (or the year is not between 1990 and 2099)."""
    if not 1990 <= year <= 2099:
        return None  # "12-06-5205-009-64" is a tax registration number, not a date
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_dates(text, order):
    """Return the dates in text as [{"iso", "ambiguous", "start", "end"}], left to right."""
    found = []
    for match in ISO_DATE.finditer(text):
        iso = make_iso_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if iso:
            found.append({"iso": iso, "ambiguous": False, "start": match.start(), "end": match.end()})

    for match in NUMERIC_DATE.finditer(text):
        first, second, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if year < 100:
            year += 2000
        ambiguous = False
        if first > 12:
            day, month = first, second
        elif second > 12:
            month, day = first, second
        else:
            ambiguous = first != second
            day, month = (second, first) if order == "mdy" else (first, second)
        iso = make_iso_date(year, month, day)
        if iso:
            found.append({"iso": iso, "ambiguous": ambiguous, "start": match.start(), "end": match.end()})

    for match in DAY_MONTH_TEXT.finditer(text):
        year = int(match.group(3))
        year += 2000 if year < 100 else 0
        iso = make_iso_date(year, MONTH_NUMBERS[match.group(2).lower()], int(match.group(1)))
        if iso:
            found.append({"iso": iso, "ambiguous": False, "start": match.start(), "end": match.end()})

    for match in MONTH_DAY_TEXT.finditer(text):
        iso = make_iso_date(int(match.group(3)), MONTH_NUMBERS[match.group(1).lower()], int(match.group(2)))
        if iso:
            found.append({"iso": iso, "ambiguous": False, "start": match.start(), "end": match.end()})

    found.sort(key=lambda item: (item["start"], -(item["end"] - item["start"])))
    separate = []
    for item in found:
        if not separate or item["start"] >= separate[-1]["end"]:
            separate.append(item)
    return separate


def mask_dates(text, order):
    """Replace dates by spaces so their digits are not mistaken for numbers."""
    masked = text
    for item in parse_dates(text, order):
        masked = masked[: item["start"]] + " " * (item["end"] - item["start"]) + masked[item["end"]:]
    return masked


def detect_date_order(texts):
    """Use an unambiguous date (for example 25/12/2024) to learn the order used."""
    for text in texts:
        for match in NUMERIC_DATE.finditer(text):
            first, second = int(match.group(1)), int(match.group(2))
            if first > 12 and second <= 12:
                return "dmy"
            if second > 12 and first <= 12:
                return "mdy"
    return config.INVOICE_DATE_ORDER


# ---------------------------------------------------------------------------
# Amounts
# ---------------------------------------------------------------------------
NUMBER_TOKEN = re.compile(r"\d[\d.,]*\d|\d")
NEGATIVE_PREFIX = re.compile(r"[-−]\s*(?:[$€£¥₹]|Rs\.?)?\s*$")


def parse_number_token(token):
    """Read 1,234.56 / 1.234,56 / 1234,5 and similar into a Decimal."""
    commas, dots = token.count(","), token.count(".")
    if commas and dots:
        decimal_separator = "," if token.rfind(",") > token.rfind(".") else "."
        thousands_separator = "." if decimal_separator == "," else ","
        cleaned = token.replace(thousands_separator, "").replace(decimal_separator, ".")
    elif commas:
        parts = token.split(",")
        if len(parts) == 2 and len(parts[1]) != 3:
            cleaned = parts[0] + "." + parts[1]
        elif len(parts) > 2 and len(parts[-1]) == 2:
            # OCR often reads 334,554.00 as 334,554,00: the last comma is the decimal point.
            cleaned = "".join(parts[:-1]) + "." + parts[-1]
        else:
            cleaned = "".join(parts)
    elif dots > 1:
        parts = token.split(".")
        if len(parts[-1]) == 2:
            # OCR often reads 89,747.84 as 89.747.84: the last dot is the decimal point.
            cleaned = "".join(parts[:-1]) + "." + parts[-1]
        else:
            cleaned = token.replace(".", "")
    else:
        cleaned = token
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def format_amount(value):
    """Money as text: 2 decimals normally, up to 4 when the invoice shows more."""
    two_places = value.quantize(Decimal("0.01"))
    if value == two_places:
        return f"{two_places:.2f}"
    return format(value.normalize(), "f")


def format_quantity(value):
    """Write a quantity as plain text without an exponent or trailing zeros."""
    return format(value.normalize(), "f")


def find_amounts(text, order):
    """Return the numbers in text as Decimals, left to right (percentages and dates are ignored)."""
    masked = mask_dates(text, order)
    amounts = []
    for match in NUMBER_TOKEN.finditer(masked):
        start, end = match.span()
        before = masked[start - 1] if start > 0 else " "
        after = masked[end] if end < len(masked) else " "
        if before.isalnum() or before in "_/":
            continue
        if before in "-−" and start >= 2 and masked[start - 2].isalnum():
            continue  # part of a code such as IVN-10012
        if after.isalnum() or masked[end:].lstrip().startswith("%"):
            continue
        token = match.group()
        if re.fullmatch(r"0\d+", token) or len(re.sub(r"\D", "", token)) > 12:
            continue  # 0698478 or a 13-digit number is a registration number or code, not an amount
        value = parse_number_token(token)
        if value is None:
            continue
        if NEGATIVE_PREFIX.search(masked[:start]):
            value = -value
        amounts.append(value)
    return amounts


CURRENCY_CODES = ("USD", "EUR", "GBP", "PKR", "AED", "SAR", "INR", "CAD", "AUD", "JPY", "CNY", "CHF")
CURRENCY_SYMBOLS = (("₹", "INR"), ("€", "EUR"), ("£", "GBP"), ("Rs", "PKR"), ("$", "USD"))


def detect_currency(texts):
    """Guess the currency code (for example USD) from the currency words and symbols in the text."""
    joined = "\n".join(texts)
    counts = {code: len(re.findall(r"\b" + code + r"\b", joined)) for code in CURRENCY_CODES}
    best = max(counts, key=counts.get)
    if counts[best] > 0:
        return best
    if re.search(r"(?<![A-Za-z])Rs(?![A-Za-z])", joined):
        return "PKR"
    # Pakistani tax invoices: NTN / STRN numbers and the FBR digital invoice mark. Checked before
    # $ and £, which OCR sometimes reads from a stray mark.
    if re.search(r"\b(?:N\.?T\.?N|STRN|S\.?T\.?\s*Reg|FBR|Rupees)\b", joined, re.IGNORECASE):
        return "PKR"
    for symbol, code in CURRENCY_SYMBOLS:
        if symbol != "Rs" and re.search(r"(?<![A-Za-z])" + re.escape(symbol), joined):
            return code
    return ""


PERCENT = re.compile(r"(?<![\d.])(\d{1,3}(?:[.,]\d{1,2})?)\s*%")


# ---------------------------------------------------------------------------
# Reading the value of one field from a piece of text
# ---------------------------------------------------------------------------
def parse_value(kind, text, order):
    """Return {"value", "ambiguous"} read from text for a field kind, or None."""
    if kind == "date":
        dates = parse_dates(text, order)
        if dates:
            return {"value": dates[0]["iso"], "ambiguous": dates[0]["ambiguous"]}
        return None

    if kind == "amount":
        amounts = find_amounts(text, order)
        return {"value": format_amount(amounts[-1]), "ambiguous": False} if amounts else None

    if kind == "quantity":
        amounts = find_amounts(text, order)
        return {"value": format_quantity(amounts[-1]), "ambiguous": False} if amounts else None

    if kind == "percent":
        match = PERCENT.search(text)
        if match:
            value = parse_number_token(match.group(1))
            if value is not None and 0 <= value <= 100:
                return {"value": format_quantity(value), "ambiguous": False}
        return None

    if kind == "code":
        masked = mask_dates(text, order)
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_\-/.#]*", masked):
            token = token.strip(".-/#_")
            if len(token) >= 2 and len(re.findall(r"\d", token)) >= 2:
                return {"value": token, "ambiguous": False}
        return None

    if kind == "text":
        cleaned = re.sub(r"\s+", " ", text).strip(" :-")
        if len(re.findall(r"[A-Za-z]", cleaned)) >= 3:
            return {"value": cleaned, "ambiguous": False}
        return None

    return None


# ---------------------------------------------------------------------------
# Field definitions: labels that introduce each value
# ---------------------------------------------------------------------------
# "below": the value may be on the line under the label.
# "pick": if several lines match a label, use the "first" or the "last".
# "reject": a label line matching this is skipped (for the whole field, or for one group).
FBR_INVOICE = r"(?:digital|fbr|feir|e-)\s*inv"
FIELD_DEFS = [
    {
        "key": "invoice_number",
        "kind": "code",
        "groups": [
            {
                # The supplier's own invoice number comes first ...
                "patterns": [
                    r"inv[a-z]{1,3}ce\s*(?:number|no\b[.,]?|num\b\.?|#|id\b)",  # also OCR "Invaice", "Involce No,"
                    r"\binv\.?\s*(?:number|no\b\.?|#)",
                    r"inv[a-z]{1,3}ce\s*[a-z]?\s*[:#]",
                    r"bill\s*(?:number|no\b\.?|#)",
                    r"document\s*(?:number|no\b\.?|#)",
                ],
                "reject": FBR_INVOICE,
                "below": True,
                "pick": "first",
            },
            {
                # A heading cell "Invoice" with the number to its right.
                "patterns": [r"^\W*invoice\W*$"],
                "below": False,
                "pick": "first",
            },
            {
                # ... the FBR digital invoice number only when there is no other.
                "patterns": [FBR_INVOICE + r"\w*\s*(?:number|no\b\.?|#|:)"],
                "below": True,
                "pick": "first",
            },
        ],
    },
    {
        "key": "due_date",
        "kind": "date",
        "groups": [{
            "patterns": [
                r"due\s*date",
                r"date\s*due",
                r"payment\s*due(?:\s*date)?",
                r"pay\s*(?:by|before)",
                r"due\s*(?:on|by)\b",
            ],
            "below": True,
            "pick": "first",
        }],
    },
    {
        "key": "invoice_date",
        "kind": "date",
        "groups": [
            {
                "patterns": [
                    r"invoice\s*date",
                    r"date\s*(?:of\s*)?issue[d]?",
                    r"issue[d]?\s*date",
                    r"date\s*of\s*invoice",
                    r"invoice\s*dated",
                    r"bill(?:ing)?\s*date",
                ],
                "below": True,
                "pick": "first",
            },
            {
                "patterns": [r"(?<![A-Za-z])(?<!due\s)(?<!due:\s)(?<!printed\s)date[d]?(?![A-Za-z])(?!\s*due)"],
                "below": True,
                "pick": "first",
            },
        ],
    },
    {
        "key": "po_number",
        "kind": "code",
        "reject": r"p\.?\s?o\.?\s*box",
        "groups": [
            {
                "patterns": [
                    r"p\.?\s?o[.,]?\s*(?:number|no\b\.?|num\b\.?|#)",
                    r"purchase\s*order\s*(?:number|no\b\.?|num\b\.?|#)?",
                    r"\bP\.?O\.?\s*[:#]",
                    r"\bp\s*/\s*o\b",
                    r"\bpo\s*ref",
                ],
                "below": True,
                "pick": "first",
            },
            {
                "patterns": [r"contract\s*(?:number|no\b\.?|#)"],
                "below": True,
                "pick": "first",
            },
        ],
    },
    {
        "key": "subtotal",
        "kind": "amount",
        "groups": [{
            "patterns": [
                r"sub[\s\-]?total",
                r"(?:amount|value)\s*excl",
                r"excl(?:uding|usive)?\.?\s*(?:of\s*)?(?:gst|tax|s\.?\s*tax|sales)",
                r"taxable\s*(?:value|amount)",
                r"retail\s*value",
            ],
            "below": False,
            "pick": "last",
        }],
    },
    {
        "key": "tax",
        "kind": "amount",
        "reject": r"further|withholding|income\s*tax|rate\s*of|tax\s*(?:id|no\b|number|#|reg)|registration",
        "groups": [
            {
                "patterns": [
                    r"sales\s*tax(?!\s*(?:invoice|reg|no\b|number|#))",
                    r"(?<![A-Za-z])(?:gst|vat|hst|s\.\s*tax)(?![A-Za-z])(?!\s*[.#:]?\s*(?:no\b|number|#|reg|inv|\d{5,}))",
                ],
                "below": False,
                "pick": "last",
            },
            {
                "patterns": [r"(?<![A-Za-z])tax(?![A-Za-z])(?!\s*(?:invoice|value|%))"],
                "below": False,
                "pick": "last",
            },
        ],
    },
    {
        "key": "total_due",
        "kind": "amount",
        "groups": [
            {
                "patterns": [
                    r"total\s*due",
                    r"amount\s*due",
                    r"balance\s*due",
                    r"amount\s*payable",
                    r"total\s*payable",
                    r"net\s*(?:amount\s*)?due",
                    r"due\s*amount",
                ],
                "below": True,
                "pick": "first",
            },
            {
                "patterns": [
                    r"grand\s*total",
                    r"total\s*amount",
                    r"inv\w{0,5}\s*total",
                    r"net\s*(?:tax\s*)?inclusive",
                    r"total\s*(?:value\s*)?incl",
                ],
                "below": False,
                "pick": "first",
            },
            {
                "patterns": [r"(?<![A-Za-z])(?<!sub-)(?<!sub )t[o0ue]t[ae@][l1i](?![A-Za-z])(?!\s*(?:quantity|qty|pcs|weight))"],
                "below": False,
                "pick": "last",
            },
        ],
    },
    {
        "key": "vendor_name",
        "kind": "text",
        "groups": [{
            "patterns": [
                r"^(?:vendor|supplier|seller|payee|sold\s*by|from|bill(?:ed)?\s*from|remit\s*to)(?:'?s)?\b"
                r"\s*(?:name|details?|info(?:rmation)?|particulars)?\s*[:\-]?"
            ],
            "below": True,
            "pick": "first",
        }],
    },
]

# Lines that belong to the buyer, never to the supplier.
BUYER_LABEL = re.compile(
    # "M/S" on its own is a buyer label; "M/s Apex Ltd" is just a name written with "Messrs".
    r"^\W*(?:buyer|customer|custo\w*|party|bill(?:ed)?\s*to|ship(?:ped)?\s*to|(?:sold|sale|sales)\s*to|deliver\w*\s*to|"
    r"consignee|(?:m/s|messrs)\W*$)",
    re.IGNORECASE,
)
# Words that are a label or section name, not a value.
GENERIC_WORDS = re.compile(
    r"^\W*(?:'?s\s*)?(?:name|details?|info(?:rmation)?|particulars|address|ph(?:one)?\s*no|contact)\W*$", re.IGNORECASE
)
# "Name:" in front of a value (OCR may read it as "Nime:" or "Narne:").
NAME_PREFIX = re.compile(r"^\W*n[a-z ]{1,4}e\s*[:\-]\s*", re.IGNORECASE)

for _definition in FIELD_DEFS:
    for _group in _definition["groups"]:
        _group["regexes"] = [re.compile(pattern, re.IGNORECASE) for pattern in _group["patterns"]]
        _group["reject_regex"] = re.compile(_group["reject"], re.IGNORECASE) if "reject" in _group else None
    _definition["reject_regex"] = re.compile(_definition["reject"], re.IGNORECASE) if "reject" in _definition else None

ALL_LABEL_REGEXES = [regex for _d in FIELD_DEFS for _g in _d["groups"] for regex in _g["regexes"]]
INVOICE_TITLE = re.compile(r"^\W*(?:tax\s+)?invoice\W*$", re.IGNORECASE)


def is_label_text(text):
    """True when the text looks like a field label (for example "Invoice No")."""
    return any(regex.search(text) for regex in ALL_LABEL_REGEXES)


# ---------------------------------------------------------------------------
# Lines and geometry
# ---------------------------------------------------------------------------
def collect_lines(page_results):
    """Flatten OCR results into one list of lines with box positions, in reading order."""
    lines = []
    for result in page_results:
        page_lines = []
        for index, line in enumerate(result.get("lines") or []):
            text = str(line.get("text") or "").strip()
            box = line.get("box")
            if not text or not box or len(box) != 4:
                continue
            left, top, right, bottom = [float(value) for value in box]
            page_lines.append({
                "page": result.get("page_number"),
                "index": index,
                "text": text,
                "confidence": line.get("confidence"),
                "l": left, "t": top, "r": right, "b": bottom,
                "h": max(bottom - top, 1.0),
                "xc": (left + right) / 2,
                "yc": (top + bottom) / 2,
            })
        page_width = result.get("image_width") or max((l["r"] for l in page_lines), default=1)
        page_height = result.get("image_height") or max((l["b"] for l in page_lines), default=1)
        if is_turned_page(page_lines):
            page_width, page_height = turn_upright(page_lines, float(page_width), float(page_height))
        for line in page_lines:
            line["page_width"], line["page_height"] = float(page_width), float(page_height)
        lines.extend(page_lines)
    return lines


def is_turned_page(page_lines):
    """A landscape invoice scanned standing up: most text boxes are tall and narrow."""
    words = [line for line in page_lines if len(line["text"]) >= 4]
    if len(words) < 8:
        return False
    tall = sum(1 for line in words if (line["b"] - line["t"]) > 1.5 * (line["r"] - line["l"]))
    return tall > 0.6 * len(words)


def turn_upright(page_lines, width, height):
    """
    Turn the box positions of a page scanned 90 degrees clockwise back upright. OCR read the
    text top to bottom, so the original top of the page is at the right edge. Returns the new size.
    """
    for line in page_lines:
        left, top, right, bottom = line["l"], line["t"], line["r"], line["b"]
        line["l"], line["r"] = top, bottom
        line["t"], line["b"] = width - right, width - left
        line["h"] = max(line["b"] - line["t"], 1.0)
        line["xc"] = (line["l"] + line["r"]) / 2
        line["yc"] = (line["t"] + line["b"]) / 2
    return height, width


def right_neighbors(label, page_lines):
    """Lines on the same row to the right of the label, nearest first."""
    found = []
    for other in page_lines:
        if other is label:
            continue
        same_row = abs(other["yc"] - label["yc"]) <= 0.6 * max(label["h"], other["h"])
        # OCR boxes of a label and its value can overlap a little ("Invoice #" | "242").
        if same_row and (other["l"] >= label["r"] - 0.5 * label["h"] or other["xc"] > label["r"]):
            found.append(other)
    found.sort(key=lambda other: other["l"])
    return found


def below_neighbors(label, page_lines):
    """Lines directly under the label (overlapping horizontally), nearest first."""
    found = []
    for other in page_lines:
        gap = other["t"] - label["b"]
        if other is label or gap < -0.3 * label["h"] or gap > 2.2 * max(label["h"], other["h"]):
            continue
        overlap = min(label["r"], other["r"]) - max(label["l"], other["l"])
        narrowest = min(label["r"] - label["l"], other["r"] - other["l"])
        if overlap > 0.25 * narrowest:
            found.append(other)
    found.sort(key=lambda other: other["t"])
    return found


def first_match(regexes, text):
    """Return the first regular expression match found in the text, or None."""
    for regex in regexes:
        match = regex.search(text)
        if match:
            return match
    return None


OTHER_LABEL_LINE = re.compile(
    r"^\W*(?:address|addr\w*|ntn|n\.t\.n|strn|gst|phone|ph|tel|cell|mobile|email|e-mail|fax|province|city)\b",
    re.IGNORECASE,
)


def read_text_value(kind, text, order):
    """parse_value, plus: a name is never a label word, and a "Name:" prefix is removed."""
    if kind != "text":
        return parse_value(kind, text, order)
    text = re.sub(r"^\W*(?:m/s|messrs)\.?\s+", "", NAME_PREFIX.sub("", text), flags=re.IGNORECASE)
    if GENERIC_WORDS.match(text) or BUYER_LABEL.match(text) or OTHER_LABEL_LINE.match(text) or is_label_text(text):
        return None
    if text.rstrip().endswith(":"):
        return None  # "Payment Terms:" and other labels
    return parse_value(kind, text, order)


def rightmost_amount(label_line, page_lines, order):
    """The last amount on the label's row (a total sits at the right edge, after rates and quantities)."""
    found = None
    for other in right_neighbors(label_line, page_lines):
        if is_label_text(other["text"]) and not find_amounts(other["text"], order):
            break  # The next label starts another value.
        amounts = find_amounts(other["text"], order)
        if amounts:
            found = {"value": format_amount(amounts[-1]), "ambiguous": False, "line": other}
    return found


def value_for_label(kind, label_line, match, page_lines, order, allow_below):
    """Value of a field whose label was found in label_line."""
    remainder = label_line["text"][match.end():].lstrip(" :.-#\t")
    if kind == "amount":
        found = rightmost_amount(label_line, page_lines, order)
        if found:
            return found
    value = read_text_value(kind, remainder, order)
    if value is not None:
        return dict(value, line=label_line)

    neighbors = right_neighbors(label_line, page_lines)[:3]
    if kind == "text" and re.search(r"particulars|details?|info", label_line["text"], re.IGNORECASE):
        neighbors = []  # a section heading ("SELLER'S PARTICULARS"): the name is under it, not beside it
    if allow_below:
        neighbors += below_neighbors(label_line, page_lines)[:3]
    for other in neighbors:
        if kind == "text" and NAME_PREFIX.match(other["text"]) and not NAME_PREFIX.sub("", other["text"]).strip():
            # A "Name :" line on its own: the name is to its right.
            for name_line in right_neighbors(other, page_lines)[:1]:
                value = read_text_value(kind, name_line["text"], order)
                if value is not None:
                    return dict(value, line=name_line)
            continue
        if is_label_text(other["text"]) or BUYER_LABEL.match(other["text"]):
            continue
        value = read_text_value(kind, other["text"], order)
        if value is not None:
            return dict(value, line=other)
    return None


def find_field(definition, lines, lines_by_page, order):
    """Search the lines for one field (using its label patterns) and return the best candidate with its source line."""
    for group in definition["groups"]:
        candidates = []
        for line in lines:
            match = first_match(group["regexes"], line["text"])
            if not match:
                continue
            if definition["reject_regex"] and definition["reject_regex"].search(line["text"]):
                continue
            if group["reject_regex"] and group["reject_regex"].search(line["text"]):
                continue
            candidate = value_for_label(
                definition["kind"], line, match, lines_by_page[line["page"]], order, group["below"]
            )
            if candidate:
                candidates.append(candidate)
        if candidates:
            return candidates[-1] if group["pick"] == "last" else candidates[0]
    return None


def make_field(candidate, force_check=False):
    """Field record returned to the caller."""
    if candidate is None:
        return {"value": "", "confidence": None, "needs_check": False, "source": None}
    line = candidate["line"]
    confidence = line.get("confidence")
    low = confidence is not None and confidence < LOW_CONFIDENCE
    return {
        "value": candidate["value"],
        "confidence": confidence,
        "needs_check": bool(force_check or low or candidate.get("ambiguous")),
        "source": {"page": line["page"], "line": line["index"]},
    }


# ---------------------------------------------------------------------------
# Fallbacks when no label is found
# ---------------------------------------------------------------------------
INVOICE_CODE = re.compile(r"\b(?:INV|IVN|INVOICE)[-_/ ]?\d[\w\-/]*", re.IGNORECASE)


def fallback_invoice_number(lines):
    """Last attempt to find an invoice number: look for anything that looks like an invoice code."""
    for line in lines:
        match = INVOICE_CODE.search(line["text"])
        if match:
            token = match.group().replace(" ", "")
            return {"value": token, "line": line, "ambiguous": False}
    return None


def fallback_invoice_date(lines, order, exclude_iso):
    """Last attempt to find the invoice date: the first date that is not the excluded one."""
    for line in lines:
        for item in parse_dates(line["text"], order):
            if item["iso"] != exclude_iso:
                return {"value": item["iso"], "line": line, "ambiguous": item["ambiguous"]}
    return None


# Titles, stamps and badges printed near the top that are not the supplier's name.
NOT_A_NAME = re.compile(
    r"\binvoice\b|\binvo\w*\b|\binv[ao]i?ce\b|\bdigit\w*|\bfbr\b|\bpral\b|invoicing|\bsystem\b|\boriginal\b|"
    r"\bduplicate\b|\bcopy\b|\bpage\b|\bundertaking\b|\bdeclaration\b|\bchallan\b|\bdelivery\b|\breceipt\b|"
    r"\bquotation\b",
    re.IGNORECASE,
)
# A line that is only a place ("PUNJAB", "Lahore - Pakistan"); "Barcode Pakistan" is a company.
PLACE_ONLY = re.compile(
    r"^\W*(?:(?:pakistan|punjab|sindh|lahore|karachi|faisalabad|islamabad|sheikhupura|kasur|sialkot|multan|"
    r"gujranwala|rawalpindi|peshawar|quetta|pk)\W*)+$",
    re.IGNORECASE,
)
ADDRESS_WORDS = re.compile(
    r"\b(?:road|street|st\.|floor|plot|block|town|colony|near|market|plaza|palaza|bazar|bazaar|chowk|suite|"
    r"shop|office|building|tower|centre|center|sector|phase|estate|area|mohalla|district|tehsil)\b",
    re.IGNORECASE,
)
COMPANY_WORDS = re.compile(
    r"\b(?:ltd|limited|pvt|private|inc|llc|co|company|corporation|corp|traders?|trading|enterprises?|sons|"
    r"brothers|bros|industries|industry|mills?|works|printers?|printing|press|packages|packaging|graphics|"
    r"services|technologies|technology|tech|chemicals?|engineering|computers?|stores?|agency|associates|"
    r"group|international|solutions|textiles?|plastics?|suppliers?|electric\w*|point|house|mart|center)\b",
    re.IGNORECASE,
)


def looks_like_company_name(line, order):
    """True when the line could be a company name (mostly letters, not an address or a label)."""
    text = NAME_PREFIX.sub("", line["text"])
    letters = len(re.findall(r"[A-Za-z]", text))
    digits = len(re.findall(r"\d", text))
    if letters < 3 or digits > 0.3 * len(text):
        return False
    if INVOICE_TITLE.match(text) or is_label_text(text) or BUYER_LABEL.match(text) or GENERIC_WORDS.match(text):
        return False
    if text.rstrip().endswith(":") or re.search(r"\w\s*:\s*\S", text):
        return False  # "Address: ...", "NTN: ..." and other label lines
    if (NOT_A_NAME.search(text) and not COMPANY_WORDS.search(text)) or PLACE_ONLY.match(text):
        return False
    if ADDRESS_WORDS.search(text) and not COMPANY_WORDS.search(text):
        return False
    if re.search(r"@|www\.|https?:", text, re.IGNORECASE):
        return False
    if parse_dates(text, order) or find_amounts(text, order):
        return False
    return True


def buyer_names(page_lines):
    """Names written next to or under a buyer label ("Customer Name: ...", "M/S ...")."""
    names = []
    for line in page_lines:
        match = BUYER_LABEL.match(line["text"])
        if not match:
            continue
        rest = re.sub(r"^[^:]*:\s*", "", line["text"][match.end():]) if ":" in line["text"] else ""
        texts = [rest] if rest else []
        texts += [other["text"] for other in right_neighbors(line, page_lines)[:1]]
        # The name can be a few lines under a heading such as "Customer's Details".
        below = sorted(
            (other for other in page_lines
             if other is not line and -0.5 * line["h"] <= other["t"] - line["b"] <= 5 * line["h"]
             and abs(other["l"] - line["l"]) <= 3 * line["h"]),
            key=lambda other: other["t"],
        )
        texts += [other["text"] for other in below[:3]]
        for text in texts:
            words = re.findall(r"[A-Za-z]{3,}", NAME_PREFIX.sub("", text))
            if len(words) >= 2:
                names.append(" ".join(words[:2]).lower())
    return names


def fallback_vendor_name(lines_by_page, order):
    """
    The supplier's name near the top of the first page: a company-like line that is
    not a title, a badge, an address, a label or the buyer's name. Larger, higher lines
    and lines with a company word (Ltd, Traders, Printers ...) are preferred.
    """
    if not lines_by_page:
        return None
    first_page = min(lines_by_page)
    page_lines = lines_by_page[first_page]
    page_height = page_lines[0]["page_height"] if page_lines else 1
    buyers = buyer_names(page_lines)
    candidates = [
        line for line in page_lines
        if line["t"] < 0.4 * page_height
        and looks_like_company_name(line, order)
        and not any(buyer in " ".join(re.findall(r"[A-Za-z]{3,}", line["text"])).lower() for buyer in buyers)
    ]
    if not candidates:
        return None
    tallest = max(line["h"] for line in candidates)

    def score(line):
        letters = len(re.findall(r"[A-Za-z]", line["text"]))
        value = line["h"] / tallest
        value += 0.8 if COMPANY_WORDS.search(line["text"]) else 0
        value -= 0.4 if letters < 6 else 0  # a logo abbreviation such as "AHP"
        value -= 0.6 * line["t"] / (0.4 * page_height)
        return value

    chosen = max(candidates, key=score)
    parts = [chosen]
    # A large name can wrap onto two lines ("Apex chemical" / "Technologies pvt Ltd").
    for other in candidates:
        if other is chosen or abs(other["h"] - chosen["h"]) > 0.15 * chosen["h"]:
            continue
        gap = min(abs(other["t"] - chosen["b"]), abs(chosen["t"] - other["b"]))
        aligned = abs(other["l"] - chosen["l"]) <= chosen["h"] or abs(other["r"] - chosen["r"]) <= chosen["h"]
        above = other["b"] <= chosen["yc"]
        if above:
            # The first half of a wrapped name, not a logo abbreviation such as "APES".
            fits = len(re.findall(r"[A-Za-z]", other["text"])) >= 6
        else:
            # The second half ("... Technologies pvt Ltd"), not a tagline under a complete name.
            fits = not COMPANY_WORDS.search(chosen["text"]) and bool(COMPANY_WORDS.search(other["text"]))
        if gap <= 0.5 * chosen["h"] and aligned and fits:
            parts.append(other)
            break
    parts.sort(key=lambda line: line["t"])
    name = " ".join(NAME_PREFIX.sub("", line["text"]) for line in parts)
    return {"value": re.sub(r"\s+", " ", name).strip(), "line": chosen, "ambiguous": False}


# ---------------------------------------------------------------------------
# More header fields: supplier and buyer details, references, tax amounts
# ---------------------------------------------------------------------------
# These labels are kept apart from FIELD_DEFS so they do not change how the main fields are read.
NTN_LABEL = r"(?:\bcnic\s*/\s*)?(?<![A-Za-z])n\.?\s?t\.?\s?n(?![A-Za-z])"
STRN_LABEL = (
    r"(?:(?<![A-Za-z])s\.?\s?t\.?\s?r\.?\s?n(?![A-Za-z])|sales\s*tax\s*reg\w*|"
    r"(?<![A-Za-z])gst(?![A-Za-z])\s*(?:no\b|#|reg\w*|number|:)|registration\s*(?:no\b|#|number))"
)
PARTY_FIELD_DEFS = {
    "ntn": {"kind": "code", "patterns": [NTN_LABEL], "reject": r"strn|sales\s*tax\s*reg"},
    "strn": {"kind": "code", "patterns": [STRN_LABEL]},
    "address": {"kind": "text", "patterns": [r"^\W*(?:address|addrass|adress|addres|addr\w*|customeradress)\b"]},
}
EXTRA_FIELD_DEFS = {
    "buyer_name": {
        "kind": "text",
        "patterns": [
            r"(?:buyer|customer|custo\w*|purchaser|party|consignee)(?:'?s)?\s*(?:name|narne|nime)\b",
            r"^\W*(?:bill(?:ed)?\s*to|sold\s*to|sale\s*to|ship(?:ped)?\s*to|m/s|messrs)\b",
            r"^\W*(?:buyer|purchaser|customer)(?:'?s)?\s*(?:details?|info\w*|particulars)?\W*$",
        ],
        "below": True,
    },
    "fbr_invoice_number": {
        "kind": "code",
        "patterns": [
            FBR_INVOICE + r"\w*\s*(?:number|no\b\.?|#|:)",
            r"pral\s*inv\w*",
            r"\bfbr\s*(?:#|no\b\.?|number|inv\w*)",
            r"digital\s*inv\w*",
        ],
        "below": True,
    },
    "dc_number": {
        "kind": "code",
        "patterns": [
            r"(?<![A-Za-z])d\.?\s?c\.?\s*(?:no\b\.?|#|number|:)",
            r"delivery\s*challan\s*(?:no\b\.?|#|number)?",
            r"\bchallan\s*(?:no\b\.?|#|number)",
        ],
        "below": True,
    },
    "gate_pass_number": {
        "kind": "code",
        "patterns": [r"gate\s*pass\s*(?:no\b\.?|#|number)?", r"\bigp\s*(?:no\b\.?|#)"],
        "below": True,
    },
    "contract_number": {
        "kind": "code",
        "patterns": [r"contract\s*(?:no\b\.?|#|number|:)"],
        "below": True,
    },
    "discount": {
        "kind": "amount",
        "patterns": [r"(?:total\s*)?discount", r"(?<![A-Za-z])dis[ce]\b"],
        "below": False,
        "pick": "last",
    },
    "further_tax": {
        "kind": "amount",
        "patterns": [r"fu[r]?t?her\s*(?:s\.?\s*)?tax", r"(?<![A-Za-z])f\.?\s*tax(?![A-Za-z])"],
        "below": False,
        "pick": "last",
    },
    "sales_tax_rate": {
        "kind": "percent",
        "patterns": [
            r"sales\s*tax\s*(?:@|%|\(|rate)",
            r"rate\s*of\s*(?:sales\s*)?tax",
            r"(?<![A-Za-z])(?:gst|s\.?\s*tax|sal\s*tax)\s*(?:@|%|\()",
            r"(?<![A-Za-z])tax\s*(?:%|@|rate)",
        ],
        "below": True,
    },
    "total_quantity": {
        "kind": "quantity",
        "patterns": [r"total\s*(?:quantity|qty|pcs|weight|kgs?|units)"],
        "below": False,
    },
    "amount_in_words": {
        "kind": "text",
        "patterns": [r"amount\s*in\s*words", r"(?<![A-Za-z])in\s*words", r"rupees\s*in\s*words"],
        "below": True,
    },
}
for _definition in list(PARTY_FIELD_DEFS.values()) + list(EXTRA_FIELD_DEFS.values()):
    _definition["regexes"] = [re.compile(pattern, re.IGNORECASE) for pattern in _definition["patterns"]]
    _definition["reject_regex"] = re.compile(_definition["reject"], re.IGNORECASE) if "reject" in _definition else None

EXTRA_LABEL_REGEXES = [
    regex for _d in list(PARTY_FIELD_DEFS.values()) + list(EXTRA_FIELD_DEFS.values()) for regex in _d["regexes"]
]
# Section headings that start the supplier's or the buyer's block.
BUYER_HEADING = re.compile(
    r"^\W*(?:buyer|customer|custo\w*|purchaser|party|bill(?:ed)?\s*to|ship(?:ped)?\s*to|sold\s*to|sale\s*to|"
    r"consignee|m/s|messrs)\b",
    re.IGNORECASE,
)
SELLER_HEADING = re.compile(
    r"^\W*(?:seller|supplier|vendor|sold\s*by|bill(?:ed)?\s*from|from)\b", re.IGNORECASE
)
AMOUNT_WORDS = re.compile(r"\b(?:hundred|thousand|lac|lakh|lacs|million|crore)\b", re.IGNORECASE)


def is_registration_number(value):
    """An NTN or STRN has at least 7 digits and is not an amount such as 3,576.96."""
    digits = re.sub(r"\D", "", value)
    return len(digits) >= 7 and not re.fullmatch(r"[\d,]*\.\d{1,2}", value)


def looks_like_party_name(value, order):
    """A buyer's name: mostly letters, not a date, an amount or a label."""
    letters = len(re.findall(r"[A-Za-z]", value))
    digits = len(re.findall(r"\d", value))
    if letters < 5 or digits > 0.3 * len(value) or parse_dates(value, order):
        return False
    return not is_extra_label(value)


def is_extra_label(text):
    """True when the text is a label of one of the extra fields (NTN, Address, DC #...)."""
    return any(regex.search(text) for regex in EXTRA_LABEL_REGEXES)


def party_of(line, page_lines, anchors=None):
    """
    "buyer" or "supplier": the section heading nearest above the line decides. The lines with the
    supplier's and the buyer's names count as headings too (anchors: {id(line): party}). Text without
    a heading above it belongs to the supplier (the supplier's details are printed at the top).
    """
    best = None
    width = line.get("page_width") or 1
    anchors = anchors or {}
    for heading in page_lines:
        if heading["t"] > line["yc"]:
            continue
        if id(heading) in anchors:
            party = anchors[id(heading)]
        elif BUYER_HEADING.match(heading["text"]):
            party = "buyer"
        elif SELLER_HEADING.match(heading["text"]):
            party = "supplier"
        else:
            continue
        # A heading in another column (seller left, buyer right) counts less than one straight above.
        sideways = max(0.0, abs(line["l"] - heading["l"]) - 0.2 * width)
        score = (line["yc"] - heading["yc"]) + 1.5 * sideways
        if best is None or score < best[0]:
            best = (score, party)
    return best[1] if best else "supplier"


def extra_candidates(definition, lines, lines_by_page, order):
    """Every value found next to (or under) one of the definition's labels, in reading order."""
    candidates = []
    for line in lines:
        match = first_match(definition["regexes"], line["text"])
        if not match:
            continue
        if definition["reject_regex"] and definition["reject_regex"].search(line["text"]):
            continue
        candidate = value_for_label(
            definition["kind"], line, match, lines_by_page[line["page"]], order, definition.get("below", True)
        )
        if candidate is None:
            continue
        value_line = candidate["line"]
        if value_line is not line and is_extra_label(value_line["text"]) and not first_match(
                definition["regexes"], value_line["text"]):
            continue  # the "value" is the next label ("NTN :" under "Address :")
        candidate["label_line"] = line
        candidates.append(candidate)
    return candidates


def find_extra_field(key, lines, lines_by_page, order):
    """One of the extra header fields (not supplier or buyer details)."""
    definition = EXTRA_FIELD_DEFS[key]
    candidates = extra_candidates(definition, lines, lines_by_page, order)
    if not candidates:
        return None
    return candidates[-1] if definition.get("pick") == "last" else candidates[0]


def find_party_fields(lines, lines_by_page, order, anchors=None):
    """NTN, STRN and address of the supplier and of the buyer: {"supplier_ntn": candidate, ...}."""
    found = {}
    for name, definition in PARTY_FIELD_DEFS.items():
        for candidate in extra_candidates(definition, lines, lines_by_page, order):
            if name in ("ntn", "strn") and not is_registration_number(candidate["value"]):
                continue
            label = candidate["label_line"]
            party = party_of(label, lines_by_page[label["page"]], anchors)
            key = party + "_" + name
            if key not in found:
                found[key] = candidate
    return found


def find_buyer_name(lines, lines_by_page, order, vendor_value):
    """The buyer's name: next to a buyer label, or a "Name :" line inside the buyer's block."""
    vendor_words = " ".join(re.findall(r"[A-Za-z]{3,}", vendor_value or "")).lower()
    for candidate in extra_candidates(EXTRA_FIELD_DEFS["buyer_name"], lines, lines_by_page, order):
        value = re.sub(r"^\W*(?:m/s|messrs)\.?\s*", "", candidate["value"], flags=re.IGNORECASE).strip()
        words = " ".join(re.findall(r"[A-Za-z]{3,}", value)).lower()
        if words and words != vendor_words and looks_like_party_name(value, order):
            return dict(candidate, value=value)
    for line in lines:
        match = NAME_PREFIX.match(line["text"])
        if not match or party_of(line, lines_by_page[line["page"]]) != "buyer":
            continue
        value = read_text_value("text", line["text"][match.end():], order)
        if value is not None and looks_like_party_name(value["value"], order):
            return dict(value, line=line)
        for other in right_neighbors(line, lines_by_page[line["page"]])[:1]:
            value = read_text_value("text", other["text"], order)
            if value is not None and looks_like_party_name(value["value"], order):
                return dict(value, line=other)
    return None


def fallback_amount_in_words(lines):
    """A line such as "Rupees One Hundred Thousand Only" without an "In words" label."""
    for line in lines:
        text = line["text"]
        if AMOUNT_WORDS.search(text) and (re.search(r"\bonly\b", text, re.I) or re.match(r"^\W*rupees\b", text, re.I)):
            value = re.sub(r"^[^:]*:\s*", "", text) if ":" in text else text
            return {"value": re.sub(r"\s+", " ", value).strip(" .-"), "line": line, "ambiguous": False}
    return None


STANDARD_TAX_RATES = (Decimal("18"), Decimal("17"), Decimal("16"), Decimal("15"), Decimal("13"),
                      Decimal("10"), Decimal("5"), Decimal("3"), Decimal("1"))


def tax_rate_from_amounts(subtotal_text, tax_text):
    """The sales tax percentage worked out from tax / subtotal, when it is a usual rate."""
    if not subtotal_text or not tax_text:
        return None
    subtotal, tax = Decimal(subtotal_text), Decimal(tax_text)
    if subtotal <= 0 or tax <= 0:
        return None
    percent = tax / subtotal * 100
    for rate in STANDARD_TAX_RATES:
        if abs(percent - rate) <= Decimal("0.05"):
            return format_quantity(rate)
    return None


# ---------------------------------------------------------------------------
# Line items table
# ---------------------------------------------------------------------------
# Words in the header row of an items table. OCR spellings ("Qnty", "Desuriprion") are included.
HEADER_WORDS = {
    "description": re.compile(r"\b(?:descr\w*|desur\w*|item\s*name|items?|products?(?:\s*name)?|goods|particulars|details|services?)\b", re.I),
    "quantity": re.compile(r"\b(?:qty|qnty|qnty\.?|quantity|q\W?\s?[au]?antity|qrity|q'ty|pcs\s*qty|hours|hrs)\b", re.I),
    "rate": re.compile(r"\b(?:rate|price|unit\s*price|unit\s*cost|um\s*rate)\b", re.I),
    "amount": re.compile(r"\b(?:a?mount|val\w{0,3}|excl\w*|exc|incl\w*|inel|total)\b", re.I),
}
# A row under the items that ends the table.
TABLE_END = re.compile(
    r"^\W*(?:sub[\s\-]?total|(?:grand\s*)?t[o0ue]t[ae@][l1i]\b|amount\s*in\s*words|in\s*words|notes?\s*[:\-]|terms\s*[:&]|"
    r"thank|sales\s*tax\s*[:@]|further\s*tax|fbr\b|this\s+is|goods\s+at|signature|discount\s*@|retail\s*value|"
    r"rupees|net\s*tax)",
    re.IGNORECASE,
)
HS_CODE = re.compile(r"\d{4}\.\d{4}")  # Pakistan customs tariff code such as 4802.6200
UNIT_SUFFIX = re.compile(r"(?:nos|pcs|pc|kgs?|mtrs?|sets?|doz|rolls?|pkts?|box(?:es)?|ltrs?|gms?|units?)\b", re.I)
UNIT_WORDS = re.compile(
    r"^\W*(?:nos?|pcs|pc|kgs?|mtrs?|meters?|sets?|doz(?:en)?|rolls?|pkts?|box(?:es)?|ltrs?|gms?|units?|"
    r"numbers,?|pieces,?|(?:numbers,?\s*)?pieces,?\s*units?|each|lot|"
    r"n[\w,.:;]{0,3}\s?[mn]?bers?)\W*$",  # OCR spellings of "Number": NLmber, N, mber, Nu mber, N: mber
    re.IGNORECASE,
)
DESCRIPTION_NOISE = re.compile(r"^\W*(?:h\.?\s*s\.?\s*cod\w*|[uv]\s*o\s*m\b|sro\b|qty\s*:)", re.IGNORECASE)
UNIT_MISREAD = re.compile(r"(?=\w{3})(?:nu?mbe[nr]s?|p[l1i]e?c?e?s?|uni?te?s?)[,.]?", re.IGNORECASE)
GLUED_AMOUNTS = re.compile(r"(\d{1,3}(?:,\d{3})*\.\d{2})([1-9]\d{0,2}(?:,\d{3})*\.\d{2})")  # "31,200.005,616.00"


def is_header_row(row, order):
    """True when the row is the header of the line item table (Description, Qty, Rate, Amount)."""
    keys = {key for line in row["lines"] for key, pattern in HEADER_WORDS.items() if pattern.search(line["text"])}
    # A stray small integer (OCR noise, a column number) does not make the header an item row.
    has_amounts = any(
        amount >= 100 or amount != amount.to_integral_value()
        for line in row["lines"] for amount in find_amounts(line["text"], order)
    )
    return not has_amounts and len(keys & {"quantity", "rate", "amount"}) >= 2 and len(row["lines"]) >= 3


def row_numbers(row, order):
    """Numbers in a table row, left to right: [{"value", "x", "line"}]. HS codes, percentages, dates and codes are skipped."""
    numbers = []
    for line in row["lines"]:
        text = mask_dates(line["text"], order)
        width = max(len(text), 1)
        for match in NUMBER_TOKEN.finditer(text):
            token, (start, end) = match.group(), match.span()
            before = text[start - 1] if start > 0 else " "
            after = text[end:]
            if before.isalpha() or before in "_/#":
                continue  # PO217999, INV#701
            if before in "-−" and start >= 2 and text[start - 2].isalnum():
                continue  # 3x130-10MM, IVN-10012
            if after[:1].isalpha() and not UNIT_SUFFIX.match(after):
                continue  # 1VGA, 10MM (but 10.00NOS is a quantity)
            if after.lstrip().startswith("%") or HS_CODE.fullmatch(token) or re.fullmatch(r"0\d+", token):
                continue
            glued = GLUED_AMOUNTS.fullmatch(token) if "," in token else None
            if not glued and len(re.sub(r"\D", "", token)) > 12:
                continue
            parts = [glued.group(1), glued.group(2)] if glued else [token]
            for offset, part in enumerate(parts):
                value = parse_number_token(part)
                if value is None or -value.as_tuple().exponent > 4:
                    continue  # "917,000034": not an amount (a rate can have 4 decimals: 2.5000)
                x = line["l"] + (line["r"] - line["l"]) * (start + offset * len(parts[0])) / width
                numbers.append({"value": value, "x": x, "line": line})
                groups = re.split(r"[.,]", part)
                if len(groups) >= 3 and len(groups[-1]) == 3:
                    # "2,764,000" / "16.311.140": OCR mixes up "," and "." in 3-decimal amounts (2,764.000).
                    alternative = Decimal("".join(groups[:-1]) + "." + groups[-1])
                    if alternative != value:
                        numbers.append({"value": alternative, "x": x + 0.01, "line": line})
    numbers.sort(key=lambda item: item["x"])
    return numbers


def find_without_amount(numbers, columns):
    """
    The amount cell was not read, but quantity x rate + tax = amount with tax is on the row:
    the amount is worked out. Returns (quantity, rate, amount) or None.
    """
    count = len(numbers)
    for i in range(count):
        for j in range(i + 1, count):
            product = numbers[i]["value"] * numbers[j]["value"]
            if product <= 0 or 1 in (numbers[i]["value"], numbers[j]["value"]):
                continue
            for k in range(j + 1, count):
                for m in range(k + 1, count):
                    tax, including = numbers[k]["value"], numbers[m]["value"]
                    tolerance = max(Decimal("1"), including * Decimal("0.005"))
                    if 0 < tax < product and abs(product + tax - including) <= tolerance:
                        quantity, rate = numbers[i], numbers[j]
                        if "quantity" in columns and "rate" in columns and \
                                abs(rate["x"] - columns["quantity"]) + abs(quantity["x"] - columns["rate"]) < \
                                abs(quantity["x"] - columns["quantity"]) + abs(rate["x"] - columns["rate"]):
                            quantity, rate = rate, quantity
                        amount = {"value": including - tax, "x": columns.get("amount", 0), "line": None}
                        return quantity, rate, amount
    # The quantity was not read either: amount = amount with tax - tax, and amount / rate is a whole quantity.
    # The amount with tax is the last number of the row and the tax comes right before it.
    for m in range(count - 1, max(count - 3, 0), -1):
        for k in range(m - 1, max(m - 3, -1), -1):
            tax, including = numbers[k]["value"], numbers[m]["value"]
            if not 0 < tax < including:
                continue
            amount_value = including - tax
            if not plausible_tax(amount_value, tax):
                continue
            for j in range(k):
                rate_value = numbers[j]["value"]
                if rate_value < 10 or rate_value >= amount_value:
                    continue
                if "rate" in columns and abs(numbers[j]["x"] - columns["rate"]) > min(
                        abs(numbers[j]["x"] - columns[key]) for key in ("quantity", "amount") if key in columns):
                    continue  # nearer another column than the rate column
                quantity_value = amount_value / rate_value
                whole = quantity_value.to_integral_value()
                if whole >= 1 and abs(quantity_value - whole) <= Decimal("0.001") * whole:
                    return ({"value": whole, "x": 0, "line": None}, numbers[j],
                            {"value": amount_value, "x": columns.get("amount", 0), "line": None})
    return None


def find_quantity_rate_amount(numbers, columns):
    """
    The three numbers of a row where quantity x rate = amount. The amount is the first such
    product from the left (the value before tax). Which factor is the quantity is decided by the
    header columns, or by position (quantity before rate).
    """
    found = None
    for k, amount in enumerate(numbers):
        value = amount["value"]
        if value <= 0:
            continue
        tolerance = max(Decimal("1"), value * Decimal("0.005"))
        for i in range(k):
            for j in range(i + 1, k):
                first, second = numbers[i]["value"], numbers[j]["value"]
                if min(first, second) < Decimal("0.2") or abs(first * second - value) > tolerance:
                    continue  # a factor below 0.2 is a misread, not a price or quantity
                trivial = first == 1 or second == 1
                candidate = (trivial, k, i, j)
                if found is None or candidate[:2] < found[:2]:
                    found = candidate
        if found and not found[0]:
            break  # The leftmost product that is not "x 1" wins.
    if not found:
        return find_without_amount(numbers, columns)
    _, k, i, j = found
    quantity, rate = numbers[i], numbers[j]
    if "quantity" in columns and "rate" in columns:
        straight = abs(quantity["x"] - columns["quantity"]) + abs(rate["x"] - columns["rate"])
        swapped = abs(rate["x"] - columns["quantity"]) + abs(quantity["x"] - columns["rate"])
        if swapped < straight:
            quantity, rate = rate, quantity
    return quantity, rate, numbers[k]


def description_text(lines, used_values, serial):
    """
    The words of a row that describe the item: not the numbers used as quantity, rate or
    amount, not units, tariff codes or column words. Numbers inside the text ("7 PLY") stay.
    """
    words = []
    for line in lines:
        text = line["text"]
        if UNIT_WORDS.match(text) or not re.search(r"[A-Za-z]{2}", text):
            continue  # unit column, or a cell with only numbers
        if DESCRIPTION_NOISE.match(text):
            continue  # "HS Code: 4813.4000", "UOM: KG" printed under the item name
        if re.fullmatch(r"\s*[\d.,]+\s*" + LINE_UNIT.pattern + r"\W*", text, re.IGNORECASE):
            continue  # a quantity with its unit ("9,180.00 Pcs")
        if is_header_text(text):
            continue
        line_words = text.split()
        # Numbers at the end of a text cell are the row's quantity, rate or amount ("Consulting 75.00 15 1,125.00");
        # numbers inside the text belong to the description ("7 PLY CTN").
        while line_words and re.fullmatch(r"[-(]?[\d.,]+\)?", line_words[-1]) and (
                parse_number_token(line_words[-1].strip("-()")) in used_values):
            line_words.pop()
        for word in line_words:
            if HS_CODE.fullmatch(word) or UNIT_WORDS.match(word) or not re.search(r"[A-Za-z0-9]", word):
                continue
            if UNIT_MISREAD.fullmatch(word):
                continue  # "Numbers, pieces, units" as OCR reads it: Numbens, pleces, unte
            if re.fullmatch(r"[\d.,]+(?:" + UNIT_SUFFIX.pattern + r")", word, re.I):
                continue  # 10.00NOS
            word = re.sub(r"^" + str(serial) + r"(?=[A-Za-z])", "", word)  # "1VGA" in row 1
            words.append(word)
    text = " ".join(words).strip(" -|:")
    return text if re.search(r"[A-Za-z]{2}", text) else ""


def nearest_number(numbers, x, exclude=()):
    """Return the number whose horizontal position is closest to x."""
    candidates = [item for item in numbers if id(item) not in exclude]
    return min(candidates, key=lambda item: abs(item["x"] - x)) if candidates else None


def items_by_columns(numbers, columns):
    """
    A row without quantity x rate = amount (for example an invoice without a rate column):
    take the numbers under the quantity and amount headings. Returns (quantity, rate, amount) or None.
    """
    if "amount" not in columns or not numbers:
        return None
    amount = nearest_number(numbers, columns["amount"])
    if amount is None or amount["value"] <= 0 or abs(amount["x"] - columns["amount"]) > columns.get("width", 1e9) * 0.08:
        return None
    def closest_column(item):
        keys = [key for key in ("quantity", "rate", "amount") if key in columns]
        return min(keys, key=lambda key: abs(item["x"] - columns[key]))

    def under(key, exclude):
        # The nearest number to the heading that is not nearer to another heading.
        if key not in columns:
            return None
        found = nearest_number([item for item in numbers if closest_column(item) == key], columns[key], exclude)
        if found is not None and abs(found["x"] - columns[key]) > columns.get("width", 1e9) * 0.08:
            return None
        return found

    quantity = under("quantity", {id(amount)})
    rate = under("rate", {id(amount), id(quantity)})
    if quantity and rate and abs(quantity["value"] * rate["value"] - amount["value"]) > max(
            Decimal("1"), amount["value"] * Decimal("0.005")):
        quantity = None  # misread quantity ("49" read as "43"): worked out from amount / rate
    return quantity, rate, amount


LINE_UNIT = re.compile(
    r"(?<![A-Za-z])(kgs?|pcs|pc|nos|mtrs?|meters?|sets?|doz(?:en)?|rolls?|pkts?|box(?:es)?|ltrs?|gms?|units?|each|lot)(?![A-Za-z])",
    re.IGNORECASE,
)


def line_extras(lines, numbers, used, amount):
    """
    The other columns of an item row: HS code, unit, tax percentage, and the sales tax, further tax
    and value including tax found from amount + tax (+ further tax) = value including tax.
    """
    texts = [line["text"] for line in lines]
    joined = " ".join(texts)
    extras = {key: "" for key in LINE_ITEM_KEYS if key not in ("description", "quantity", "rate", "amount")}
    hs_code = re.search(r"(?<![\d.])\d{4}\.\d{4}(?![\d.])", joined)
    if hs_code:
        extras["hs_code"] = hs_code.group()
    for text in texts:
        if UNIT_WORDS.match(text):
            extras["uom"] = re.sub(r"\s+", " ", text).strip(" ,.:;")
            break
    if not extras["uom"]:
        unit = LINE_UNIT.search(re.sub(r"\d{4}\.\d{4}", " ", joined))
        if unit:
            extras["uom"] = unit.group(1).upper()
    percent = PERCENT.search(joined)
    if percent:
        value = parse_number_token(percent.group(1))
        if value is not None and 0 < value <= 100:
            extras["tax_rate"] = format_quantity(value)

    value = amount["value"]
    used_ids = {id(item) for item in used if item}
    rest = [
        item for item in numbers
        if id(item) not in used_ids and item["value"] > 0
        and (amount["line"] is None or item["x"] > amount["x"])
    ]
    found = None
    for i in range(len(rest)):
        for j in range(i + 1, len(rest)):
            for k in range(j + 1, len(rest)):
                if rest[j]["value"] < value and close(value + rest[i]["value"] + rest[j]["value"], rest[k]["value"]):
                    found = (rest[i], rest[j], rest[k])
                    break
            if found:
                break
        if found:
            break
    if found is None:
        for i in range(len(rest)):
            for j in range(i + 1, len(rest)):
                if rest[i]["value"] < value and close(value + rest[i]["value"], rest[j]["value"]):
                    found = (rest[i], None, rest[j])
                    break
            if found:
                break
    if found:
        sales_tax, further_tax, including = found
        extras["sales_tax"] = format_amount(sales_tax["value"])
        extras["further_tax"] = format_amount(further_tax["value"]) if further_tax else ""
        extras["amount_incl_tax"] = format_amount(including["value"])
        if not extras["tax_rate"]:
            extras["tax_rate"] = tax_rate_from_amounts(format_amount(value), extras["sales_tax"]) or ""
    elif extras["tax_rate"]:
        expected = value * Decimal(extras["tax_rate"]) / 100
        for item in rest:
            if close(item["value"], expected):
                extras["sales_tax"] = format_amount(item["value"])
                extras["amount_incl_tax"] = format_amount(value + item["value"])
                break
    return extras


def read_items_table(rows, columns, page_number, order, median_height, pending=None, subtotal=None):
    """Read the line item rows below the table header and turn them into line items (description, quantity, rate, amount)."""
    items = []
    pending = list(pending or [])  # text rows that are not yet attached to an item
    last_bottom = None
    carry = []            # a row without a match, tried again together with the next row (a tall cell split in two)

    def numbers_of(row_lines, serial):
        found = row_numbers({"lines": row_lines}, order)
        if found and found[0]["value"] == serial and found[0]["line"]["l"] <= min(line["l"] for line in row_lines):
            found = found[1:]  # serial number column
        return found

    for row in rows:
        lines = [line for line in row["lines"] if line["h"] <= 2.2 * median_height]  # skip stamps and signatures
        if not lines:
            continue
        top = min(line["t"] for line in lines)
        if last_bottom is not None and top - last_bottom > 6 * median_height and items:
            break  # A large gap after the items: the rest of the page is not the table.
        if any(TOTAL_ROW.match(line["text"].strip()) for line in lines):
            break  # the totals row

        serial = len(items) + 1
        triple = None
        if carry:
            numbers = numbers_of(carry + lines, serial)
            triple = find_quantity_rate_amount(numbers, columns)
            if triple:
                lines = carry + lines
            else:
                text = description_text(carry, set(), serial)
                if text:
                    pending.append(text)
            carry = []
        if triple is None:
            numbers = numbers_of(lines, serial)
            triple = find_quantity_rate_amount(numbers, columns)
        if triple is None and any(TABLE_END.match(line["text"]) for line in lines):
            break
        if triple is None:
            text = description_text(lines, set(), serial)
            if text and not numbers:
                anchor = items[-1].get("_last_text") if items else None
                first = min(lines, key=lambda line: line["l"])
                if anchor and abs(first["l"] - anchor["l"]) <= 1.5 * anchor["h"] \
                        and -0.6 * anchor["h"] <= first["t"] - anchor["b"] <= 0.6 * anchor["h"]:
                    # Directly under the item's name and aligned with it: the second line of the name.
                    items[-1]["description"] = (items[-1]["description"] + " - " + text).strip(" -")
                    items[-1]["_last_text"] = first
                    items[-1].pop("_awaiting", None)
                    last_bottom = max(line["b"] for line in lines)
                    continue
                if items and items[-1].pop("_awaiting", False):
                    # The item's row had no words: its name is printed just under the numbers.
                    items[-1]["description"] = text
                    items[-1]["_continues"] = True
                elif items and items[-1].get("_continues"):
                    items[-1]["description"] = (items[-1]["description"] + " " + text).strip()
                else:
                    pending.append(text)
                last_bottom = max(line["b"] for line in lines)
                continue
            if not text and not pending:
                continue
            triple = items_by_columns(numbers, columns)
            if triple is None:
                carry = lines
                continue

        quantity, rate, amount = triple
        width = columns.get("width", 1e9)
        suspicious = quantity and rate and (
            1 in (quantity["value"], rate["value"])  # "117.00" read as "1:7.00" gives "1 x 7 = 7"
            or ("amount" in columns and abs(amount["x"] - columns["amount"]) > 0.08 * width)  # not under Amount
        )
        if suspicious:
            by_columns = items_by_columns(numbers, columns)
            if by_columns and by_columns[1] and by_columns[2]["value"] != amount["value"]:
                quantity, rate, amount = None, by_columns[1], by_columns[2]
        if quantity is None and rate and rate["value"] > 0:
            # The quantity cell could not be read ("1,:49.00"): amount / rate, a whole number when close to one.
            derived = amount["value"] / rate["value"]
            whole = derived.to_integral_value()
            derived = whole if abs(derived - whole) <= max(Decimal("0.01"), derived * Decimal("0.0005")) \
                else derived.quantize(Decimal("0.01"))
            quantity = {"value": derived, "x": 0, "line": None}
        used_values = {item["value"] for item in (quantity, rate, amount) if item}
        if items and close(amount["value"], sum(Decimal(item["amount"]) for item in items)) and len(items) >= 1 \
                and (quantity is None or rate is None or not description_text(lines, used_values, serial)):
            break  # the totals row (its "Total" label sits on another line): amount = sum of the items
        description = description_text(lines, used_values, serial)
        if items:
            items[-1].pop("_awaiting", None)
            items[-1].pop("_continues", None)
        if pending:
            if not description:
                description = " ".join(pending)
            elif items:
                items[-1]["description"] = (items[-1]["description"] + " - " + " ".join(pending)).strip(" -")
            pending = []
        if rate is None and quantity is not None and quantity["value"] > 0:
            rate_value = (amount["value"] / quantity["value"]).quantize(Decimal("0.0001"))
        else:
            rate_value = rate["value"] if rate else None
        confidences = [line["confidence"] for line in lines if line.get("confidence") is not None]
        extras = line_extras(lines, numbers, (quantity, rate, amount), amount)
        items.append({
            "description": description,
            "hs_code": extras["hs_code"],
            "uom": extras["uom"],
            "quantity": format_quantity(quantity["value"]) if quantity else "",
            "rate": format_amount(rate_value) if rate_value is not None else "",
            "amount": format_amount(amount["value"]),
            "tax_rate": extras["tax_rate"],
            "sales_tax": extras["sales_tax"],
            "further_tax": extras["further_tax"],
            "discount": extras["discount"],
            "amount_incl_tax": extras["amount_incl_tax"],
            "confidence": min(confidences) if confidences else None,
            "page": page_number,
        })
        if not description:
            items[-1]["_awaiting"] = True
        name_lines = [
            line for line in lines
            if re.search(r"[A-Za-z]{3}", line["text"]) and not is_header_text(line["text"])
            and not UNIT_WORDS.match(line["text"]) and not DESCRIPTION_NOISE.match(line["text"])
        ]
        if name_lines:
            items[-1]["_last_text"] = max(name_lines, key=lambda line: line["b"])
        last_bottom = max(line["b"] for line in lines)
        if len(items) >= MAX_LINE_ITEMS:
            break
    if pending and items:
        items[-1]["description"] = (items[-1]["description"] + " - " + " ".join(pending)).strip(" -")
    for item in items:
        item.pop("_awaiting", None)
        item.pop("_continues", None)
        item.pop("_last_text", None)
    return items


HEADER_EXTRA = re.compile(
    r"%|@|\b(?:tax|sales|of|rs\.?|payable|uom|unit|um|hs|code|s\.?\s*no|sr|gst|kgs|excluding|including|"
    r"inclusive|exclusive|further|f\.?\s*tax|value|net|discount|weight|sal|po|ref|ex\w{0,3}ding|in\w{0,3}ding)\b",
    re.IGNORECASE,
)


DESCRIPTION_HEADER = re.compile(
    r"\b(?:descr\w*|desur\w*|particulars|item\s*name|product\s*name|goods)\b", re.IGNORECASE
)


def is_header_text(text):
    """A cell with only column words ("Tax Value", "Sales Tax - Rs.", "18% Value"); "Services" is an item."""
    rest = text
    for pattern in (DESCRIPTION_HEADER, HEADER_WORDS["quantity"], HEADER_WORDS["rate"], HEADER_WORDS["amount"], HEADER_EXTRA):
        rest = pattern.sub(" ", rest)
    return not re.search(r"[A-Za-z]{2}", rest)


def is_header_continuation(row, order):
    """A second header line: only column words, no amounts."""
    if any(find_amounts(line["text"], order) for line in row["lines"]):
        return False
    return all(is_header_text(line["text"]) for line in row["lines"])


AMOUNT_BEFORE_TAX = re.compile(r"\b(?:excl\w*|exc|amount|value(?!\s*incl))\b", re.I)


def header_columns(header_rows):
    """x positions of the quantity, rate and amount (before tax) columns in the header."""
    columns = {}
    lines = [line for row in header_rows for line in row["lines"]]
    for key in ("quantity", "rate"):
        for line in lines:
            if key == "rate" and re.search(r"tax|sales|%|\bof\b", line["text"], re.I):
                continue  # "Rate of Sales Tax" is the tax percentage, not the item price
            if key not in columns and HEADER_WORDS[key].search(line["text"]):
                columns[key] = line["xc"]
    amount_lines = [line for line in lines if AMOUNT_BEFORE_TAX.search(line["text"])] or \
        [line for line in lines if HEADER_WORDS["amount"].search(line["text"])]
    if amount_lines:
        columns["amount"] = min(amount_lines, key=lambda line: line["xc"])["xc"]
    if lines:
        columns["width"] = lines[0]["page_width"]
    return columns


def extract_line_items(lines_by_page, order, subtotal=None):
    """Line items from the items table of each page: rows where quantity x rate = amount."""
    items = []
    for page_number in sorted(lines_by_page):
        page_lines = lines_by_page[page_number]
        if not page_lines:
            continue
        rows = group_rows(page_lines)
        heights = sorted(line["h"] for line in page_lines)
        median_height = heights[len(heights) // 2]
        for position, row in enumerate(rows):
            if not is_header_row(row, order):
                continue
            # A header can take two text rows ("Value" over "Excluding").
            header_rows = [row]
            start = position + 1
            while start < len(rows) and abs(rows[start]["yc"] - header_rows[-1]["yc"]) <= 1.6 * median_height \
                    and is_header_continuation(rows[start], order):
                header_rows.append(rows[start])
                start += 1
            # The first item's description can sit on a header text row ("7 PLY CTN ..." next to "Sales Tax - Rs.").
            first_text = [
                line["text"] for header_row in header_rows for line in header_row["lines"]
                if re.search(r"[A-Za-z]{2}", line["text"]) and not is_header_text(line["text"])
                and not UNIT_WORDS.match(line["text"])
            ]
            found = read_items_table(
                rows[start:], header_columns(header_rows), page_number, order, median_height, first_text, subtotal
            )
            if found:
                items.extend(found)
                break
        else:
            items.extend(items_without_header(rows, page_number, order, median_height, subtotal))
    return items[:MAX_LINE_ITEMS]


def items_without_header(rows, page_number, order, median_height, subtotal=None):
    """
    The header was not read (white text on a dark band often is not): the table starts at the
    first row where quantity x rate = amount, or quantity x rate + tax = amount with tax.
    """
    for position, row in enumerate(rows):
        if any(TOTAL_ROW.match(line["text"].strip()) for line in row["lines"]):
            return []
        numbers = row_numbers(row, order)
        triple = find_quantity_rate_amount(numbers, {})
        if triple and 1 not in (triple[0]["value"], triple[1]["value"]) and re.search(
                r"[A-Za-z]{3}", " ".join(line["text"] for line in row["lines"])):
            return read_items_table(rows[position:], {}, page_number, order, median_height, subtotal=subtotal)
    return []


def _group_by_height(page_lines, slope):
    """Group the text lines of a page into rows. Lines at about the same height (allowing for page tilt) form one row."""
    heights = sorted(line["h"] for line in page_lines)
    median_height = heights[len(heights) // 2]

    def level(line):
        return line["yc"] - slope * line["xc"]

    rows = []
    for line in sorted(page_lines, key=lambda item: (level(item), item["l"])):
        if rows and abs(level(line) - rows[-1]["yc"]) <= 0.6 * median_height:
            rows[-1]["lines"].append(line)
            rows[-1]["yc"] = sum(level(item) for item in rows[-1]["lines"]) / len(rows[-1]["lines"])
        else:
            rows.append({"yc": level(line), "lines": [line]})
    for row in rows:
        row["lines"].sort(key=lambda item: (item["l"], item["yc"]))
    return rows


def page_slope(page_lines):
    """
    How much a scanned page is tilted: the rise of a row per pixel to the right, measured on
    long rows (median of their least-squares slopes). 0 for a straight page.
    """
    width = page_lines[0]["page_width"] if page_lines else 1
    slopes = []
    for row in _group_by_height(page_lines, 0):
        lines = row["lines"]
        if len(lines) < 4 or max(line["xc"] for line in lines) - min(line["xc"] for line in lines) < 0.4 * width:
            continue
        mean_x = sum(line["xc"] for line in lines) / len(lines)
        mean_y = sum(line["yc"] for line in lines) / len(lines)
        spread = sum((line["xc"] - mean_x) ** 2 for line in lines)
        if spread:
            slopes.append(sum((line["xc"] - mean_x) * (line["yc"] - mean_y) for line in lines) / spread)
    if len(slopes) < 2:
        return 0.0
    slopes.sort()
    slope = slopes[len(slopes) // 2]
    return slope if 0.004 <= abs(slope) <= 0.05 else 0.0


def group_rows(page_lines):
    """Group lines that are on the same visual row (allowing for a slightly tilted scan)."""
    if not page_lines:
        return []
    return _group_by_height(page_lines, page_slope(page_lines))


# ---------------------------------------------------------------------------
# Subtotal + tax = total
# ---------------------------------------------------------------------------
TOTAL_WORD = r"t[o0ue]t[ae@][l1i]"  # OCR reads "Total" as "Totai", "Tutal", "Totel" or "Tetal"
TOTAL_ROW = re.compile(r"(?<![A-Za-z])(?:grand\s*)?" + TOTAL_WORD + r"(?![A-Za-z])(?!\s*(?:quantity|qty|pcs|weight))", re.IGNORECASE)
AMOUNT_TOLERANCE = Decimal("1.00")  # rounding on the invoice


def close(first, second):
    """True when two amounts are equal within the allowed rounding tolerance."""
    return abs(first - second) <= AMOUNT_TOLERANCE


def table_total_amounts(lines_by_page, order):
    """
    Read a totals row of the items table, for example
    "Total | 50.00 | 31,200.00 | 5,616.00 | 0.00 | 36,816.00": find amounts where one amount
    plus the amounts after it (the taxes) equals a later amount (the total).
    Returns (subtotal, tax, total, line) as Decimals, or None.
    """
    best = None
    for page_number in sorted(lines_by_page):
        page_lines = lines_by_page[page_number]
        for row in group_rows(page_lines):
            texts = [line["text"] for line in row["lines"]]
            start = next((i for i, text in enumerate(texts) if TOTAL_ROW.search(text) and not re.search(r"sub", text, re.I)), None)
            if start is None:
                continue
            amounts = []
            for line in row["lines"][start:]:
                text = line["text"]
                match = TOTAL_ROW.search(text)
                if match and line is row["lines"][start]:
                    text = text[match.end():]
                amounts.extend(find_amounts(text, order))
            # Prefer the rightmost total, then the widest subtotal.
            for k in range(len(amounts) - 1, 1, -1):
                total = amounts[k]
                for i in range(0, k - 1):
                    tax = sum(amounts[i + 1:k], Decimal(0))
                    subtotal = amounts[i]
                    if subtotal > 0 and tax > 0 and tax < subtotal and close(subtotal + tax, total):
                        found = (subtotal, tax, total, row["lines"][start])
                        if best is None or total > best[2]:
                            best = found
                        break
                if best and best[2] == total:
                    break
    return best


def page_amounts(lines, order):
    """All money amounts found in the given lines."""
    return {amount for line in lines for amount in find_amounts(line["text"], order)}


def money_amounts(lines, order):
    """Amounts written like money (with a thousands separator or decimals), so phone numbers and codes are left out."""
    found = []
    for line in lines:
        masked = mask_dates(line["text"], order)
        for match in NUMBER_TOKEN.finditer(masked):
            if not re.search(r"[.,]", match.group()):
                continue
            found.extend(find_amounts(match.group(), order))
    return sorted({amount for amount in found if amount > 0}, reverse=True)


def plausible_tax(subtotal, tax):
    """Sales tax (with any further tax) is between 0.5 % and 35 % of the value."""
    return subtotal > 0 and Decimal("0.005") * subtotal < tax <= Decimal("0.35") * subtotal


def sum_on_page(lines, order, total=None):
    """
    Two printed amounts that add up to a total: (subtotal, tax, total).
    Tries the given total first, then the five largest amounts on the page.
    """
    amounts = money_amounts(lines, order)
    targets = ([total] if total is not None else []) + amounts[:5]
    for target in targets:
        for subtotal in amounts:
            if subtotal >= target:
                continue
            tax = target - subtotal
            if plausible_tax(subtotal, tax) and any(close(tax, amount) for amount in amounts):
                return subtotal, tax, target
    return None


def reconcile_amounts(found, lines, lines_by_page, order):
    """
    Make subtotal, tax and total agree. Labels can point at the wrong number
    (a rate, a quantity, a further tax of 0), so:
    1. values that already add up are kept;
    2. otherwise a totals row of the items table that adds up is used;
    3. otherwise a missing value is worked out from the other two, when that number is printed on the page.
    """
    def amount(key):
        return Decimal(found[key]["value"]) if found.get(key) else None

    def put(key, value, line):
        found[key] = {"value": format_amount(value), "line": line, "ambiguous": False}

    subtotal, tax, total = amount("subtotal"), amount("tax"), amount("total_due")
    if subtotal is not None and tax is not None and total is not None and close(subtotal + tax, total):
        return
    table = table_total_amounts(lines_by_page, order)
    if table:
        table_subtotal, table_tax, table_total, line = table
        put("subtotal", table_subtotal, line)
        if total is not None and total > table_total + AMOUNT_TOLERANCE and total - table_subtotal < table_subtotal:
            # A labelled grand total above the table total includes a further tax: keep it.
            put("tax", total - table_subtotal, line)
        else:
            put("total_due", table_total, line)
            put("tax", table_tax, line)
        return

    printed = page_amounts(lines, order)
    if total is not None and tax is not None and (subtotal is None or not close(subtotal + tax, total)):
        if total - tax in printed and total - tax > 0:
            put("subtotal", total - tax, found["total_due"]["line"])
            return
    if total is not None and subtotal is not None and (tax is None or not close(subtotal + tax, total)):
        if total - subtotal in printed and total - subtotal >= 0:
            put("tax", total - subtotal, found["total_due"]["line"])
            return
    if subtotal is not None and tax is not None and total is None:
        if subtotal + tax in printed:
            put("total_due", subtotal + tax, found["subtotal"]["line"])
            return

    # Last resort: two printed amounts that add up to the total (or to the largest amount on the page).
    line = (found.get("total_due") or found.get("subtotal") or found.get("tax") or {}).get("line")
    parts = sum_on_page(lines, order, total) if lines else None
    if parts:
        source = line or lines[0]
        put("subtotal", parts[0], source)
        put("tax", parts[1], source)
        put("total_due", parts[2], source)


def reconcile_line_items(items, subtotal_text):
    """
    When the items do not add up to the subtotal and one item's quantity x rate differs from its
    amount (a misread digit, for example 69,540 for 69,640), use quantity x rate if that makes them add up.
    """
    if not subtotal_text or not items:
        return items
    subtotal = Decimal(subtotal_text)
    amounts = [Decimal(item["amount"]) if item["amount"] else Decimal(0) for item in items]
    if close(sum(amounts), subtotal):
        return items
    for position, item in enumerate(items):
        if not (item["quantity"] and item["rate"]):
            continue
        product = Decimal(item["quantity"]) * Decimal(item["rate"])
        if close(product, amounts[position]):
            continue
        for candidate in (product.quantize(Decimal("1")), product.quantize(Decimal("0.01"))):
            if close(sum(amounts) - amounts[position] + candidate, subtotal):
                item["amount"] = format_amount(candidate)
                return items
    return items


def clean_code(value):
    """'1403.00' printed by a spreadsheet is invoice 1403; 'PO222035' / 'P0222035' is PO 222035."""
    value = re.sub(r"^(\d+)\.0+$", r"\1", value)
    return value


def clean_po(value):
    """Remove an OCR stamp prefix from a PO number (for example P0222035 becomes 222035)."""
    # Only a stamp-like "P0222035" / "PO222035"; a written "PO-5500" is kept as printed.
    return re.sub(r"^P[O0](?=\d{5,}$)", "", clean_code(value), flags=re.IGNORECASE)


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------
def extract_invoice_data(page_results):
    """
    Extract invoice fields and line items.

    Returns {"fields": {key: field record}, "line_items": [...], "date_order": "dmy"|"mdy"}.
    """
    lines = collect_lines(page_results)
    lines_by_page = {}
    for line in lines:
        lines_by_page.setdefault(line["page"], []).append(line)

    order = detect_date_order([line["text"] for line in lines])
    definitions = {definition["key"]: definition for definition in FIELD_DEFS}
    found = {}
    for key in ("invoice_number", "due_date", "invoice_date", "po_number", "subtotal", "tax", "total_due", "vendor_name"):
        found[key] = find_field(definitions[key], lines, lines_by_page, order)

    reconcile_amounts(found, lines, lines_by_page, order)
    if found["invoice_number"]:
        found["invoice_number"]["value"] = clean_code(found["invoice_number"]["value"])
    if found["po_number"]:
        found["po_number"]["value"] = clean_po(found["po_number"]["value"])
    vendor = found["vendor_name"]
    if vendor and NOT_A_NAME.search(vendor["value"]) and not COMPANY_WORDS.search(vendor["value"]):
        found["vendor_name"] = None  # A title or badge next to a "From" / "Seller" label.
    if vendor and found["vendor_name"] and lines_by_page:
        vendor_words = " ".join(re.findall(r"[A-Za-z]{3,}", vendor["value"])).lower()
        if any(buyer in vendor_words for buyer in buyer_names(lines_by_page[min(lines_by_page)])):
            found["vendor_name"] = None  # the buyer's name, found next to a supplier heading

    forced = set()
    if found["invoice_number"] is None:
        found["invoice_number"] = fallback_invoice_number(lines)
        forced.add("invoice_number")
    if found["invoice_date"] is None:
        due_iso = found["due_date"]["value"] if found["due_date"] else None
        found["invoice_date"] = fallback_invoice_date(lines, order, due_iso)
        forced.add("invoice_date")
    if found["vendor_name"] is None:
        found["vendor_name"] = fallback_vendor_name(lines_by_page, order)
        forced.add("vendor_name")

    items = extract_line_items(
        lines_by_page, order, Decimal(found["subtotal"]["value"]) if found.get("subtotal") else None
    )
    forced |= amounts_from_items(found, items, lines, order)

    # Supplier and buyer details, references and the other amounts.
    found["buyer_name"] = find_buyer_name(
        lines, lines_by_page, order, found["vendor_name"]["value"] if found.get("vendor_name") else ""
    )
    anchors = {}
    if found.get("vendor_name"):
        anchors[id(found["vendor_name"]["line"])] = "supplier"
    if found["buyer_name"]:
        anchors[id(found["buyer_name"]["line"])] = "buyer"
    found.update(find_party_fields(lines, lines_by_page, order, anchors))
    for key in ("fbr_invoice_number", "dc_number", "gate_pass_number", "contract_number", "discount",
                "further_tax", "sales_tax_rate", "total_quantity", "amount_in_words"):
        found[key] = find_extra_field(key, lines, lines_by_page, order)
    for key in ("fbr_invoice_number", "dc_number", "gate_pass_number", "contract_number"):
        if found[key]:
            found[key]["value"] = clean_code(found[key]["value"])
    if found["amount_in_words"] is None:
        found["amount_in_words"] = fallback_amount_in_words(lines)
    if found["sales_tax_rate"] is None and found.get("tax"):
        rate = tax_rate_from_amounts(
            found["subtotal"]["value"] if found.get("subtotal") else None, found["tax"]["value"]
        )
        if rate:
            found["sales_tax_rate"] = {"value": rate, "line": found["tax"]["line"], "ambiguous": False}
            forced.add("sales_tax_rate")

    fields = {key: make_field(found.get(key), force_check=key in forced) for key in FIELD_KEYS if key != "currency"}
    fields["currency"] = {
        "value": detect_currency([line["text"] for line in lines]),
        "confidence": None,
        "needs_check": False,
        "source": None,
    }
    ordered_fields = {key: fields[key] for key in FIELD_KEYS}
    return {
        "fields": ordered_fields,
        "line_items": reconcile_line_items(items, ordered_fields["subtotal"]["value"]),
        "date_order": order,
    }


def amounts_from_items(found, items, lines, order):
    """
    When the summary was not read (cut off, or a scan turned on its side), the "subtotal" found can
    be one item's own amount. With two or more items that each add up (quantity x rate = amount), their
    sum is the subtotal; tax and total are kept only when the page shows a total that fits. Returns the
    fields that the user should check.
    """
    subtotal = found.get("subtotal")
    if len(items) < 2 or not subtotal:
        return set()
    amounts = [Decimal(item["amount"]) for item in items if item["amount"]]
    value = Decimal(subtotal["value"])
    # The summary was one item's own row, or it was cut off at the page edge ("53,12" for 53,120.01).
    from_item_row = any(close(value, amount) for amount in amounts)
    cut_off = value > 0 and sum(amounts) > 50 * value
    if len(amounts) != len(items) or close(sum(amounts), value) or not (from_item_row or cut_off):
        return set()
    if not all(item["quantity"] and item["rate"] and close(Decimal(item["quantity"]) * Decimal(item["rate"]),
                                                           Decimal(item["amount"])) for item in items):
        return set()
    items_total = sum(amounts)
    line = subtotal["line"]
    found["subtotal"] = {"value": format_amount(items_total), "line": line, "ambiguous": False}
    printed = money_amounts(lines, order)
    for total in printed:
        tax = total - items_total
        if plausible_tax(items_total, tax) and any(close(tax, amount) for amount in printed):
            found["tax"] = {"value": format_amount(tax), "line": line, "ambiguous": False}
            found["total_due"] = {"value": format_amount(total), "line": line, "ambiguous": False}
            return {"subtotal"}
    found["tax"] = None
    found["total_due"] = None
    return {"subtotal"}


# ---------------------------------------------------------------------------
# Where each invoice starts in a document with several invoices
# ---------------------------------------------------------------------------
# "Page 2", "Page 2 of 3", "Page No. 2", "page 2/3": the first number is the page.
PAGE_MARKER = re.compile(r"\bpage\s*(?:no\.?\s*)?[:#]?\s*(\d{1,3})(?:\s*(?:of|/)\s*\d{1,3})?(?!\d)", re.IGNORECASE)


INVOICE_WORDS = re.compile(r"invoice|inv[a-z]{1,3}ce|sales\s*tax|\btotal\b|amount", re.IGNORECASE)


def needs_more_detail(page_result):
    """
    True when a page looks like an invoice but its invoice number, date or amounts could not be
    read completely: small print at a low DPI often reads as nonsense, and a sharper image of the
    page usually fixes it. Supporting documents and pages that continue a table are left alone.
    """
    lines = page_result.get("lines") or []
    if page_result.get("status") != "completed" or len(lines) < 5:
        return False
    if not INVOICE_WORDS.search(" ".join(str(line.get("text") or "") for line in lines)):
        return False
    signals = page_signals(page_result)
    if signals["attachment"] or signals["continues_table"]:
        return False
    fields = extract_invoice_data([page_result])["fields"]
    if not (fields["invoice_number"]["value"] and fields["invoice_date"]["value"] and fields["total_due"]["value"]):
        return True
    amounts = [fields[key]["value"] for key in ("subtotal", "tax", "total_due")]
    return not (all(amounts) and close(Decimal(amounts[0]) + Decimal(amounts[1]), Decimal(amounts[2])))


# Supporting documents filed behind an invoice.
ATTACHMENT_TITLE = re.compile(
    r"\b(?:undertaking|goods\s*declaration|bill\s*of\s*(?:export|entry|lading)|delivery\s*challan|"
    r"gate\s*pass|packing\s*list|certificate|inspection\s*report|weighment)\b",
    re.IGNORECASE,
)


def normalize_code(value):
    """'INV-0012 ' and 'inv 0012' compare equal."""
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def page_signals(page_result):
    """What one page tells about where an invoice starts: its invoice number, page marker and own data."""
    fields = extract_invoice_data([page_result])["fields"]
    marker = None
    for line in page_result.get("lines") or []:
        match = PAGE_MARKER.search(str(line.get("text") or ""))
        if match:
            marker = int(match.group(1))
            break
    top_texts = [
        str(line.get("text") or "") for line in page_result.get("lines") or []
        if line.get("box") and float(line["box"][1]) < 0.4 * float(page_result.get("image_height") or 1e9)
    ]
    attachment = (
        any(ATTACHMENT_TITLE.search(text) for text in top_texts)
        and not any(re.search(r"\binvoice\b", text, re.IGNORECASE) and len(text.split()) <= 4 for text in top_texts)
    )
    height = float(page_result.get("image_height") or 1e9)
    head = [
        str(line.get("text") or "") for line in page_result.get("lines") or []
        if line.get("box") and float(line["box"][1]) < 0.1 * height
    ]
    head_amounts = sum(
        1 for text in head for amount in find_amounts(text, config.INVOICE_DATE_ORDER) if amount >= 10
    )
    continues_table = head_amounts >= 3 and not any(
        # a heading such as "SALES TAX INVOICE", not a label such as "FBR Invoice No. :"
        re.search(r"invoice|inv[a-z]{1,3}ce|quotation", text, re.IGNORECASE)
        and not re.search(r"fbr|digital|pral|\bno\b|#|:", text, re.IGNORECASE)
        for text in top_texts
    )
    return {
        "invoice_number": normalize_code(fields["invoice_number"]["value"]),
        "has_own_data": bool(fields["invoice_date"]["value"] or fields["total_due"]["value"]),
        "page_marker": marker,
        "attachment": attachment,
        "continues_table": continues_table,
    }


def starts_new_invoice(signals, current_number):
    """
    Rules, in order:
    1. A supporting document without an invoice heading (undertaking, goods declaration,
       delivery challan ...) belongs to the invoice before it, whatever its own page numbers say.
    2. A page marker decides: "Page 1" starts an invoice, "Page 2 of 3" continues one.
    3. An invoice number decides: the same number continues the invoice, a different one starts a new one.
    4. A page with its own date or total is a new invoice (the number may not have been read).
    5. A page with none of these (terms, blank back side) belongs to the invoice before it.
    """
    if signals.get("attachment") or signals.get("continues_table"):
        return False  # a supporting document, or the items table going on from the page before
    if signals["page_marker"] is not None:
        return signals["page_marker"] <= 1
    if signals["invoice_number"]:
        return signals["invoice_number"] != current_number
    return signals["has_own_data"]


def split_into_invoices(pages, forced=None):
    """
    Group pages into invoices.

    pages:  [(page_number, signals or None)] in page order; None = the page has no OCR text yet.
    forced: {page_number: True (starts an invoice) | False (continues the one before)}; wins over the rules.
    Returns a list of invoices, each a list of page numbers.
    """
    forced = forced or {}
    groups = []
    current_number = None
    previous_unread = False
    for page_number, signals in pages:
        if not groups:
            starts = True
        elif page_number in forced:
            starts = bool(forced[page_number])
        elif signals is None or previous_unread:
            starts = True  # An unread page is its own invoice until OCR reads it.
        else:
            starts = starts_new_invoice(signals, current_number)

        number = signals["invoice_number"] if signals else ""
        if starts:
            groups.append([page_number])
            current_number = number or None
        else:
            groups[-1].append(page_number)
            current_number = current_number or number or None
        previous_unread = signals is None
    return groups
