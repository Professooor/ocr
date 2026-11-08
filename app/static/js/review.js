/* ==========================================================================
   review.js - Review page
   - Without a document ID: searchable list of documents.
   - With a document ID: invoice review workspace
       left : page thumbnails and a zoomable page viewer with the OCR text boxes
       right: form with the data read from the invoice (autofilled, editable),
              line items, and the OCR text
     Select a field, then click text on the invoice to fill it in.
   Uses shared helpers from base.js (window.App).
   ========================================================================== */
(function () {
    "use strict";

    var POLL_INTERVAL_MS = 1500;
    var ACTIVE_OCR_STATUSES = ["queued", "running"];
    var LOW_CONFIDENCE = 0.8;
    var ZOOM_STEP = 1.25;
    var ZOOM_MIN = 0.1;
    var ZOOM_MAX = 4;
    var SVG_NS = "http://www.w3.org/2000/svg";
    var DASH = "—";
    var SPLIT_MIN_LEFT = 320;
    var SPLIT_MIN_RIGHT = 420;

    var CURRENCY_SYMBOLS = {
        USD: "$", EUR: "€", GBP: "£", INR: "₹", PKR: "Rs", AED: "AED", SAR: "SAR",
        CAD: "$", AUD: "$", JPY: "¥", CNY: "¥", CHF: "CHF"
    };

    /* Fields of the invoice form. kind: text | code | date | amount | select */
    var FIELDS = {
        vendor_name: { kind: "text", label: "Vendor Name", required: true },
        invoice_number: { kind: "code", label: "Invoice Number", required: true },
        invoice_date: { kind: "date", label: "Invoice Date", required: true },
        due_date: { kind: "date", label: "Due Date", required: true },
        po_number: { kind: "code", label: "PO Number", required: false },
        currency: { kind: "select", label: "Currency", required: false },
        subtotal: { kind: "amount", label: "Subtotal", required: false },
        tax: { kind: "amount", label: "Tax", required: false },
        total_due: { kind: "amount", label: "Total Due", required: true }
    };
    var FIELD_KEYS = Object.keys(FIELDS);
    var COLUMN_KINDS = { description: "text", quantity: "quantity", rate: "amount", amount: "amount" };
    var COLUMN_LABELS = { description: "Description", quantity: "Quantity", rate: "Rate", amount: "Amount" };

    var root = document.getElementById("review-root");
    var documentId = root.getAttribute("data-document-id");

    /* Document list */
    var listSection = document.getElementById("document-list-section");
    var listMessage = document.getElementById("document-list-message");
    var searchInput = document.getElementById("document-search");
    var documentCount = document.getElementById("document-count");
    var documentTable = document.getElementById("document-table");
    var documentTableBody = document.getElementById("document-table-body");
    var documentEmpty = document.getElementById("document-empty");
    var documentEmptyTitle = document.getElementById("document-empty-title");
    var documentEmptyText = document.getElementById("document-empty-text");
    var documentEmptyAction = document.getElementById("document-empty-action");

    /* Workspace: left pane */
    var reviewSection = document.getElementById("document-review-section");
    var workspace = document.getElementById("workspace");
    var splitter = document.getElementById("splitter");
    var reviewFileIcon = document.getElementById("review-file-icon");
    var reviewFilename = document.getElementById("review-filename");
    var reviewMeta = document.getElementById("review-meta");
    var pageList = document.getElementById("page-list");
    var prevPageButton = document.getElementById("prev-page");
    var nextPageButton = document.getElementById("next-page");
    var pageTitle = document.getElementById("page-title");
    var overlayToggle = document.getElementById("overlay-toggle");
    var zoomOutButton = document.getElementById("zoom-out");
    var zoomFitButton = document.getElementById("zoom-fit");
    var zoomInButton = document.getElementById("zoom-in");
    var viewerCanvas = document.getElementById("viewer-canvas");
    var viewerPage = document.getElementById("viewer-page");
    var pageImage = document.getElementById("page-image");
    var ocrOverlay = document.getElementById("ocr-overlay");
    var pageLoading = document.getElementById("page-loading");
    var pageImageMessage = document.getElementById("page-image-message");
    var viewerHint = document.getElementById("viewer-hint");

    /* Workspace: right pane */
    var invoiceStatus = document.getElementById("invoice-status");
    var exportButton = document.getElementById("export-excel-button");
    var exportLabel = document.getElementById("export-excel-label");
    var tabDetails = document.getElementById("tab-details");
    var tabOcr = document.getElementById("tab-ocr");
    var panelDetails = document.getElementById("panel-details");
    var panelOcr = document.getElementById("panel-ocr");
    var reextractButton = document.getElementById("reextract-button");
    var formScroll = document.getElementById("form-scroll");
    var reviewMessage = document.getElementById("review-message");
    var ocrBanner = document.getElementById("ocr-banner");
    var ocrBannerText = document.getElementById("ocr-banner-text");
    var runOcrButton = document.getElementById("run-ocr-button");
    var runOcrLabel = document.getElementById("run-ocr-label");
    var invoiceForm = document.getElementById("invoice-form");
    var lineTable = document.getElementById("line-table");
    var lineBody = document.getElementById("line-body");
    var lineEmpty = document.getElementById("line-empty");
    var lineCount = document.getElementById("line-items-count");
    var lineSelectAll = document.getElementById("line-select-all");
    var addLineButton = document.getElementById("add-line");
    var deleteLinesButton = document.getElementById("delete-lines");
    var editColumnsButton = document.getElementById("edit-columns");
    var columnMenu = document.getElementById("column-menu");
    var saveState = document.getElementById("save-state");
    var saveDraftButton = document.getElementById("save-draft");
    var saveChangesButton = document.getElementById("save-changes");

    /* OCR text tab */
    var ocrStatusBadge = document.getElementById("ocr-status-badge");
    var copyButton = document.getElementById("copy-text");
    var ocrDetails = document.getElementById("ocr-details");
    var confidenceMeter = document.getElementById("confidence-meter");
    var ocrMessage = document.getElementById("ocr-message");
    var ocrLines = document.getElementById("ocr-lines");

    var fields = {};   // key -> { wrap, input, note, clear, def }
    var state = {
        documents: [],
        document: null,
        pages: [],
        data: null,           // invoice data from the API
        lines: [],            // line items being edited
        dirty: false,
        currentPage: null,
        pageRequestId: 0,
        polling: false,
        result: null,         // OCR result of the current page
        zoomMode: "fit",
        scale: 1,
        naturalWidth: 0,
        naturalHeight: 0,
        highlighted: null,
        activeInput: null,
        activeSource: null,   // { page, line } of the selected field
        sources: {},          // key -> { page, line }
        columns: { quantity: true, rate: true },
        saving: false
    };

    /* =====================================================================
       Small helpers
       ===================================================================== */

    function clamp(value, minimum, maximum) {
        return Math.min(maximum, Math.max(minimum, value));
    }

    function documentApi(path) {
        return "/api/review/documents/" + encodeURIComponent(documentId) + (path || "");
    }

    function invoiceApi(path) {
        return "/api/documents/" + encodeURIComponent(documentId) + "/invoice-data" + (path || "");
    }

    function storageGet(key) {
        try { return window.localStorage.getItem(key); } catch (error) { return null; }
    }

    function storageSet(key, value) {
        try { window.localStorage.setItem(key, value); } catch (error) { /* Storage is optional. */ }
    }

    function fileBadge(kind) {
        var badge = App.el("span", "file-badge" + (kind === "pdf" ? " is-pdf" : ""));
        badge.appendChild(App.icon(kind === "pdf" ? "file" : "image"));
        return badge;
    }

    /** Scroll an element into view inside a scrollable container (not the whole window). */
    function scrollIntoContainer(container, element) {
        var outer = container.getBoundingClientRect();
        var inner = element.getBoundingClientRect();
        var margin = 12;
        if (inner.top < outer.top) {
            container.scrollTop -= outer.top - inner.top + margin;
        } else if (inner.bottom > outer.bottom) {
            container.scrollTop += inner.bottom - outer.bottom + margin;
        }
    }

    /* ---------------- Numbers ---------------- */

    /**
     * Read a typed number: "$1,758.5" -> "1758.50". Returns "" for empty input
     * and null when it is not a number. twoPlaces pads the decimals (money).
     */
    function parseNumber(text, twoPlaces) {
        var s = String(text).replace(/\s/g, "").replace(/[^\d.,\-−]/g, "");
        if (!s) {
            return "";
        }
        var negative = /^[-−]/.test(s);
        s = s.replace(/[-−]/g, "");
        if (!/\d/.test(s)) {
            return null;
        }
        var commas = (s.match(/,/g) || []).length;
        var dots = (s.match(/\./g) || []).length;
        var cleaned;
        if (commas && dots) {
            var decimal = s.lastIndexOf(",") > s.lastIndexOf(".") ? "," : ".";
            var thousands = decimal === "," ? "." : ",";
            cleaned = s.split(thousands).join("").replace(decimal, ".");
        } else if (commas) {
            var parts = s.split(",");
            cleaned = parts.length === 2 && parts[1].length !== 3 ? parts[0] + "." + parts[1] : parts.join("");
        } else if (dots > 1) {
            cleaned = s.split(".").join("");
        } else {
            cleaned = s;
        }
        if (!/^\d+(\.\d+)?$/.test(cleaned)) {
            return null;
        }
        var pieces = cleaned.split(".");
        var whole = pieces[0].replace(/^0+(?=\d)/, "");
        var fraction = pieces[1] || "";
        if (twoPlaces) {
            while (fraction.length < 2) {
                fraction += "0";
            }
        }
        return (negative ? "-" : "") + whole + (fraction ? "." + fraction : "");
    }

    /** "1758.00" -> "1,758.00" */
    function formatMoney(normalized) {
        if (normalized === "" || normalized === null || normalized === undefined) {
            return "";
        }
        var negative = String(normalized).charAt(0) === "-";
        var pieces = String(normalized).replace("-", "").split(".");
        var whole = pieces[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
        return (negative ? "-" : "") + whole + (pieces[1] ? "." + pieces[1] : "");
    }

    function formatDateForNote(iso) {
        var match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
        if (!match) {
            return iso;
        }
        var date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
        return date.toLocaleDateString(undefined, { dateStyle: "medium" });
    }

    /* =====================================================================
       Document list
       ===================================================================== */

    function renderListSkeleton() {
        documentTableBody.textContent = "";
        for (var row = 0; row < 4; row += 1) {
            var tr = App.el("tr", "skeleton-row");
            ["70%", "55%", "30%", "50%", "50%", "20%"].forEach(function (width) {
                var cell = App.el("td");
                var bar = App.el("span", "skeleton skeleton--line");
                bar.style.width = width;
                cell.appendChild(bar);
                tr.appendChild(cell);
            });
            documentTableBody.appendChild(tr);
        }
    }

    async function loadDocumentList() {
        listSection.hidden = false;
        renderListSkeleton();
        try {
            state.documents = await App.apiRequest("/api/review/documents");
        } catch (error) {
            documentTableBody.textContent = "";
            documentTable.hidden = true;
            App.showMessage(listMessage, "error", error.message);
            return;
        } finally {
            documentTable.setAttribute("aria-busy", "false");
        }
        renderDocumentList();
    }

    function matchesSearch(item, query) {
        if (!query) {
            return true;
        }
        return String(item.original_filename || "").toLowerCase().indexOf(query) !== -1 ||
            item.document_id.indexOf(query) !== -1;
    }

    function renderDocumentList() {
        var query = searchInput.value.trim().toLowerCase();
        var visible = state.documents.filter(function (item) { return matchesSearch(item, query); });
        var total = state.documents.length;

        documentTableBody.textContent = "";
        visible.forEach(function (item, index) {
            documentTableBody.appendChild(createDocumentRow(item, index));
        });

        documentCount.textContent = query
            ? visible.length + " of " + total + " documents"
            : total + (total === 1 ? " document" : " documents");
        documentTable.hidden = visible.length === 0;
        documentEmpty.hidden = visible.length !== 0;
        if (!visible.length) {
            if (total === 0) {
                documentEmptyTitle.textContent = "No documents yet";
                documentEmptyText.textContent = "Upload a PDF or image to see it here.";
                documentEmptyAction.hidden = false;
            } else {
                documentEmptyTitle.textContent = "No matching documents";
                documentEmptyText.textContent = "Nothing matches “" + searchInput.value.trim() + "”.";
                documentEmptyAction.hidden = true;
            }
        }
    }

    function createDocumentRow(item, index) {
        var href = App.url("/review/" + encodeURIComponent(item.document_id));
        var row = App.el("tr", "doc-row");
        row.setAttribute("data-href", href);
        row.style.animationDelay = Math.min(index * 30, 300) + "ms";

        var fileCell = App.el("td");
        var fileWrap = App.el("div", "doc-row__file");
        fileWrap.appendChild(fileBadge(item.document_kind));
        var textWrap = App.el("div");
        textWrap.style.minWidth = "0";
        var link = App.el("a", "doc-row__name", item.original_filename);
        link.href = href;
        link.title = item.original_filename;
        textWrap.appendChild(link);
        textWrap.appendChild(App.el("span", "doc-row__id", item.document_id.slice(0, 12) + "…"));
        fileWrap.appendChild(textWrap);
        fileCell.appendChild(fileWrap);

        var dateCell = App.el("td", "doc-row__date", App.formatRelativeTime(item.uploaded_at));
        dateCell.title = App.formatDateTime(item.uploaded_at);
        var pagesCell = App.el("td", "doc-table__num", item.page_count);
        var ocrCell = App.el("td");
        ocrCell.appendChild(App.createStatusBadge(item.ocr.status));
        var reviewCell = App.el("td");
        if (item.data_status) {
            reviewCell.appendChild(App.createStatusBadge(item.data_status));
        } else {
            reviewCell.appendChild(App.el("span", "text-muted", DASH));
        }
        var arrowCell = App.el("td");
        arrowCell.appendChild(App.icon("chevron-right", "doc-row__chevron"));

        [fileCell, dateCell, pagesCell, ocrCell, reviewCell, arrowCell].forEach(function (cell) {
            row.appendChild(cell);
        });
        return row;
    }

    /* =====================================================================
       Workspace: loading
       ===================================================================== */

    async function loadDocument() {
        reviewSection.hidden = false;
        restoreLayout();
        var data;
        try {
            data = await App.apiRequest(documentApi());
        } catch (error) {
            reviewFilename.textContent = "Document not available";
            App.showMessage(reviewMessage, "error", error.message);
            disableForm();
            return;
        }
        applyDocument(data);
        exportButton.hidden = false;
        if (state.pages.length) {
            selectPage(Math.min(pageFromHash() || 1, state.pages.length));
        } else {
            App.showMessage(reviewMessage, "warning", "This document has no page images.");
        }
        await loadInvoiceData();
        if (ACTIVE_OCR_STATUSES.indexOf(state.document.ocr.status) !== -1) {
            startPolling();
        }
    }

    function disableForm() {
        saveDraftButton.disabled = true;
        saveChangesButton.disabled = true;
        addLineButton.disabled = true;
        invoiceForm.querySelectorAll("input, select").forEach(function (input) { input.disabled = true; });
    }

    function applyDocument(data) {
        state.document = data.document;
        state.pages = data.pages;
        var doc = state.document;
        var pdf = doc.document_kind === "pdf";
        reviewFilename.textContent = doc.original_filename;
        reviewFilename.title = doc.original_filename;
        document.title = doc.original_filename + " | Invoice Review";
        reviewFileIcon.className = "file-badge" + (pdf ? " is-pdf" : "");
        reviewFileIcon.textContent = "";
        reviewFileIcon.appendChild(App.icon(pdf ? "file" : "image"));
        reviewMeta.textContent = String(doc.file_type || "").toUpperCase() + " · " + doc.page_count +
            (doc.page_count === 1 ? " page" : " pages") + " · " +
            (doc.conversion.dpi ? doc.conversion.dpi + " DPI" : "original resolution");
        renderThumbnails();
        updateOcrBanner();
    }

    async function loadInvoiceData() {
        try {
            applyInvoiceData(await App.apiRequest(invoiceApi()));
        } catch (error) {
            App.showMessage(reviewMessage, "error", error.message);
        }
    }

    /* =====================================================================
       Invoice form
       ===================================================================== */

    function bindFields() {
        FIELD_KEYS.forEach(function (key) {
            var wrap = invoiceForm.querySelector('[data-key="' + key + '"]');
            fields[key] = {
                wrap: wrap,
                input: wrap.querySelector("input, select"),
                note: wrap.querySelector("[data-note]"),
                clear: wrap.querySelector(".input-wrap__clear"),
                def: FIELDS[key]
            };
        });
    }

    function setCurrency(code) {
        var select = fields.currency.input;
        code = code || "";
        if (code && !select.querySelector('option[value="' + code + '"]')) {
            var option = App.el("option", null, code);
            option.value = code;
            select.appendChild(option);
        }
        select.value = code;
        var symbol = CURRENCY_SYMBOLS[code] || "";
        var prefix = code ? (symbol && symbol !== code ? code + " " + symbol : code) : "";
        invoiceForm.querySelectorAll("[data-currency-prefix]").forEach(function (element) {
            element.textContent = prefix;
        });
        invoiceForm.querySelectorAll(".input-wrap--money .form-input").forEach(function (input) {
            input.style.paddingLeft = Math.max(44, 20 + prefix.length * 8) + "px";
        });
    }

    function setFieldValue(key, value) {
        var field = fields[key];
        value = value === null || value === undefined ? "" : String(value);
        if (key === "currency") {
            setCurrency(value);
        } else if (field.def.kind === "amount") {
            field.input.value = formatMoney(value);
        } else {
            field.input.value = value;
        }
        updateClearButton(key);
    }

    /** Current value of a field as stored text. Returns { value, error }. */
    function readField(key) {
        var field = fields[key];
        var text = field.input.value.trim();
        if (field.def.kind === "amount") {
            var number = parseNumber(text, true);
            if (number === null) {
                return { value: text, error: field.def.label + " must be a number such as 1,234.50." };
            }
            return { value: number, error: null };
        }
        return { value: text, error: null };
    }

    function updateClearButton(key) {
        var field = fields[key];
        if (field.clear) {
            field.clear.hidden = !field.input.value || field.input.disabled;
        }
    }

    /** Explain where the field's value comes from, under the field. */
    function updateNote(key) {
        var field = fields[key];
        if (field.wrap.classList.contains("has-error")) {
            return;
        }
        var info = (state.data && state.data.extracted && state.data.extracted[key]) || {};
        var original = info.value || "";
        var current = readField(key).value;
        var text = "";
        var kind = "";

        if (!current) {
            if (original) {
                text = "Cleared";
                kind = "missing";
            } else if (state.data && state.data.ocr_ready && key !== "currency") {
                text = "Not found on the invoice. Enter it or click it on the invoice.";
                kind = "missing";
            }
        } else if (!original) {
            text = "Entered manually";
            kind = "edited";
        } else if (current === original) {
            if (info.needs_check) {
                text = "Autofilled from invoice – please verify";
                kind = "check";
            } else {
                text = "Autofilled from invoice";
                kind = "autofilled";
            }
        } else {
            var shown = FIELDS[key].kind === "date" ? formatDateForNote(original)
                : FIELDS[key].kind === "amount" ? formatMoney(original) : original;
            text = "Edited. The invoice was read as “" + shown + "”";
            kind = "edited";
        }
        field.note.className = "field__note" + (kind ? " is-" + kind : "");
        field.note.textContent = text;
    }

    function updateAllNotes() {
        FIELD_KEYS.forEach(updateNote);
    }

    function clearFieldError(key) {
        fields[key].wrap.classList.remove("has-error");
        fields[key].input.removeAttribute("aria-invalid");
    }

    function setFieldError(key, message) {
        var field = fields[key];
        field.wrap.classList.add("has-error");
        field.input.setAttribute("aria-invalid", "true");
        field.note.className = "field__note is-error";
        field.note.textContent = message;
    }

    function setDirty(dirty) {
        state.dirty = dirty;
        saveState.classList.toggle("is-dirty", dirty);
        if (dirty) {
            saveState.textContent = "Unsaved changes";
        } else if (state.data && state.data.saved_at) {
            saveState.textContent = (state.data.status === "draft" ? "Draft saved " : "Saved ") +
                App.formatRelativeTime(state.data.saved_at);
        } else {
            saveState.textContent = "";
        }
    }

    function applyInvoiceData(data) {
        clearActiveInput();
        state.data = data;
        state.sources = {};
        FIELD_KEYS.forEach(function (key) {
            fields[key].wrap.classList.remove("has-error");
            fields[key].input.removeAttribute("aria-invalid");
            var info = data.extracted[key];
            if (info && info.source) {
                state.sources[key] = info.source;
            }
        });
        setCurrency(data.values.currency);
        FIELD_KEYS.forEach(function (key) {
            if (key !== "currency") {
                setFieldValue(key, data.values[key]);
            }
        });
        state.lines = data.line_items.map(function (item) {
            return {
                description: item.description || "",
                quantity: item.quantity || "",
                rate: item.rate || "",
                amount: item.amount || "",
                confidence: item.confidence === undefined ? null : item.confidence,
                selected: false
            };
        });
        renderLines();
        updateAllNotes();
        invoiceStatus.textContent = "";
        invoiceStatus.appendChild(App.createStatusBadge(data.status));
        reextractButton.hidden = !data.ocr_ready;
        setDirty(false);
        updateOcrBanner();
    }

    /* =====================================================================
       Line items
       ===================================================================== */

    function createCellInput(rowIndex, column, value) {
        var input = App.el("input", "cell-input" + (column === "description" ? "" : " cell-input--num"));
        input.type = "text";
        input.value = column === "rate" || column === "amount" ? formatMoney(value) : value;
        input.setAttribute("data-row", String(rowIndex));
        input.setAttribute("data-col", column);
        input.setAttribute("aria-label", COLUMN_LABELS[column] + ", line " + (rowIndex + 1));
        input.autocomplete = "off";
        input.spellcheck = false;
        if (column !== "description") {
            input.inputMode = "decimal";
        }
        return input;
    }

    function renderLines() {
        lineBody.textContent = "";
        state.lines.forEach(function (item, index) {
            var row = App.el("tr");
            if (item.selected) {
                row.classList.add("is-selected");
            }
            if (item.confidence !== null && item.confidence < LOW_CONFIDENCE) {
                row.classList.add("is-check");
                row.title = "Low OCR confidence - please verify this line";
            }

            var checkCell = App.el("td", "line-table__check");
            var check = App.el("input");
            check.type = "checkbox";
            check.checked = item.selected;
            check.setAttribute("data-select", String(index));
            check.setAttribute("aria-label", "Select line " + (index + 1));
            checkCell.appendChild(check);
            row.appendChild(checkCell);

            ["description", "quantity", "rate", "amount"].forEach(function (column) {
                var cell = App.el("td");
                cell.setAttribute("data-col", column);
                cell.appendChild(createCellInput(index, column, item[column]));
                row.appendChild(cell);
            });
            lineBody.appendChild(row);
        });
        lineEmpty.hidden = state.lines.length !== 0;
        lineCount.textContent = state.lines.length ? state.lines.length + (state.lines.length === 1 ? " item" : " items") : "";
        updateLineSelection();
    }

    function updateLineSelection() {
        var selected = state.lines.filter(function (item) { return item.selected; }).length;
        deleteLinesButton.disabled = selected === 0;
        lineSelectAll.checked = state.lines.length > 0 && selected === state.lines.length;
        lineSelectAll.indeterminate = selected > 0 && selected < state.lines.length;
    }

    function addLine() {
        state.lines.push({ description: "", quantity: "", rate: "", amount: "", confidence: null, selected: false });
        renderLines();
        setDirty(true);
        var inputs = lineBody.querySelectorAll('[data-col="description"] input');
        inputs[inputs.length - 1].focus();
    }

    function deleteSelectedLines() {
        state.lines = state.lines.filter(function (item) { return !item.selected; });
        renderLines();
        setDirty(true);
    }

    function applyColumnVisibility() {
        lineTable.classList.toggle("hide-quantity", !state.columns.quantity);
        lineTable.classList.toggle("hide-rate", !state.columns.rate);
        columnMenu.querySelectorAll("[data-column-toggle]").forEach(function (box) {
            box.checked = state.columns[box.getAttribute("data-column-toggle")];
        });
    }

    function toggleColumnMenu(show) {
        columnMenu.hidden = !show;
        editColumnsButton.setAttribute("aria-expanded", String(show));
    }

    /* =====================================================================
       Selected field and "click the invoice to fill it in"
       ===================================================================== */

    /** Describe a form input: { kind, label, key } or null when it cannot be filled from the page. */
    function describeInput(input) {
        if (!input) {
            return null;
        }
        var wrap = input.closest("[data-key]");
        if (wrap && invoiceForm.contains(input)) {
            var key = wrap.getAttribute("data-key");
            return { kind: FIELDS[key].kind, label: FIELDS[key].label, key: key };
        }
        if (input.hasAttribute("data-col")) {
            var column = input.getAttribute("data-col");
            return {
                kind: COLUMN_KINDS[column],
                label: COLUMN_LABELS[column] + " (line " + (Number(input.getAttribute("data-row")) + 1) + ")",
                column: column,
                row: Number(input.getAttribute("data-row"))
            };
        }
        return null;
    }

    function isFillable(description) {
        return Boolean(description) && description.kind !== "select";
    }

    function setActiveInput(input) {
        if (state.activeInput === input) {
            return;
        }
        clearActiveInput();
        var description = describeInput(input);
        if (!description) {
            return;
        }
        state.activeInput = input;
        if (description.key) {
            fields[description.key].wrap.classList.add("is-active");
        } else {
            input.classList.add("is-active");
        }
        workspace.classList.toggle("pick-mode", isFillable(description));
        updateHint();
        if (description.key) {
            showSource(state.sources[description.key] || null);
        } else {
            showSource(null);
        }
    }

    function clearActiveInput() {
        if (state.activeInput) {
            var description = describeInput(state.activeInput);
            if (description && description.key) {
                fields[description.key].wrap.classList.remove("is-active");
            } else {
                state.activeInput.classList.remove("is-active");
            }
        }
        state.activeInput = null;
        workspace.classList.remove("pick-mode");
        showSource(null);
        updateHint();
    }

    function updateHint() {
        var description = describeInput(state.activeInput);
        viewerHint.textContent = "";
        viewerHint.appendChild(App.icon("pointer"));
        if (isFillable(description)) {
            viewerHint.appendChild(document.createTextNode("Selected: "));
            viewerHint.appendChild(App.el("strong", null, description.label));
            viewerHint.appendChild(document.createTextNode(". Click the matching text on the invoice to fill it in (Esc to cancel)."));
        } else {
            viewerHint.appendChild(document.createTextNode(
                "Select a field on the right, then click the matching text on the invoice to fill it in."
            ));
        }
    }

    /** Mark the OCR line a field was read from. source: { page, line } or null. */
    function showSource(source) {
        state.activeSource = source;
        if (source && state.pages.length && source.page !== state.currentPage && source.page <= state.pages.length) {
            selectPage(source.page);
            return;
        }
        applySourceMark();
    }

    function applySourceMark() {
        ocrOverlay.querySelectorAll(".is-source").forEach(function (shape) { shape.classList.remove("is-source"); });
        var source = state.activeSource;
        if (!source || source.page !== state.currentPage) {
            return;
        }
        var box = ocrOverlay.querySelector('[data-line="' + source.line + '"]');
        if (box) {
            box.classList.add("is-source");
            centerBoxInViewer(box);
        }
    }

    async function fillFromLine(line, lineIndex) {
        var input = state.activeInput;
        var description = describeInput(input);
        if (!isFillable(description)) {
            return;
        }
        var parsed;
        try {
            parsed = await App.apiRequest(invoiceApi("/parse-value"), {
                method: "POST",
                headers: { Accept: "application/json", "Content-Type": "application/json" },
                body: JSON.stringify({ kind: description.kind, text: line.text })
            });
        } catch (error) {
            App.toast({ type: "warning", title: "Could not fill " + description.label, message: error.message, timeout: 4500 });
            return;
        }
        if (state.activeInput !== input) {
            return; // The user selected another field meanwhile.
        }
        if (description.key) {
            setFieldValue(description.key, parsed.value);
            state.sources[description.key] = { page: state.currentPage, line: lineIndex };
            state.activeSource = state.sources[description.key];
            applySourceMark();
            clearFieldError(description.key);
            updateNote(description.key);
        } else {
            var item = state.lines[description.row];
            item[description.column] = parsed.value;
            input.value = description.column === "rate" || description.column === "amount"
                ? formatMoney(parsed.value) : parsed.value;
            input.classList.remove("is-invalid");
        }
        setDirty(true);
        if (parsed.ambiguous) {
            App.toast({ type: "info", title: "Check the date", message: "The day and month could be read either way.", timeout: 4000 });
        }
    }

    /* =====================================================================
       Saving
       ===================================================================== */

    function collectPayload(status) {
        var errors = [];
        var values = {};
        FIELD_KEYS.forEach(function (key) {
            var result = readField(key);
            values[key] = result.value;
            if (result.error) {
                errors.push({ field: key, message: result.error });
            } else if (status === "saved" && FIELDS[key].required && !result.value) {
                errors.push({ field: key, message: FIELDS[key].label + " is required." });
            }
        });

        var lineItems = [];
        state.lines.forEach(function (item, index) {
            var clean = { description: item.description.trim(), confidence: item.confidence };
            ["quantity", "rate", "amount"].forEach(function (column) {
                var text = String(item[column] || "").trim();
                var number = parseNumber(text, column !== "quantity");
                if (number === null) {
                    errors.push({
                        field: "line_items." + index + "." + column,
                        message: COLUMN_LABELS[column] + " of line " + (index + 1) + " must be a number."
                    });
                }
                clean[column] = number === null ? text : number;
            });
            lineItems.push(clean);
        });
        return { payload: { status: status, values: values, line_items: lineItems }, errors: errors };
    }

    function showErrors(errors) {
        var first = null;
        errors.forEach(function (error) {
            var match = /^line_items\.(\d+)\.(\w+)$/.exec(error.field);
            if (match) {
                var cell = lineBody.querySelector('[data-row="' + match[1] + '"][data-col="' + match[2] + '"]');
                if (cell) {
                    cell.classList.add("is-invalid");
                    first = first || cell;
                }
            } else if (fields[error.field]) {
                setFieldError(error.field, error.message);
                first = first || fields[error.field].input;
            }
        });
        if (first) {
            tabDetails.click();
            first.scrollIntoView({ block: "center", behavior: "smooth" });
            first.focus({ preventScroll: true });
        }
    }

    function clearErrors() {
        FIELD_KEYS.forEach(clearFieldError);
        lineBody.querySelectorAll(".is-invalid").forEach(function (cell) { cell.classList.remove("is-invalid"); });
        App.clearMessage(reviewMessage);
    }

    async function save(status) {
        if (state.saving) {
            return;
        }
        clearErrors();
        var prepared = collectPayload(status);
        if (prepared.errors.length) {
            showErrors(prepared.errors);
            updateAllNotes();
            App.toast({
                type: "error",
                title: status === "saved" ? "Fill in the required fields" : "Some values are not valid",
                message: prepared.errors[0].message
            });
            return;
        }

        state.saving = true;
        saveDraftButton.disabled = true;
        saveChangesButton.disabled = true;
        try {
            var data = await App.apiRequest(invoiceApi(), {
                method: "PUT",
                headers: { Accept: "application/json", "Content-Type": "application/json" },
                body: JSON.stringify(prepared.payload)
            });
            applyInvoiceData(data);
            App.toast({
                type: "success",
                title: status === "draft" ? "Draft saved" : "Changes saved",
                message: "Used for the Excel export.",
                timeout: 3000
            });
        } catch (error) {
            if (Array.isArray(error.details) && error.details.length) {
                showErrors(error.details);
            }
            App.showMessage(reviewMessage, "error", error.message);
            App.toast({ type: "error", title: "Not saved", message: error.message });
        } finally {
            state.saving = false;
            saveDraftButton.disabled = false;
            saveChangesButton.disabled = false;
        }
    }

    async function reextract() {
        if ((state.dirty || (state.data && state.data.status !== "needs_review")) &&
            !window.confirm("Replace the current values with a fresh reading of the invoice? Your changes will be lost.")) {
            return;
        }
        reextractButton.disabled = true;
        try {
            applyInvoiceData(await App.apiRequest(invoiceApi("/extract"), { method: "POST" }));
            App.toast({ type: "success", title: "Invoice read again", timeout: 2500 });
        } catch (error) {
            App.showMessage(reviewMessage, "error", error.message);
        } finally {
            reextractButton.disabled = false;
        }
    }

    /* =====================================================================
       Pages and viewer
       ===================================================================== */

    function renderThumbnails() {
        pageList.textContent = "";
        state.pages.forEach(function (page) {
            var item = App.el("li");
            var button = App.el("button", "thumb");
            button.type = "button";
            button.setAttribute("data-page-number", String(page.page_number));
            button.setAttribute("aria-label", "Page " + page.page_number);

            var frame = App.el("span", "thumb__image");
            if (page.width && page.height) {
                frame.style.setProperty("--ratio", (page.width / page.height).toFixed(4));
            }
            var image = App.el("img");
            image.loading = "lazy";
            image.alt = "";
            image.src = App.url(page.image_url);
            frame.appendChild(image);

            var footer = App.el("span", "thumb__footer");
            footer.appendChild(App.el("span", "thumb__dot"));
            footer.appendChild(App.el("span", null, String(page.page_number)));

            button.appendChild(frame);
            button.appendChild(footer);
            item.appendChild(button);
            pageList.appendChild(item);
        });
        updateThumbnailStates();
    }

    function updateThumbnailStates() {
        var ocr = state.document.ocr;
        var running = ACTIVE_OCR_STATUSES.indexOf(ocr.status) !== -1;
        pageList.querySelectorAll(".thumb").forEach(function (button) {
            var number = Number(button.getAttribute("data-page-number"));
            var page = state.pages[number - 1];
            var pageState = page.ocr_status;
            if (pageState === "pending" && running && ocr.current_page === number) {
                pageState = "running";
            }
            button.querySelector(".thumb__dot").className = "thumb__dot is-" + pageState;
            button.title = "Page " + number + " · " + App.statusLabel(pageState);
            var active = number === state.currentPage;
            button.classList.toggle("is-active", active);
            if (active) {
                button.setAttribute("aria-current", "page");
            } else {
                button.removeAttribute("aria-current");
            }
        });
    }

    function pageFromHash() {
        var match = /^#page=([0-9]+)$/.exec(window.location.hash);
        return match ? Number(match[1]) : null;
    }

    function selectPage(pageNumber) {
        var page = state.pages[pageNumber - 1];
        if (!page) {
            return;
        }
        state.currentPage = pageNumber;
        updateThumbnailStates();
        var activeThumb = pageList.querySelector('.thumb[data-page-number="' + pageNumber + '"]');
        if (activeThumb) {
            scrollIntoContainer(pageList.parentElement, activeThumb);
        }
        if (window.history && window.history.replaceState) {
            window.history.replaceState(null, "", "#page=" + pageNumber);
        }

        pageTitle.textContent = "Page " + pageNumber + " of " + state.pages.length;
        prevPageButton.disabled = pageNumber <= 1;
        nextPageButton.disabled = pageNumber >= state.pages.length;
        loadPageImage(page);
        loadPageOcr(pageNumber);
    }

    function loadPageImage(page) {
        var source = App.url(page.image_url);
        pageImageMessage.hidden = true;
        viewerPage.hidden = false;
        state.naturalWidth = page.width;
        state.naturalHeight = page.height;
        applyZoom();
        if (pageImage.getAttribute("src") === source && pageImage.complete && pageImage.naturalWidth) {
            return;
        }
        viewerPage.classList.add("is-loading");
        pageLoading.hidden = false;
        pageImage.alt = "Page " + page.page_number + " of " + state.document.original_filename;
        pageImage.src = source;
        viewerCanvas.scrollTop = 0;
    }

    function fitScale() {
        var style = window.getComputedStyle(viewerCanvas);
        var available = viewerCanvas.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
        return clamp(available / (state.naturalWidth || 1), ZOOM_MIN, ZOOM_MAX);
    }

    function applyZoom() {
        if (!state.naturalWidth) {
            return;
        }
        if (state.zoomMode === "fit") {
            state.scale = fitScale();
        }
        viewerPage.style.width = Math.round(state.naturalWidth * state.scale) + "px";
        var percent = Math.round(state.scale * 100) + "%";
        zoomFitButton.textContent = state.zoomMode === "fit" ? "Fit" : percent;
        zoomFitButton.title = state.zoomMode === "fit" ? "Fitted to width (" + percent + ")" : "Fit to width (0)";
        zoomOutButton.disabled = state.scale <= ZOOM_MIN;
        zoomInButton.disabled = state.scale >= ZOOM_MAX;
    }

    function zoomBy(factor) {
        state.zoomMode = "manual";
        state.scale = clamp(state.scale * factor, ZOOM_MIN, ZOOM_MAX);
        applyZoom();
    }

    function zoomToFit() {
        state.zoomMode = "fit";
        applyZoom();
    }

    /* ---------------- OCR result of the current page ---------------- */

    async function loadPageOcr(pageNumber) {
        var requestId = ++state.pageRequestId;
        state.result = null;
        renderOcrLoading();
        try {
            var data = await App.apiRequest(documentApi("/pages/" + pageNumber));
            if (requestId !== state.pageRequestId) {
                return; // A newer page was selected meanwhile.
            }
            renderOcrResult(data);
        } catch (error) {
            if (requestId === state.pageRequestId) {
                resetOcrPanel();
                showOcrMessage("error", error.message);
            }
        }
    }

    function resetOcrPanel() {
        ocrDetails.hidden = true;
        ocrMessage.hidden = true;
        copyButton.hidden = true;
        ocrStatusBadge.textContent = "";
        ocrLines.textContent = "";
        clearOverlay();
    }

    function renderOcrLoading() {
        resetOcrPanel();
        var skeleton = App.el("li", "ocr-skeleton");
        for (var index = 0; index < 5; index += 1) {
            var bar = App.el("span", "skeleton skeleton--line");
            bar.style.width = (92 - index * 11) + "%";
            skeleton.appendChild(bar);
        }
        ocrLines.appendChild(skeleton);
    }

    function showOcrMessage(kind, text) {
        ocrMessage.className = "ocr-message" + (kind ? " is-" + kind : "");
        ocrMessage.textContent = "";
        ocrMessage.appendChild(App.icon(kind === "error" ? "alert" : kind === "warning" ? "warning" : "scan"));
        ocrMessage.appendChild(App.el("span", null, text));
        ocrMessage.hidden = false;
    }

    function fillStats(result) {
        var confidence = result.average_confidence;
        App.fillFields(ocrDetails, {
            line_count: result.line_count,
            average_confidence: confidence === null || confidence === undefined ? DASH : App.formatPercent(confidence),
            duration_seconds: result.duration_seconds === null || result.duration_seconds === undefined
                ? DASH : App.formatSeconds(result.duration_seconds)
        });
        confidenceMeter.style.width = ((confidence || 0) * 100) + "%";
        confidenceMeter.classList.toggle("is-low", (confidence || 0) < LOW_CONFIDENCE);
    }

    function renderOcrResult(data) {
        var result = data.ocr_result;
        var status = result ? result.status : data.page.ocr_status;
        state.result = result;
        resetOcrPanel();
        ocrStatusBadge.appendChild(App.createStatusBadge(status));

        if (!result || !result.lines.length) {
            if (result) {
                fillStats(result);
                ocrDetails.hidden = false;
            }
            var kind = status === "failed" ? "error" : status === "no_text" ? "warning" : null;
            showOcrMessage(kind, data.message || "No OCR result is available for this page.");
            return;
        }

        fillStats(result);
        ocrDetails.hidden = false;
        copyButton.hidden = false;
        result.lines.forEach(function (line, index) {
            ocrLines.appendChild(createLineItem(line, index));
        });
        renderOverlay();
        applySourceMark();
    }

    function createLineItem(line, index) {
        var item = App.el("li");
        var button = App.el("button", "ocr-line");
        button.type = "button";
        button.setAttribute("data-line", String(index));
        button.appendChild(App.el("span", "ocr-line__number", line.line_number));
        button.appendChild(App.el("span", "ocr-line__text", line.text));
        if (line.confidence !== null && line.confidence !== undefined) {
            var low = line.confidence < LOW_CONFIDENCE;
            var confidence = App.el("span", "ocr-line__confidence" + (low ? " is-low" : ""), App.formatPercent(line.confidence));
            confidence.title = low ? "Low recognition confidence - please check" : "Recognition confidence";
            button.appendChild(confidence);
        } else {
            button.appendChild(App.el("span"));
        }
        item.appendChild(button);
        return item;
    }

    /* ---------------- Text boxes on the page ---------------- */

    function clearOverlay() {
        ocrOverlay.textContent = "";
        state.highlighted = null;
    }

    function boxToPolygon(box) {
        if (!box) {
            return null;
        }
        return [[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]];
    }

    function renderOverlay() {
        clearOverlay();
        var result = state.result;
        if (!result || !result.lines.length) {
            return;
        }
        var width = result.image_width || state.naturalWidth;
        var height = result.image_height || state.naturalHeight;
        if (!width || !height) {
            return;
        }
        ocrOverlay.setAttribute("viewBox", "0 0 " + width + " " + height);
        result.lines.forEach(function (line, index) {
            var points = line.polygon || boxToPolygon(line.box);
            if (!points) {
                return;
            }
            var shape = document.createElementNS(SVG_NS, "polygon");
            shape.setAttribute("points", points.map(function (point) { return point[0] + "," + point[1]; }).join(" "));
            var low = line.confidence !== null && line.confidence !== undefined && line.confidence < LOW_CONFIDENCE;
            shape.setAttribute("class", "ocr-box" + (low ? " is-low" : ""));
            shape.setAttribute("data-line", String(index));
            var title = document.createElementNS(SVG_NS, "title");
            title.textContent = line.text + (line.confidence !== null ? " (" + App.formatPercent(line.confidence) + ")" : "");
            shape.appendChild(title);
            ocrOverlay.appendChild(shape);
        });
        ocrOverlay.classList.toggle("is-hidden", !overlayToggle.checked);
    }

    function centerBoxInViewer(box) {
        var outer = viewerCanvas.getBoundingClientRect();
        var inner = box.getBoundingClientRect();
        var visible = inner.top >= outer.top && inner.bottom <= outer.bottom &&
            inner.left >= outer.left && inner.right <= outer.right;
        if (!visible) {
            viewerCanvas.scrollBy({
                top: inner.top + inner.height / 2 - (outer.top + outer.height / 2),
                left: inner.left + inner.width / 2 - (outer.left + outer.width / 2),
                behavior: "smooth"
            });
        }
    }

    /** Highlight one recognised line on the page and in the OCR text list. */
    function highlightLine(index, reveal) {
        ocrOverlay.querySelectorAll(".is-highlighted").forEach(function (shape) { shape.classList.remove("is-highlighted"); });
        ocrLines.querySelectorAll(".is-highlighted").forEach(function (line) { line.classList.remove("is-highlighted"); });
        state.highlighted = index;
        if (index === null) {
            return;
        }
        var box = ocrOverlay.querySelector('[data-line="' + index + '"]');
        var line = ocrLines.querySelector('.ocr-line[data-line="' + index + '"]');
        if (box) {
            box.classList.add("is-highlighted");
        }
        if (line) {
            line.classList.add("is-highlighted");
        }
        if (reveal === "box" && box && overlayToggle.checked) {
            centerBoxInViewer(box);
        }
        if (reveal === "line" && line && !panelOcr.hidden) {
            scrollIntoContainer(formScroll, line);
        }
    }

    async function copyPageText() {
        if (!state.result || !state.result.text) {
            return;
        }
        var copied = await App.copyText(state.result.text);
        if (copied) {
            App.toast({
                type: "success",
                title: "Text copied",
                message: "Page " + state.currentPage + " · " + state.result.line_count + " line(s)",
                timeout: 2500
            });
        } else {
            App.toast({ type: "error", title: "Copy failed", message: "Select the text in the list and copy it." });
        }
    }

    /* =====================================================================
       Tabs
       ===================================================================== */

    function switchTab(name, moveFocus) {
        var showDetails = name === "details";
        tabDetails.setAttribute("aria-selected", String(showDetails));
        tabOcr.setAttribute("aria-selected", String(!showDetails));
        tabDetails.tabIndex = showDetails ? 0 : -1;
        tabOcr.tabIndex = showDetails ? -1 : 0;
        panelDetails.hidden = !showDetails;
        panelOcr.hidden = showDetails;
        if (moveFocus) {
            (showDetails ? tabDetails : tabOcr).focus();
        }
    }

    /* =====================================================================
       OCR run and progress
       ===================================================================== */

    function updateOcrBanner() {
        if (!state.document) {
            return;
        }
        var ocr = state.document.ocr;
        var running = ACTIVE_OCR_STATUSES.indexOf(ocr.status) !== -1;
        var remaining = (ocr.pages_pending || 0) + (ocr.pages_failed || 0);
        var ready = Boolean(state.data && state.data.ocr_ready);

        ocrBannerText.textContent = "";
        runOcrButton.hidden = true;
        if (running) {
            var done = ocr.pages_completed + ocr.pages_no_text + ocr.pages_failed;
            ocrBannerText.appendChild(App.el("span", "spinner"));
            ocrBannerText.appendChild(document.createTextNode(
                " Reading the invoice: " + done + " of " + ocr.pages_total + " page(s) finished"
            ));
            ocrBanner.hidden = false;
        } else if (remaining > 0) {
            ocrBannerText.textContent = ocr.pages_failed
                ? ocr.pages_failed + " page(s) could not be read. " + (ocr.error || "")
                : "OCR has not been run for this invoice yet. Run it to fill the form automatically, or enter the values yourself.";
            runOcrLabel.textContent = ocr.pages_failed ? "Retry failed pages" : "Run OCR";
            runOcrButton.hidden = false;
            ocrBanner.hidden = false;
        } else if (!ready && ocr.status === "interrupted") {
            ocrBannerText.textContent = ocr.error || "OCR was interrupted.";
            ocrBanner.hidden = false;
        } else {
            ocrBanner.hidden = true;
        }
    }

    async function runOcr() {
        runOcrButton.disabled = true;
        App.clearMessage(reviewMessage);
        try {
            await App.apiRequest("/api/documents/" + encodeURIComponent(documentId) + "/ocr", { method: "POST" });
            App.toast({ type: "info", title: "OCR started", message: "Pages are read one after another.", timeout: 3000 });
            startPolling();
        } catch (error) {
            App.showMessage(reviewMessage, "error", error.message);
        } finally {
            runOcrButton.disabled = false;
        }
    }

    function startPolling() {
        if (state.polling) {
            return;
        }
        state.polling = true;
        pollOcrStatus();
    }

    async function pollOcrStatus() {
        var status;
        try {
            status = await App.apiRequest("/api/documents/" + encodeURIComponent(documentId) + "/ocr/status");
        } catch (error) {
            state.polling = false;
            App.showMessage(reviewMessage, "error", error.message);
            return;
        }

        var current = state.pages[state.currentPage - 1];
        var currentStatusBefore = current ? current.ocr_status : null;
        status.pages.forEach(function (pageStatus) {
            var page = state.pages[pageStatus.page_number - 1];
            if (page) {
                page.ocr_status = pageStatus.ocr_status;
                page.ocr_error = pageStatus.ocr_error;
            }
        });
        state.document.ocr = status;
        updateThumbnailStates();
        updateOcrBanner();
        if (current && current.ocr_status !== currentStatusBefore) {
            loadPageOcr(state.currentPage);
        }

        if (ACTIVE_OCR_STATUSES.indexOf(status.status) !== -1) {
            window.setTimeout(pollOcrStatus, POLL_INTERVAL_MS);
            return;
        }
        state.polling = false;
        await onOcrFinished(status);
    }

    async function onOcrFinished(status) {
        if (status.status === "failed") {
            App.toast({ type: "error", title: "OCR failed", message: status.error || "See the OCR text tab." });
            updateOcrBanner();
            return;
        }
        if (state.dirty) {
            App.toast({
                type: "info",
                title: "OCR finished",
                message: "Your unsaved changes were kept. Use Re-read invoice to fill the form from the new text."
            });
            return;
        }
        await loadInvoiceData();
        App.toast({
            type: status.status === "partial" ? "warning" : "success",
            title: status.status === "partial" ? "OCR partly finished" : "Invoice read",
            message: "Check the autofilled values and correct anything that is wrong."
        });
    }

    /* ---------------- Excel export ---------------- */

    async function exportExcel() {
        exportButton.disabled = true;
        exportLabel.textContent = "Exporting…";
        try {
            var filename = await App.downloadFile(
                "/api/documents/" + encodeURIComponent(documentId) + "/export/excel",
                "document_ocr.xlsx"
            );
            var unsaved = state.dirty ? " (unsaved changes are not included)" : "";
            App.toast({ type: "success", title: "Excel file downloaded", message: filename + unsaved, timeout: 4500 });
        } catch (error) {
            App.toast({ type: "error", title: "Export failed", message: error.message });
        } finally {
            exportButton.disabled = false;
            exportLabel.textContent = "Export to Excel";
        }
    }

    /* =====================================================================
       Resizable divider
       ===================================================================== */

    function setLeftWidth(pixels) {
        var total = workspace.getBoundingClientRect().width;
        var width = clamp(pixels, SPLIT_MIN_LEFT, Math.max(SPLIT_MIN_LEFT, total - SPLIT_MIN_RIGHT - 10));
        workspace.style.setProperty("--left-width", Math.round(width) + "px");
        if (state.zoomMode === "fit") {
            applyZoom();
        }
        return width;
    }

    function restoreLayout() {
        var saved = Number(storageGet("reviewLeftWidth"));
        if (saved && window.matchMedia("(min-width: 1001px)").matches) {
            setLeftWidth(saved);
        }
        var columns = storageGet("reviewColumns");
        if (columns) {
            try {
                var parsed = JSON.parse(columns);
                state.columns.quantity = parsed.quantity !== false;
                state.columns.rate = parsed.rate !== false;
            } catch (error) { /* Keep defaults. */ }
        }
        applyColumnVisibility();
    }

    function startResize(event) {
        event.preventDefault();
        splitter.setPointerCapture(event.pointerId);
        splitter.classList.add("is-dragging");
        document.body.classList.add("is-resizing");
    }

    function moveResize(event) {
        if (!splitter.classList.contains("is-dragging")) {
            return;
        }
        setLeftWidth(event.clientX - workspace.getBoundingClientRect().left);
    }

    function endResize(event) {
        if (!splitter.classList.contains("is-dragging")) {
            return;
        }
        splitter.classList.remove("is-dragging");
        document.body.classList.remove("is-resizing");
        var width = parseFloat(getComputedStyle(workspace).getPropertyValue("--left-width"));
        if (width) {
            storageSet("reviewLeftWidth", String(Math.round(width)));
        }
    }

    /* =====================================================================
       Events
       ===================================================================== */

    // Document list
    searchInput.addEventListener("input", renderDocumentList);
    documentTableBody.addEventListener("click", function (event) {
        if (event.target.closest("a")) {
            return;
        }
        var row = event.target.closest(".doc-row");
        if (row) {
            window.location.href = row.getAttribute("data-href");
        }
    });

    // Form fields
    bindFields();
    FIELD_KEYS.forEach(function (key) {
        var field = fields[key];
        field.input.addEventListener("input", function () {
            clearFieldError(key);
            updateNote(key);
            updateClearButton(key);
            setDirty(true);
        });
        field.input.addEventListener("change", function () {
            if (key === "currency") {
                setCurrency(field.input.value);
            }
            clearFieldError(key);
            updateNote(key);
            setDirty(true);
        });
        field.input.addEventListener("blur", function () {
            if (field.def.kind === "amount") {
                var result = readField(key);
                if (!result.error) {
                    field.input.value = formatMoney(result.value);
                }
            }
            updateNote(key);
        });
        if (field.clear) {
            field.clear.addEventListener("click", function () {
                field.input.value = "";
                clearFieldError(key);
                updateClearButton(key);
                updateNote(key);
                setDirty(true);
                field.input.focus();
            });
        }
    });
    invoiceForm.addEventListener("submit", function (event) { event.preventDefault(); });

    // Selected field (for click-to-fill) - fields and line item cells
    document.addEventListener("focusin", function (event) {
        var target = event.target;
        if (target.matches && target.matches(".field input, .field select, .cell-input")) {
            setActiveInput(target);
        }
    });

    // Line items
    lineBody.addEventListener("input", function (event) {
        var input = event.target;
        if (!input.hasAttribute("data-col")) {
            return;
        }
        var item = state.lines[Number(input.getAttribute("data-row"))];
        item[input.getAttribute("data-col")] = input.value;
        input.classList.remove("is-invalid");
        setDirty(true);
    });
    lineBody.addEventListener("focusout", function (event) {
        var input = event.target;
        var column = input.getAttribute && input.getAttribute("data-col");
        if (column === "rate" || column === "amount") {
            var number = parseNumber(input.value, true);
            if (number !== null) {
                input.value = formatMoney(number);
                state.lines[Number(input.getAttribute("data-row"))][column] = number;
            }
        }
    });
    lineBody.addEventListener("change", function (event) {
        var box = event.target;
        if (box.hasAttribute("data-select")) {
            var index = Number(box.getAttribute("data-select"));
            state.lines[index].selected = box.checked;
            box.closest("tr").classList.toggle("is-selected", box.checked);
            updateLineSelection();
        }
    });
    lineSelectAll.addEventListener("change", function () {
        state.lines.forEach(function (item) { item.selected = lineSelectAll.checked; });
        lineBody.querySelectorAll("[data-select]").forEach(function (box) {
            box.checked = lineSelectAll.checked;
            box.closest("tr").classList.toggle("is-selected", lineSelectAll.checked);
        });
        updateLineSelection();
    });
    addLineButton.addEventListener("click", addLine);
    deleteLinesButton.addEventListener("click", deleteSelectedLines);
    editColumnsButton.addEventListener("click", function (event) {
        event.stopPropagation();
        toggleColumnMenu(columnMenu.hidden);
    });
    columnMenu.addEventListener("click", function (event) { event.stopPropagation(); });
    columnMenu.addEventListener("change", function (event) {
        var name = event.target.getAttribute("data-column-toggle");
        if (name) {
            state.columns[name] = event.target.checked;
            applyColumnVisibility();
            storageSet("reviewColumns", JSON.stringify(state.columns));
        }
    });
    document.addEventListener("click", function () { toggleColumnMenu(false); });

    // Save, export, re-read, OCR
    saveDraftButton.addEventListener("click", function () { save("draft"); });
    saveChangesButton.addEventListener("click", function () { save("saved"); });
    exportButton.addEventListener("click", exportExcel);
    reextractButton.addEventListener("click", reextract);
    runOcrButton.addEventListener("click", runOcr);

    // Tabs
    tabDetails.addEventListener("click", function () { switchTab("details", false); });
    tabOcr.addEventListener("click", function () { switchTab("ocr", false); });
    [tabDetails, tabOcr].forEach(function (tab) {
        tab.addEventListener("keydown", function (event) {
            if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
                event.preventDefault();
                switchTab(panelDetails.hidden ? "details" : "ocr", true);
            }
        });
    });
    copyButton.addEventListener("click", copyPageText);

    // Pages
    pageList.addEventListener("click", function (event) {
        var button = event.target.closest(".thumb");
        if (button) {
            selectPage(Number(button.getAttribute("data-page-number")));
        }
    });
    prevPageButton.addEventListener("click", function () { selectPage(state.currentPage - 1); });
    nextPageButton.addEventListener("click", function () { selectPage(state.currentPage + 1); });

    pageImage.addEventListener("load", function () {
        viewerPage.classList.remove("is-loading");
        pageLoading.hidden = true;
        state.naturalWidth = pageImage.naturalWidth;
        state.naturalHeight = pageImage.naturalHeight;
        applyZoom();
    });
    pageImage.addEventListener("error", function () {
        viewerPage.classList.remove("is-loading");
        pageLoading.hidden = true;
        viewerPage.hidden = true;
        pageImageMessage.hidden = false;
        pageImageMessage.textContent = "The page image could not be loaded.";
    });

    // Zoom and text boxes
    zoomInButton.addEventListener("click", function () { zoomBy(ZOOM_STEP); });
    zoomOutButton.addEventListener("click", function () { zoomBy(1 / ZOOM_STEP); });
    zoomFitButton.addEventListener("click", zoomToFit);
    overlayToggle.addEventListener("change", function () {
        ocrOverlay.classList.toggle("is-hidden", !overlayToggle.checked);
    });

    var resizeFrame = null;
    window.addEventListener("resize", function () {
        if (state.zoomMode !== "fit" || resizeFrame) {
            return;
        }
        resizeFrame = window.requestAnimationFrame(function () {
            resizeFrame = null;
            applyZoom();
        });
    });

    // Text boxes on the page: hover highlights the line; click fills the selected field
    ocrOverlay.addEventListener("mousedown", function (event) {
        if (event.target.closest("[data-line]")) {
            event.preventDefault(); // Keep the selected field focused.
        }
    });
    ocrOverlay.addEventListener("mouseover", function (event) {
        var box = event.target.closest("[data-line]");
        if (box) {
            highlightLine(Number(box.getAttribute("data-line")), null);
        }
    });
    ocrOverlay.addEventListener("mouseout", function (event) {
        var next = event.relatedTarget;
        if (!next || !next.closest || !next.closest(".ocr-box")) {
            highlightLine(null);
        }
    });
    ocrOverlay.addEventListener("click", function (event) {
        var box = event.target.closest("[data-line]");
        if (!box || !state.result) {
            return;
        }
        var index = Number(box.getAttribute("data-line"));
        var line = state.result.lines[index];
        if (isFillable(describeInput(state.activeInput))) {
            fillFromLine(line, index);
        } else {
            highlightLine(index, "line");
        }
    });

    // OCR text list
    ocrLines.addEventListener("mouseover", function (event) {
        var line = event.target.closest(".ocr-line");
        if (line) {
            highlightLine(Number(line.getAttribute("data-line")), null);
        }
    });
    ocrLines.addEventListener("mouseleave", function () { highlightLine(null); });
    ocrLines.addEventListener("click", function (event) {
        var line = event.target.closest(".ocr-line");
        if (!line) {
            return;
        }
        var index = Number(line.getAttribute("data-line"));
        if (isFillable(describeInput(state.activeInput)) && state.result) {
            fillFromLine(state.result.lines[index], index);
        } else {
            highlightLine(index, "box");
        }
    });

    // Divider
    splitter.addEventListener("pointerdown", startResize);
    splitter.addEventListener("pointermove", moveResize);
    splitter.addEventListener("pointerup", endResize);
    splitter.addEventListener("pointercancel", endResize);
    splitter.addEventListener("keydown", function (event) {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") {
            return;
        }
        event.preventDefault();
        var current = viewerCanvas.closest(".viewer-pane").getBoundingClientRect().width;
        var width = setLeftWidth(current + (event.key === "ArrowRight" ? 24 : -24));
        storageSet("reviewLeftWidth", String(Math.round(width)));
    });

    // Leaving the page with unsaved changes
    window.addEventListener("beforeunload", function (event) {
        if (state.dirty) {
            event.preventDefault();
            event.returnValue = "";
        }
    });
    reviewSection.addEventListener("click", function (event) {
        var link = event.target.closest("a[href]");
        if (link && state.dirty && !window.confirm("You have unsaved changes. Leave without saving?")) {
            event.preventDefault();
        }
    });

    // Keyboard shortcuts: arrows / Page Up / Page Down change page, + - 0 zoom, Esc clears the selected field
    document.addEventListener("keydown", function (event) {
        if (!state.pages.length || event.altKey || event.ctrlKey || event.metaKey) {
            return;
        }
        if (event.key === "Escape" && state.activeInput) {
            var active = state.activeInput;
            clearActiveInput();
            active.blur();
            return;
        }
        var role = event.target.getAttribute && event.target.getAttribute("role");
        if (App.isTypingTarget(event.target) || role === "tab" || role === "separator") {
            return;
        }
        var key = event.key;
        if ((key === "ArrowLeft" || key === "PageUp") && state.currentPage > 1) {
            event.preventDefault();
            selectPage(state.currentPage - 1);
        } else if ((key === "ArrowRight" || key === "PageDown") && state.currentPage < state.pages.length) {
            event.preventDefault();
            selectPage(state.currentPage + 1);
        } else if (key === "+" || key === "=") {
            zoomBy(ZOOM_STEP);
        } else if (key === "-") {
            zoomBy(1 / ZOOM_STEP);
        } else if (key === "0") {
            zoomToFit();
        }
    });

    updateHint();
    if (documentId) {
        loadDocument();
    } else {
        loadDocumentList();
    }
})();
