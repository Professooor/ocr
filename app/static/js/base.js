/* ==========================================================================
   base.js - shared helpers for every page (exposed as window.App)
   API calls, messages, toasts, icons, formatting and navigation state.
   Page-specific code belongs in the page's own JavaScript file.
   ========================================================================== */
(function () {
    "use strict";

    var SVG_NS = "http://www.w3.org/2000/svg";
    var rootPath = document.body.getAttribute("data-root-path") || "";
    var toastRegion = document.querySelector("[data-toast-region]");

    /* ---------------- URLs and API ---------------- */

    /** Build an application URL that also works behind a reverse-proxy sub-path. */
    function url(path) {
        return rootPath + path;
    }

    /** Turn a standard API error payload into an Error with .message, .code and .status. */
    function apiError(payload, status) {
        var details = payload && payload.error ? payload.error : {};
        var error = new Error(details.message || "The request failed (HTTP " + status + ").");
        error.code = details.code || "HTTP_" + status;
        error.status = status;
        error.details = details.details || null;
        return error;
    }

    /**
     * Call a JSON API endpoint.
     * Resolves with payload.data on success, rejects with an Error otherwise.
     */
    async function apiRequest(path, options) {
        var response;
        try {
            response = await fetch(url(path), Object.assign({ headers: { Accept: "application/json" } }, options || {}));
        } catch (networkError) {
            var offline = new Error("Could not reach the server. Check that the application is running.");
            offline.code = "NETWORK_ERROR";
            offline.status = 0;
            throw offline;
        }

        var payload = null;
        try {
            payload = await response.json();
        } catch (parseError) {
            payload = null;
        }

        if (!response.ok || !payload || payload.success !== true) {
            throw apiError(payload, response.status);
        }
        return payload.data;
    }

    /* ---------------- DOM helpers ---------------- */

    /** Create an element with an optional class name and text (text is never parsed as HTML). */
    function el(tagName, className, text) {
        var element = document.createElement(tagName);
        if (className) {
            element.className = className;
        }
        if (text !== undefined && text !== null) {
            element.textContent = String(text);
        }
        return element;
    }

    /** Create an icon from the shared SVG sprite in base.html. */
    function icon(name, className) {
        var svg = document.createElementNS(SVG_NS, "svg");
        svg.setAttribute("class", "icon" + (className ? " " + className : ""));
        svg.setAttribute("aria-hidden", "true");
        var use = document.createElementNS(SVG_NS, "use");
        use.setAttribute("href", "#i-" + name);
        svg.appendChild(use);
        return svg;
    }

    var ALERT_ICONS = { info: "info", success: "check", warning: "warning", error: "alert" };

    /** Show an inline alert. type: "info" | "success" | "warning" | "error". */
    function showMessage(element, type, text) {
        if (!element) {
            return;
        }
        // Only the alert classes change; the element keeps its own layout classes.
        element.classList.remove("alert--info", "alert--success", "alert--warning", "alert--error");
        element.classList.add("alert", "alert--" + type);
        element.textContent = "";
        element.appendChild(icon(ALERT_ICONS[type] || "info"));
        element.appendChild(el("span", null, text));
        element.hidden = false;
    }

    /** Remove a message box from the page. */
    function clearMessage(element) {
        if (!element) {
            return;
        }
        element.textContent = "";
        element.hidden = true;
    }

    /**
     * Show a toast notification in the corner of the screen.
     * options: { type, title, message, timeout }  (timeout 0 keeps it open)
     */
    function toast(options) {
        if (!toastRegion) {
            return;
        }
        var type = options.type || "info";
        var item = el("div", "toast toast--" + type);
        item.setAttribute("role", type === "error" ? "alert" : "status");

        var body = el("div");
        body.appendChild(el("p", "toast__title", options.title || ""));
        if (options.message) {
            body.appendChild(el("p", "toast__message", options.message));
        }

        var close = el("button", "toast__close");
        close.type = "button";
        close.setAttribute("aria-label", "Dismiss notification");
        close.appendChild(icon("x", "icon--sm"));

        item.appendChild(icon(ALERT_ICONS[type] || "info", "toast__icon"));
        item.appendChild(body);
        item.appendChild(close);
        toastRegion.appendChild(item);

        function dismiss() {
            if (item.classList.contains("is-leaving")) {
                return;
            }
            item.classList.add("is-leaving");
            window.setTimeout(function () { item.remove(); }, 220);
        }

        close.addEventListener("click", dismiss);
        var timeout = options.timeout === undefined ? 5000 : options.timeout;
        if (timeout > 0) {
            window.setTimeout(dismiss, timeout);
        }
    }

    /** Fill [data-field="name"] elements inside a container from an object. */
    function fillFields(container, values) {
        if (!container) {
            return;
        }
        Object.keys(values).forEach(function (name) {
            var field = container.querySelector('[data-field="' + name + '"]');
            if (!field) {
                return;
            }
            var value = values[name];
            field.textContent = "";
            if (value instanceof Node) {
                field.appendChild(value);
            } else {
                field.textContent = value === null || value === undefined || value === "" ? "-" : String(value);
            }
        });
    }

    /* ---------------- Formatting ---------------- */

    /** Show a file size in a readable unit (KB, MB, ...). */
    function formatBytes(bytes) {
        if (typeof bytes !== "number" || isNaN(bytes)) {
            return "-";
        }
        var units = ["B", "KB", "MB", "GB"];
        var value = bytes;
        var index = 0;
        while (value >= 1024 && index < units.length - 1) {
            value /= 1024;
            index += 1;
        }
        return value.toFixed(index === 0 ? 0 : 1) + " " + units[index];
    }

    /** Show an ISO date and time as readable text. */
    function formatDateTime(isoText) {
        if (!isoText) {
            return "-";
        }
        var date = new Date(isoText);
        return isNaN(date.getTime())
            ? isoText
            : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    }

    /** "just now", "5 min ago", "3 h ago", "2 days ago", or a date for older values. */
    function formatRelativeTime(isoText) {
        var date = new Date(isoText);
        if (!isoText || isNaN(date.getTime())) {
            return "-";
        }
        var seconds = Math.round((Date.now() - date.getTime()) / 1000);
        if (seconds < 45) {
            return "just now";
        }
        var minutes = Math.round(seconds / 60);
        if (minutes < 60) {
            return minutes + " min ago";
        }
        var hours = Math.round(minutes / 60);
        if (hours < 24) {
            return hours + " h ago";
        }
        var days = Math.round(hours / 24);
        if (days < 7) {
            return days + (days === 1 ? " day ago" : " days ago");
        }
        return date.toLocaleDateString(undefined, { dateStyle: "medium" });
    }

    /** Show a duration in seconds as readable text. */
    function formatSeconds(seconds) {
        if (typeof seconds !== "number" || isNaN(seconds)) {
            return "-";
        }
        if (seconds < 60) {
            return seconds.toFixed(seconds < 10 ? 1 : 0) + " s";
        }
        var minutes = Math.floor(seconds / 60);
        return minutes + " min " + Math.round(seconds % 60) + " s";
    }

    /** Show a fraction (0 to 1) as a percentage. */
    function formatPercent(fraction) {
        return typeof fraction === "number" ? (fraction * 100).toFixed(1) + "%" : "-";
    }

    /* ---------------- Status pills ---------------- */

    var STATUS_LABELS = {
        not_started: "Not started",
        queued: "Queued",
        running: "Running",
        completed: "Completed",
        partial: "Partly completed",
        failed: "Failed",
        interrupted: "Interrupted",
        pending: "Pending",
        no_text: "No text found",
        needs_review: "Needs review",
        draft: "Draft",
        saved: "Saved",
        posted: "Posted"
    };

    /** Turn a status code from the API (for example "no_text") into text for the user. */
    function statusLabel(status) {
        return STATUS_LABELS[status] || status || "-";
    }

    /** Create a status pill element: coloured dot + label. */
    function createStatusBadge(status) {
        var safeStatus = String(status || "unknown").replace(/[^a-z_]/g, "");
        var badge = el("span", "status status--" + safeStatus);
        badge.appendChild(el("span", "status__dot"));
        badge.appendChild(document.createTextNode(statusLabel(status)));
        return badge;
    }

    /* ---------------- Clipboard ---------------- */

    /** Copy text to the clipboard. Resolves true on success. */
    async function copyText(text) {
        try {
            if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(text);
                return true;
            }
        } catch (error) {
            // Fall back to the selection method below.
        }
        var area = el("textarea");
        area.value = text;
        area.setAttribute("readonly", "");
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.appendChild(area);
        area.select();
        var copied = false;
        try {
            copied = document.execCommand("copy");
        } catch (error) {
            copied = false;
        }
        area.remove();
        return copied;
    }

    /** True when keyboard shortcuts must be ignored (user is typing in a field). */
    function isTypingTarget(target) {
        if (!target) {
            return false;
        }
        var tag = target.tagName;
        return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
    }

    /* ---------------- Navigation ---------------- */

    /** Highlight the menu link of the page that is open. */
    function markActiveNavigation() {
        var page = document.body.getAttribute("data-page");
        document.querySelectorAll(".app-nav__link").forEach(function (link) {
            var isActive = link.getAttribute("data-nav") === page;
            link.classList.toggle("is-active", isActive);
            if (isActive) {
                link.setAttribute("aria-current", "page");
            }
        });
    }

    markActiveNavigation();

    /* ---------------- Colour theme (system / light / dark) ---------------- */

    var THEMES = ["system", "light", "dark"];
    var THEME_TEXT = {
        system: { icon: "monitor", label: "Colour theme: follows the system" },
        light: { icon: "sun", label: "Colour theme: light" },
        dark: { icon: "moon", label: "Colour theme: dark" }
    };

    /** Read the theme (light or dark) the user chose earlier. */
    function savedTheme() {
        try {
            var value = window.localStorage.getItem("theme");
            return THEMES.indexOf(value) !== -1 ? value : "system";
        } catch (error) {
            return "system";
        }
    }

    var systemDark = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

    /** Switch between light and dark theme and remember the choice. */
    function applyTheme(theme) {
        // The CSS dark colours follow data-theme; "system" resolves to the operating system's choice.
        var resolved = theme === "system" ? (systemDark && systemDark.matches ? "dark" : "light") : theme;
        document.documentElement.setAttribute("data-theme", resolved);
        var button = document.querySelector("[data-theme-switch]");
        if (button) {
            var text = THEME_TEXT[theme];
            button.setAttribute("aria-label", text.label + " (click to change)");
            button.title = text.label;
            button.querySelector("use").setAttribute("href", "#i-" + text.icon);
        }
    }

    /** Remember and apply a theme choice. */
    function chooseTheme(theme) {
        try {
            window.localStorage.setItem("theme", theme);
        } catch (error) {
            // Private window: the choice lasts until the page is reloaded.
        }
        applyTheme(theme);
    }

    applyTheme(savedTheme());

    var themeButton = document.querySelector("[data-theme-switch]");
    if (themeButton) {
        themeButton.addEventListener("click", function () {
            chooseTheme(THEMES[(THEMES.indexOf(savedTheme()) + 1) % THEMES.length]);
        });
    }

    if (systemDark && systemDark.addEventListener) {
        systemDark.addEventListener("change", function () {
            if (savedTheme() === "system") {
                applyTheme("system");
            }
        });
    }

    window.App = {
        url: url,
        apiRequest: apiRequest,
        apiError: apiError,
        el: el,
        icon: icon,
        showMessage: showMessage,
        clearMessage: clearMessage,
        toast: toast,
        fillFields: fillFields,
        formatBytes: formatBytes,
        formatDateTime: formatDateTime,
        formatRelativeTime: formatRelativeTime,
        formatSeconds: formatSeconds,
        formatPercent: formatPercent,
        statusLabel: statusLabel,
        createStatusBadge: createStatusBadge,
        copyText: copyText,
        isTypingTarget: isTypingTarget
    };
})();
