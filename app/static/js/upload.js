/* ==========================================================================
   upload.js - Upload page
   Select one document (drag and drop or browse), choose the PDF DPI,
   upload it, follow page conversion and OCR live, then open the review.
   Uses shared helpers from base.js (window.App).
   ========================================================================== */
(function () {
    "use strict";

    var POLL_INTERVAL_MS = 1200;
    var FINAL_OCR_STATUSES = ["completed", "partial", "failed", "interrupted"];
    var A4_WIDTH_POINTS = 595;
    var A4_HEIGHT_POINTS = 842;
    var MAX_STRIP_PAGES = 23;

    var form = document.getElementById("upload-form");
    var dropzone = document.getElementById("dropzone");
    var fileInput = document.getElementById("document-file");
    var filePreview = document.getElementById("file-preview");
    var filePreviewIcon = document.getElementById("file-preview-icon");
    var fileName = document.getElementById("file-name");
    var fileMeta = document.getElementById("file-meta");
    var fileClear = document.getElementById("file-clear");
    var dpiField = document.getElementById("dpi-field");
    var dpiInput = document.getElementById("pdf-dpi");
    var dpiRange = document.getElementById("pdf-dpi-range");
    var dpiHint = document.getElementById("dpi-hint");
    var dpiEstimate = document.getElementById("dpi-estimate");
    var uploadMessage = document.getElementById("upload-message");
    var uploadButton = document.getElementById("upload-button");
    var uploadButtonText = document.getElementById("upload-button-text");

    var panel = document.getElementById("processing-panel");
    var processingStatus = document.getElementById("processing-status");
    var uploadProgress = document.getElementById("upload-progress");
    var uploadProgressBar = document.getElementById("upload-progress-bar");
    var ocrProgress = document.getElementById("ocr-progress");
    var ocrProgressBar = document.getElementById("ocr-progress-bar");
    var processingMessage = document.getElementById("processing-message");
    var summary = document.getElementById("document-summary");
    var pageStrip = document.getElementById("page-strip");
    var reviewLink = document.getElementById("review-link");
    var exportButton = document.getElementById("export-excel-button");
    var exportLabel = document.getElementById("export-excel-label");

    var rules = {
        minDpi: Number(dpiInput.min),
        maxDpi: Number(dpiInput.max),
        defaultDpi: Number(dpiInput.value),
        extensions: form.getAttribute("data-allowed-extensions").split(",")
    };

    var state = {
        file: null,
        busy: false,
        startedAt: 0,
        documentId: null,
        thumbs: {}
    };

    /* ---------------- Validation (same rules as the backend) ---------------- */

    function getExtension(filename) {
        var index = filename.lastIndexOf(".");
        return index === -1 ? "" : filename.slice(index).toLowerCase();
    }

    function isPdf(file) {
        return Boolean(file) && getExtension(file.name) === ".pdf";
    }

    function validateDpi(rawValue) {
        var text = String(rawValue).trim();
        var message = "DPI must be a whole number from " + rules.minDpi + " to " + rules.maxDpi + ".";
        if (!/^[0-9]+$/.test(text)) {
            return { error: message };
        }
        var dpi = Number(text);
        if (dpi < rules.minDpi || dpi > rules.maxDpi) {
            return { error: message };
        }
        return { dpi: dpi };
    }

    function validateFile(file) {
        if (!file) {
            return "Please select a document to upload.";
        }
        if (rules.extensions.indexOf(getExtension(file.name)) === -1) {
            return "Unsupported file type. Allowed types: " +
                rules.extensions.map(function (ext) { return ext.slice(1).toUpperCase(); }).join(", ") + ".";
        }
        if (file.size === 0) {
            return "The selected file is empty.";
        }
        return null; // No file size limit.
    }

    /* ---------------- File selection ---------------- */

    function selectFile(file) {
        App.clearMessage(uploadMessage);
        var error = validateFile(file);
        if (error) {
            state.file = null;
            fileInput.value = "";
            renderFilePreview();
            App.showMessage(uploadMessage, "error", error);
            return;
        }
        state.file = file;
        renderFilePreview();
    }

    function clearFile() {
        state.file = null;
        fileInput.value = "";
        App.clearMessage(uploadMessage);
        renderFilePreview();
        fileInput.focus();
    }

    function renderFilePreview() {
        var file = state.file;
        dropzone.hidden = Boolean(file);
        filePreview.hidden = !file;
        if (file) {
            var pdf = isPdf(file);
            filePreviewIcon.textContent = "";
            filePreviewIcon.classList.toggle("is-pdf", pdf);
            filePreviewIcon.appendChild(App.icon(pdf ? "file" : "image"));
            fileName.textContent = file.name;
            fileName.title = file.name;
            fileMeta.textContent = App.formatBytes(file.size) + " · " +
                getExtension(file.name).slice(1).toUpperCase() + (pdf ? " document" : " image");
        }
        updateDpiAvailability();
    }

    /* ---------------- DPI ---------------- */

    function updateDpiAvailability() {
        var applies = !state.file || isPdf(state.file);
        dpiField.classList.toggle("is-disabled", !applies);
        dpiInput.disabled = !applies || state.busy;
        dpiRange.disabled = !applies || state.busy;
        dpiHint.textContent = applies
            ? dpiHint.getAttribute("data-default-text")
            : "DPI applies to PDF files only. Images are processed at their original resolution.";
        updateDpiDisplay();
    }

    function updateDpiDisplay() {
        var check = validateDpi(dpiInput.value);
        dpiInput.classList.toggle("is-invalid", Boolean(check.error));
        if (check.error) {
            dpiEstimate.textContent = check.error;
            return;
        }
        var dpi = check.dpi;
        dpiRange.value = String(dpi);
        var percent = ((dpi - rules.minDpi) / (rules.maxDpi - rules.minDpi)) * 100;
        dpiRange.style.setProperty("--fill", percent + "%");

        var width = Math.ceil((A4_WIDTH_POINTS * dpi) / 72);
        var height = Math.ceil((A4_HEIGHT_POINTS * dpi) / 72);
        dpiEstimate.textContent = "";
        dpiEstimate.appendChild(document.createTextNode("An A4 page becomes "));
        dpiEstimate.appendChild(App.el("strong", null, width + " × " + height + " px"));
        dpiEstimate.appendChild(document.createTextNode(dpi === rules.defaultDpi ? " (default)." : "."));
    }

    /* ---------------- Drag and drop ---------------- */

    function hasFiles(event) {
        var types = event.dataTransfer ? event.dataTransfer.types : [];
        return Array.prototype.indexOf.call(types, "Files") !== -1;
    }

    // The whole form accepts dropped files, so a new file can replace the selected one.
    function handleDragOver(event) {
        if (!hasFiles(event) || state.busy) {
            return;
        }
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
        form.classList.add("is-dragover");
    }

    function handleDragLeave(event) {
        if (!form.contains(event.relatedTarget)) {
            form.classList.remove("is-dragover");
        }
    }

    function handleDrop(event) {
        event.preventDefault();
        form.classList.remove("is-dragover");
        if (state.busy) {
            return;
        }
        var files = event.dataTransfer.files;
        if (files.length > 1) {
            App.showMessage(uploadMessage, "error", "Upload one document at a time.");
            return;
        }
        if (files.length === 1) {
            selectFile(files[0]);
        }
    }

    /* ---------------- Processing panel ---------------- */

    function setStatus(status, label) {
        processingStatus.textContent = "";
        if (status) {
            var badge = App.createStatusBadge(status);
            if (label) {
                badge.lastChild.textContent = label;
            }
            processingStatus.appendChild(badge);
        }
    }

    function setStep(name, stepState, text) {
        var step = panel.querySelector('[data-step="' + name + '"]');
        var marker = step.querySelector(".timeline__marker");
        step.classList.remove("is-active", "is-done", "is-failed");
        marker.textContent = "";
        if (stepState === "active") {
            step.classList.add("is-active");
            marker.appendChild(App.el("span", "spinner"));
        } else if (stepState === "ready") {
            step.classList.add("is-active");
            marker.appendChild(App.icon(marker.getAttribute("data-icon")));
        } else if (stepState === "done") {
            step.classList.add("is-done");
            marker.appendChild(App.icon("check"));
        } else if (stepState === "failed") {
            step.classList.add("is-failed");
            marker.appendChild(App.icon("x"));
        } else {
            marker.appendChild(App.icon(marker.getAttribute("data-icon")));
        }
        var textElement = step.querySelector("[data-step-text]");
        if (!textElement.hasAttribute("data-original")) {
            textElement.setAttribute("data-original", textElement.textContent);
        }
        textElement.textContent = text || textElement.getAttribute("data-original");
    }

    function resetPanel() {
        ["upload", "convert", "ocr", "review"].forEach(function (name) { setStep(name, null); });
        uploadProgress.hidden = true;
        uploadProgressBar.style.width = "0";
        ocrProgress.hidden = true;
        ocrProgressBar.style.width = "0";
        summary.hidden = true;
        pageStrip.textContent = "";
        state.thumbs = {};
        reviewLink.hidden = true;
        exportButton.hidden = true;
        App.clearMessage(processingMessage);
        setStatus(null);
    }

    function setBusy(busy) {
        state.busy = busy;
        uploadButton.disabled = busy;
        fileInput.disabled = busy;
        fileClear.disabled = busy;
        dropzone.classList.toggle("is-disabled", busy);
        uploadButtonText.textContent = busy ? "Processing..." : "Upload and process";
        updateDpiAvailability();
    }

    function showSummary(details) {
        App.fillFields(summary, {
            original_filename: details.original_filename,
            document_id: details.document_id,
            page_count: details.page_count,
            dpi: details.conversion.dpi ? details.conversion.dpi + " DPI" : "Original",
            elapsed: "-"
        });
        summary.querySelector('[data-field="original_filename"]').title = details.original_filename;
        summary.hidden = false;
        reviewLink.href = App.url("/review/" + encodeURIComponent(details.document_id));
    }

    function updateElapsed() {
        App.fillFields(summary, { elapsed: App.formatSeconds((Date.now() - state.startedAt) / 1000) });
    }

    function renderPageStrip(pages) {
        pageStrip.textContent = "";
        state.thumbs = {};
        pages.slice(0, MAX_STRIP_PAGES).forEach(function (page, index) {
            var figure = App.el("figure", "page-thumb is-pending");
            figure.style.animationDelay = Math.min(index * 40, 600) + "ms";
            var image = App.el("img", "page-thumb__image");
            image.src = App.url(page.image_url);
            image.alt = "Page " + page.page_number;
            image.loading = "lazy";
            var badge = App.el("span", "page-thumb__badge");
            figure.appendChild(image);
            figure.appendChild(badge);
            figure.appendChild(App.el("figcaption", "page-thumb__label", "Page " + page.page_number));
            pageStrip.appendChild(figure);
            state.thumbs[page.page_number] = figure;
        });
        if (pages.length > MAX_STRIP_PAGES) {
            pageStrip.appendChild(App.el("div", "page-strip__more", "+" + (pages.length - MAX_STRIP_PAGES) + " more"));
        }
    }

    function updatePageStrip(status) {
        if (!Array.isArray(status.pages)) {
            return; // The "start OCR" response has no per-page list; the status endpoint has.
        }
        status.pages.forEach(function (page) {
            var figure = state.thumbs[page.page_number];
            if (!figure) {
                return;
            }
            var pageState = page.ocr_status;
            if (pageState === "pending" && status.current_page === page.page_number) {
                pageState = "running";
            }
            if (figure.getAttribute("data-state") === pageState) {
                return;
            }
            figure.setAttribute("data-state", pageState);
            figure.className = "page-thumb is-" + pageState;
            var badge = figure.querySelector(".page-thumb__badge");
            badge.textContent = "";
            if (pageState === "running") {
                badge.appendChild(App.el("span", "spinner"));
            } else if (pageState === "completed") {
                badge.appendChild(App.icon("check"));
            } else if (pageState === "no_text") {
                badge.appendChild(App.icon("alert"));
            } else if (pageState === "failed") {
                badge.appendChild(App.icon("x"));
            }
            figure.title = "Page " + page.page_number + ": " + App.statusLabel(pageState === "running" ? "running" : page.ocr_status);
        });
    }

    function showOcrProgress(status) {
        var total = status.pages_total || 0;
        var done = status.pages_completed + status.pages_no_text + status.pages_failed;
        var percent = total ? Math.round((done / total) * 100) : 0;
        ocrProgress.hidden = false;
        ocrProgressBar.style.width = percent + "%";
        var text;
        if (status.status === "queued") {
            text = "Waiting for the OCR engine...";
        } else if (status.status === "running") {
            text = "Reading page " + (status.current_page || done + 1) + " of " + total + " · " + percent + "%";
        } else {
            text = done + " of " + total + " page(s) processed.";
        }
        setStep("ocr", "active", text);
        setStatus(status.status === "queued" ? "queued" : "running", status.status === "queued" ? "Queued" : "Reading text");
        updatePageStrip(status);
        updateElapsed();
    }

    function finishOcr(status) {
        setBusy(false);
        updatePageStrip(status);
        updateElapsed();
        ocrProgressBar.style.width = "100%";
        reviewLink.hidden = false;
        exportButton.hidden = status.status !== "completed" && status.status !== "partial";
        var pagesText = status.pages_total + " page(s) · " + App.formatSeconds((Date.now() - state.startedAt) / 1000);

        if (status.status === "completed") {
            setStep("ocr", "done", "Text recognised on every page." + (status.pages_no_text ? " " + status.pages_no_text + " page(s) had no readable text." : ""));
            setStep("review", "ready", "Ready. Open Document Review to compare pages and text.");
            setStatus("completed", "Completed");
            App.toast({ type: "success", title: "Document processed", message: pagesText });
        } else if (status.status === "partial") {
            setStep("ocr", "failed", status.pages_failed + " page(s) failed. You can retry them from Document Review.");
            setStep("review", "ready", "Open Document Review to see the results.");
            setStatus("partial", "Partly completed");
            App.toast({ type: "warning", title: "OCR partly completed", message: status.pages_failed + " page(s) failed." });
        } else {
            setStep("ocr", "failed", status.error || "OCR failed.");
            setStatus("failed", "Failed");
            App.showMessage(processingMessage, "error", status.error || "OCR failed. See Document Review for details.");
            App.toast({ type: "error", title: "OCR failed", message: status.error || "See Document Review for details." });
        }
        reviewLink.focus({ preventScroll: true });
    }

    function failProcessing(step, message) {
        setBusy(false);
        setStep(step, "failed", message);
        setStatus("failed", "Failed");
        App.showMessage(processingMessage, "error", message);
        App.toast({ type: "error", title: "Processing stopped", message: message });
    }

    async function pollOcrStatus(documentId) {
        if (state.documentId !== documentId) {
            return; // A newer upload started.
        }
        var status;
        try {
            status = await App.apiRequest("/api/documents/" + encodeURIComponent(documentId) + "/ocr/status");
        } catch (error) {
            reviewLink.hidden = false;
            failProcessing("ocr", error.message);
            return;
        }
        if (FINAL_OCR_STATUSES.indexOf(status.status) !== -1) {
            finishOcr(status);
        } else {
            showOcrProgress(status);
            window.setTimeout(function () { pollOcrStatus(documentId); }, POLL_INTERVAL_MS);
        }
    }

    /* ---------------- Excel export ---------------- */

    async function exportExcel() {
        if (!state.documentId) {
            return;
        }
        exportButton.disabled = true;
        exportLabel.textContent = "Exporting…";
        try {
            var filename = await App.downloadFile(
                "/api/documents/" + encodeURIComponent(state.documentId) + "/export/excel",
                "document_ocr.xlsx"
            );
            App.toast({ type: "success", title: "Excel file downloaded", message: filename, timeout: 4000 });
        } catch (error) {
            App.toast({ type: "error", title: "Export failed", message: error.message });
        } finally {
            exportButton.disabled = false;
            exportLabel.textContent = "Export to Excel";
        }
    }

    /* ---------------- Upload with progress ---------------- */

    /** POST the form with XMLHttpRequest so the upload percentage can be shown. */
    function uploadDocument(formData, onProgress) {
        return new Promise(function (resolve, reject) {
            var request = new XMLHttpRequest();
            request.open("POST", App.url("/api/documents/upload"));
            request.setRequestHeader("Accept", "application/json");
            request.upload.addEventListener("progress", function (event) {
                if (event.lengthComputable) {
                    onProgress(event.loaded / event.total);
                }
            });
            request.addEventListener("load", function () {
                var payload = null;
                try {
                    payload = JSON.parse(request.responseText);
                } catch (error) {
                    payload = null;
                }
                if (request.status >= 200 && request.status < 300 && payload && payload.success) {
                    resolve(payload.data);
                } else {
                    reject(App.apiError(payload, request.status));
                }
            });
            request.addEventListener("error", function () {
                reject(new Error("Could not reach the server. Check that the application is running."));
            });
            request.send(formData);
        });
    }

    async function handleSubmit(event) {
        event.preventDefault();
        if (state.busy) {
            return;
        }
        App.clearMessage(uploadMessage);

        var file = state.file;
        var fileError = validateFile(file);
        if (fileError) {
            App.showMessage(uploadMessage, "error", fileError);
            fileInput.focus();
            return;
        }

        var formData = new FormData();
        formData.append("file", file);
        if (isPdf(file)) {
            var dpiCheck = validateDpi(dpiInput.value);
            if (dpiCheck.error) {
                dpiInput.classList.add("is-invalid");
                App.showMessage(uploadMessage, "error", dpiCheck.error);
                dpiInput.focus();
                return;
            }
            formData.append("dpi", String(dpiCheck.dpi));
        }

        setBusy(true);
        resetPanel();
        state.startedAt = Date.now();
        state.documentId = null;
        setStatus("running", "Uploading");
        setStep("upload", "active", "Uploading " + App.formatBytes(file.size) + "...");
        uploadProgress.hidden = false;
        if (window.matchMedia("(max-width: 1000px)").matches) {
            panel.scrollIntoView({ behavior: "smooth", block: "start" });
        }

        var uploaded;
        try {
            uploaded = await uploadDocument(formData, function (fraction) {
                uploadProgressBar.style.width = Math.round(fraction * 100) + "%";
                if (fraction >= 1) {
                    setStep("upload", "done", "Uploaded and validated.");
                    setStep("convert", "active", isPdf(file) ? "Converting pages to images..." : "Preparing the page image...");
                    setStatus("running", "Converting");
                } else {
                    setStep("upload", "active", "Uploading... " + Math.round(fraction * 100) + "%");
                }
            });
        } catch (error) {
            uploadProgress.hidden = true;
            setStep("convert", null);
            failProcessing("upload", error.message);
            return;
        }

        uploadProgress.hidden = true;
        state.documentId = uploaded.document_id;
        setStep("upload", "done", "Uploaded and validated.");
        setStep("convert", "done", uploaded.page_count + " page image(s) created" +
            (uploaded.conversion.dpi ? " at " + uploaded.conversion.dpi + " DPI." : " at original resolution."));
        showSummary(uploaded);
        updateElapsed();

        try {
            var pages = await App.apiRequest("/api/documents/" + encodeURIComponent(uploaded.document_id) + "/pages");
            renderPageStrip(pages.pages);
        } catch (error) {
            pageStrip.textContent = ""; // Thumbnails are optional; OCR continues.
        }

        setStep("ocr", "active", "Starting OCR...");
        try {
            var ocrStatus = await App.apiRequest(
                "/api/documents/" + encodeURIComponent(uploaded.document_id) + "/ocr",
                { method: "POST" }
            );
            showOcrProgress(ocrStatus);
            pollOcrStatus(uploaded.document_id);
        } catch (error) {
            reviewLink.hidden = false;
            failProcessing("ocr", error.message);
        }
    }

    /* ---------------- Events ---------------- */

    form.addEventListener("submit", handleSubmit);
    fileInput.addEventListener("change", function () {
        if (fileInput.files.length > 1) {
            App.showMessage(uploadMessage, "error", "Upload one document at a time.");
            fileInput.value = "";
            return;
        }
        if (fileInput.files.length === 1) {
            selectFile(fileInput.files[0]);
        }
    });
    fileClear.addEventListener("click", clearFile);
    exportButton.addEventListener("click", exportExcel);

    dpiInput.addEventListener("input", updateDpiDisplay);
    dpiRange.addEventListener("input", function () {
        dpiInput.value = dpiRange.value;
        updateDpiDisplay();
    });

    ["dragenter", "dragover"].forEach(function (name) {
        form.addEventListener(name, handleDragOver);
    });
    form.addEventListener("dragleave", handleDragLeave);
    form.addEventListener("drop", handleDrop);

    // A file dropped outside the form must not make the browser leave the page.
    window.addEventListener("dragover", function (event) {
        if (hasFiles(event)) {
            event.preventDefault();
        }
    });
    window.addEventListener("drop", function (event) {
        if (hasFiles(event) && !form.contains(event.target)) {
            event.preventDefault();
        }
    });

    resetPanel();
    setStatus("not_started", "Waiting for a document");
    renderFilePreview();
})();
