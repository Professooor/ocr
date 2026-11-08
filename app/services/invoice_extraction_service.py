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

FIELD_KEYS = (
    "vendor_name",
    "invoice_number",
    "invoice_date",
    "due_date",
    "po_number",
    "currency",
    "subtotal",
    "tax",
    "total_due",
)

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
    r"(?<![A-Za-z])(" + MONTH_NAMES + r")[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})(?!\d)",
    re.IGNORECASE,
)


def make_iso_date(year, month, day):
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
        else:
            cleaned = "".join(parts)
    elif dots > 1:
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
        value = parse_number_token(match.group())
        if value is None:
            continue
        if NEGATIVE_PREFIX.search(masked[:start]):
            value = -value
        amounts.append(value)
    return amounts


CURRENCY_CODES = ("USD", "EUR", "GBP", "PKR", "AED", "SAR", "INR", "CAD", "AUD", "JPY", "CNY", "CHF")
CURRENCY_SYMBOLS = (("₹", "INR"), ("€", "EUR"), ("£", "GBP"), ("Rs", "PKR"), ("$", "USD"))


def detect_currency(texts):
    joined = "\n".join(texts)
    counts = {code: len(re.findall(r"\b" + code + r"\b", joined)) for code in CURRENCY_CODES}
    best = max(counts, key=counts.get)
    if counts[best] > 0:
        return best
    for symbol, code in CURRENCY_SYMBOLS:
        if re.search(r"(?<![A-Za-z])" + re.escape(symbol) + (r"(?![A-Za-z])" if symbol == "Rs" else ""), joined):
            return code
    return ""


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

    if kind == "code":
        masked = mask_dates(text, order)
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_\-/.#]*", masked):
            token = token.strip(".-/#_")
            if len(token) >= 2 and re.search(r"\d", token):
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
FIELD_DEFS = [
    {
        "key": "invoice_number",
        "kind": "code",
        "groups": [{
            "patterns": [
                r"invoice\s*(?:number|no\b\.?|num\b\.?|#|id\b)",
                r"\binv\.?\s*(?:number|no\b\.?|#)",
                r"invoice\s*[:#]",
                r"bill\s*(?:number|no\b\.?|#)",
                r"document\s*(?:number|no\b\.?|#)",
            ],
            "below": True,
            "pick": "first",
        }],
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
                "patterns": [r"(?<![A-Za-z])(?<!due\s)(?<!due:\s)date[d]?(?![A-Za-z])(?!\s*due)"],
                "below": True,
                "pick": "first",
            },
        ],
    },
    {
        "key": "po_number",
        "kind": "code",
        "reject": r"p\.?\s?o\.?\s*box",
        "groups": [{
            "patterns": [
                r"p\.?\s?o\.?\s*(?:number|no\b\.?|num\b\.?|#)",
                r"purchase\s*order\s*(?:number|no\b\.?|num\b\.?|#)?",
                r"\bP\.?O\.?\s*[:#]",
            ],
            "below": True,
            "pick": "first",
        }],
    },
    {
        "key": "subtotal",
        "kind": "amount",
        "groups": [{"patterns": [r"sub[\s\-]?total"], "below": False, "pick": "last"}],
    },
    {
        "key": "tax",
        "kind": "amount",
        "groups": [{
            "patterns": [
                r"(?<![A-Za-z])(?:sales\s*)?tax(?![A-Za-z])(?!\s*(?:id|no\b|number|#|reg|invoice))",
                r"\b(?:vat|gst|hst)\b(?!\s*(?:id|no\b|number|#|reg))",
            ],
            "below": False,
            "pick": "last",
        }],
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
                "patterns": [r"grand\s*total", r"total\s*amount", r"invoice\s*total"],
                "below": False,
                "pick": "first",
            },
            {
                "patterns": [r"(?<![A-Za-z])(?<!sub-)(?<!sub )total(?![A-Za-z])"],
                "below": False,
                "pick": "last",
            },
        ],
    },
    {
        "key": "vendor_name",
        "kind": "text",
        "groups": [{
            "patterns": [r"^(?:vendor|supplier|seller|payee|sold\s*by|from|bill(?:ed)?\s*from|remit\s*to)\b\s*(?:name)?\s*[:\-]?"],
            "below": True,
            "pick": "first",
        }],
    },
]

for _definition in FIELD_DEFS:
    for _group in _definition["groups"]:
        _group["regexes"] = [re.compile(pattern, re.IGNORECASE) for pattern in _group["patterns"]]
    _definition["reject_regex"] = re.compile(_definition["reject"], re.IGNORECASE) if "reject" in _definition else None

ALL_LABEL_REGEXES = [regex for _d in FIELD_DEFS for _g in _d["groups"] for regex in _g["regexes"]]
INVOICE_TITLE = re.compile(r"^\W*(?:tax\s+)?invoice\W*$", re.IGNORECASE)


def is_label_text(text):
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
        for line in page_lines:
            line["page_width"], line["page_height"] = float(page_width), float(page_height)
        lines.extend(page_lines)
    return lines


def right_neighbors(label, page_lines):
    """Lines on the same row to the right of the label, nearest first."""
    found = []
    for other in page_lines:
        if other is label:
            continue
        same_row = abs(other["yc"] - label["yc"]) <= 0.6 * max(label["h"], other["h"])
        if same_row and other["l"] >= label["r"] - 0.5 * label["h"]:
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
    for regex in regexes:
        match = regex.search(text)
        if match:
            return match
    return None


def value_for_label(kind, label_line, match, page_lines, order, allow_below):
    """Value of a field whose label was found in label_line."""
    remainder = label_line["text"][match.end():].lstrip(" :.-#\t")
    value = parse_value(kind, remainder, order)
    if value is not None:
        return dict(value, line=label_line)

    neighbors = right_neighbors(label_line, page_lines)[:3]
    if allow_below:
        neighbors += below_neighbors(label_line, page_lines)[:3]
    for other in neighbors:
        if is_label_text(other["text"]):
            continue
        value = parse_value(kind, other["text"], order)
        if value is not None:
            return dict(value, line=other)
    return None


def find_field(definition, lines, lines_by_page, order):
    for group in definition["groups"]:
        candidates = []
        for line in lines:
            match = first_match(group["regexes"], line["text"])
            if not match:
                continue
            if definition["reject_regex"] and definition["reject_regex"].search(line["text"]):
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
    for line in lines:
        match = INVOICE_CODE.search(line["text"])
        if match:
            token = match.group().replace(" ", "")
            return {"value": token, "line": line, "ambiguous": False}
    return None


def fallback_invoice_date(lines, order, exclude_iso):
    for line in lines:
        for item in parse_dates(line["text"], order):
            if item["iso"] != exclude_iso:
                return {"value": item["iso"], "line": line, "ambiguous": item["ambiguous"]}
    return None


def looks_like_company_name(line, order):
    text = line["text"]
    letters = len(re.findall(r"[A-Za-z]", text))
    digits = len(re.findall(r"\d", text))
    if letters < 3 or digits > 0.3 * len(text):
        return False
    if INVOICE_TITLE.match(text) or is_label_text(text):
        return False
    if re.search(r"@|www\.|https?:", text, re.IGNORECASE):
        return False
    if parse_dates(text, order) or find_amounts(text, order):
        return False
    return True


def fallback_vendor_name(lines_by_page, order):
    """The largest text near the top of the first page that is not a title or label."""
    if not lines_by_page:
        return None
    first_page = min(lines_by_page)
    page_lines = lines_by_page[first_page]
    page_height = page_lines[0]["page_height"] if page_lines else 1
    candidates = [
        line for line in page_lines
        if line["t"] < 0.4 * page_height and looks_like_company_name(line, order)
    ]
    if not candidates:
        return None
    # Box heights vary by a few pixels, so take the top-most line among the larger ones.
    tallest = max(line["h"] for line in candidates)
    large_lines = [line for line in candidates if line["h"] >= 0.75 * tallest]
    chosen = min(large_lines, key=lambda line: (line["t"], line["l"]))
    return {"value": re.sub(r"\s+", " ", chosen["text"]).strip(), "line": chosen, "ambiguous": False}


# ---------------------------------------------------------------------------
# Line items table
# ---------------------------------------------------------------------------
HEADER_PATTERNS = [
    ("description", re.compile(r"description|item|items|item\s*description|details|services?|products?|particulars")),
    ("quantity", re.compile(r"qty|quantity|hours|hrs|units|no")),
    ("rate", re.compile(r"rate|price|unit\s*price|unit\s*cost|price\s*each|cost")),
    ("amount", re.compile(r"amount|total|line\s*total|ext\.?\s*(?:price|amount)|value")),
]
STOP_ROW = re.compile(
    r"^(?:sub[\s\-]?total|total|balance|amount\s*due|grand|notes?|terms|payment|thank|tax\b|discount|deposit|vat|gst|shipping|please)",
    re.IGNORECASE,
)
NUMERIC_PART = re.compile(r"^[-(−]*(?:[$€£¥₹]|Rs\.?)?\d[\d.,]*\)?$")


def header_key(text):
    cleaned = re.sub(r"[:.]+$", "", text.strip().lower())
    for key, pattern in HEADER_PATTERNS:
        if pattern.fullmatch(cleaned):
            return key
    return None


def group_rows(page_lines):
    """Group lines that are on the same visual row."""
    if not page_lines:
        return []
    heights = sorted(line["h"] for line in page_lines)
    median_height = heights[len(heights) // 2]
    rows = []
    for line in sorted(page_lines, key=lambda item: (item["yc"], item["l"])):
        if rows and abs(line["yc"] - rows[-1]["yc"]) <= 0.6 * median_height:
            rows[-1]["lines"].append(line)
            rows[-1]["yc"] = sum(item["yc"] for item in rows[-1]["lines"]) / len(rows[-1]["lines"])
        else:
            rows.append({"yc": line["yc"], "lines": [line]})
    for row in rows:
        row["lines"].sort(key=lambda item: (item["l"], item["yc"]))
    return rows


def nearest_column(line, numeric_columns):
    """Name of the numeric column closest to the line (by centre or right edge)."""
    best_key, best_distance = None, None
    for key, header in numeric_columns:
        distance = min(abs(line["xc"] - header["xc"]), abs(line["r"] - header["r"]))
        if best_distance is None or distance < best_distance:
            best_key, best_distance = key, distance
    return best_key


def peel_numbers(text, count):
    """Split 'Consulting 75.00 15 1,125.00' into ('Consulting', ['75.00', '15', '1,125.00'])."""
    parts = text.split()
    numbers = []
    while parts and len(numbers) < count and NUMERIC_PART.match(parts[-1]):
        numbers.insert(0, parts.pop())
    return " ".join(parts), numbers


def read_table_rows(rows, columns, page_width, page_number, order):
    numeric_columns = sorted(
        [(key, line) for key, line in columns.items() if key != "description"],
        key=lambda item: item[1]["xc"],
    )
    if not numeric_columns:
        return []
    numeric_keys = [key for key, _ in numeric_columns]
    boundary = min(line["l"] for _, line in numeric_columns) - 0.04 * page_width

    items = []
    for row in rows:
        if STOP_ROW.match(row["lines"][0]["text"].strip()):
            break
        texts = {"description": [], "quantity": [], "rate": [], "amount": []}
        confidences = []
        for line in row["lines"]:
            key = "description" if line["xc"] < boundary else nearest_column(line, numeric_columns)
            texts[key].append(line["text"])
            if line.get("confidence") is not None:
                confidences.append(line["confidence"])

        description = " ".join(texts["description"]).strip()
        numbers = {key: " ".join(texts[key]).strip() for key in numeric_keys}

        # Some OCR results keep a whole row in one line: split the trailing numbers off.
        if description and not any(numbers.values()):
            description, peeled = peel_numbers(description, len(numeric_keys))
            for key, token in zip(numeric_keys[len(numeric_keys) - len(peeled):], peeled):
                numbers[key] = token

        values = {}
        for key in ("quantity", "rate", "amount"):
            parsed = parse_value("quantity" if key == "quantity" else "amount", numbers.get(key, ""), order)
            values[key] = parsed["value"] if parsed else ""

        if not any(values.values()):
            # A line of text under an item is more detail for that item.
            if description and items and not texts["quantity"] + texts["rate"] + texts["amount"]:
                items[-1]["description"] = (items[-1]["description"] + " - " + description).strip(" -")
            continue

        items.append({
            "description": description,
            "quantity": values["quantity"],
            "rate": values["rate"],
            "amount": values["amount"],
            "confidence": min(confidences) if confidences else None,
            "page": page_number,
        })
        if len(items) >= MAX_LINE_ITEMS:
            break
    return items


def extract_line_items(lines_by_page, order):
    items = []
    for page_number in sorted(lines_by_page):
        page_lines = lines_by_page[page_number]
        rows = group_rows(page_lines)
        page_width = page_lines[0]["page_width"] if page_lines else 1
        for position, row in enumerate(rows):
            columns = {}
            for line in row["lines"]:
                key = header_key(line["text"])
                if key and key not in columns:
                    columns[key] = line
            if len(columns) >= 2:
                items.extend(read_table_rows(rows[position + 1:], columns, page_width, page_number, order))
                break
    return items[:MAX_LINE_ITEMS]


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
        "line_items": extract_line_items(lines_by_page, order),
        "date_order": order,
    }
