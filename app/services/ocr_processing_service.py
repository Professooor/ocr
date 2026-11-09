"""
OCR processing with PaddleOCR 3.x.

- The PaddleOCR engine is created once (lazily, on the first OCR job) and reused.
- OCR runs in one dedicated background worker thread that takes documents from
  a queue, one at a time. The browser polls the status for page-by-page
  progress. Waiting jobs do not occupy web server threads.
- Each page is processed independently: a failure on one page is recorded for
  that page and does not discard results of other pages.
- Pages that already have a successful result are skipped, so running OCR
  again only processes pending or failed pages.

Engines (OCR_ENGINE in .env):
    onnxruntime  PaddleOCR with official PP-OCR ONNX models (Windows + Oracle Linux 7.9)
    paddle       PaddleOCR with PaddlePaddle inference (Windows only, see README)
"""

import logging
import os
import queue
import threading
import time
from importlib import metadata as package_metadata

from app import config
from app.errors import ServiceError
from app.services import document_metadata_service, ocr_results_service, storage_service

logger = logging.getLogger(__name__)

SUPPORTED_ENGINES = ("onnxruntime", "paddle")

_engine = None
_engine_lock = threading.Lock()
_job_lock = threading.Lock()
_job_queue = queue.Queue()
_worker_thread = None
_worker_lock = threading.Lock()


def package_version(name):
    """Return the installed version of a Python package, or None if it is not installed."""
    try:
        return package_metadata.version(name)
    except package_metadata.PackageNotFoundError:
        return None


def get_model_dirs():
    """Model folders used by the onnxruntime engine (created by scripts/prepare_ocr_models.py)."""
    return {
        "text_detection": config.OCR_MODEL_DIR / f"{config.OCR_TEXT_DETECTION_MODEL}_onnx",
        "text_recognition": config.OCR_MODEL_DIR / f"{config.OCR_TEXT_RECOGNITION_MODEL}_onnx",
    }


def get_engine_info():
    """Describe the configured OCR engine without loading it."""
    info = {
        "engine": config.OCR_ENGINE,
        "device": config.OCR_DEVICE,
        "text_detection_model": config.OCR_TEXT_DETECTION_MODEL,
        "text_recognition_model": config.OCR_TEXT_RECOGNITION_MODEL,
        "paddleocr_version": package_version("paddleocr"),
        "loaded": _engine is not None,
    }
    if config.OCR_ENGINE == "onnxruntime":
        info["onnxruntime_version"] = package_version("onnxruntime")
        missing = [
            name for name, folder in get_model_dirs().items()
            if not (folder / "inference.onnx").is_file()
        ]
        info["models_ready"] = not missing
        info["missing_models"] = missing
    else:
        info["paddlepaddle_version"] = package_version("paddlepaddle")
        info["models_ready"] = None  # PaddleX downloads Paddle models on first use.
    return info


def create_engine():
    """Create the PaddleOCR 3.x pipeline from the configuration."""
    if config.OCR_ENGINE not in SUPPORTED_ENGINES:
        raise ServiceError(
            f"OCR_ENGINE must be one of {', '.join(SUPPORTED_ENGINES)}.", 500, "OCR_CONFIG_ERROR"
        )

    # Keep PaddleX downloads and caches inside the project model folder and skip
    # the network connectivity check that PaddleX runs at import time.
    os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(config.OCR_MODEL_DIR / "paddlex_cache"))
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

    options = {
        "text_detection_model_name": config.OCR_TEXT_DETECTION_MODEL,
        "text_recognition_model_name": config.OCR_TEXT_RECOGNITION_MODEL,
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "device": config.OCR_DEVICE,
        "engine": config.OCR_ENGINE,
    }
    if config.OCR_ENGINE == "onnxruntime":
        model_dirs = get_model_dirs()
        for name, folder in model_dirs.items():
            if not (folder / "inference.onnx").is_file():
                raise ServiceError(
                    f"OCR model files are missing ({folder.name}). "
                    "Run scripts/prepare_ocr_models.py first.",
                    503,
                    "OCR_MODELS_MISSING",
                )
        options["text_detection_model_dir"] = str(model_dirs["text_detection"])
        options["text_recognition_model_dir"] = str(model_dirs["text_recognition"])
        options["engine_config"] = {"intra_op_num_threads": config.OCR_CPU_THREADS}
    else:
        options["enable_mkldnn"] = config.OCR_ENABLE_MKLDNN
        options["cpu_threads"] = config.OCR_CPU_THREADS

    try:
        from paddleocr import PaddleOCR
    except ImportError as error:
        logger.exception("PaddleOCR could not be imported")
        raise ServiceError("PaddleOCR is not installed correctly.", 503, "OCR_UNAVAILABLE") from error

    started = time.time()
    engine = PaddleOCR(**options)
    logger.info(
        "PaddleOCR %s loaded (engine=%s, det=%s, rec=%s) in %.1fs",
        package_version("paddleocr"),
        config.OCR_ENGINE,
        config.OCR_TEXT_DETECTION_MODEL,
        config.OCR_TEXT_RECOGNITION_MODEL,
        time.time() - started,
    )
    return engine


def get_engine():
    """Return the shared PaddleOCR engine. It is created on first use, so the model is loaded only once."""
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = create_engine()
        return _engine


def engine_summary():
    """Short description of the OCR engine and models. It is stored with every OCR result."""
    return {
        "name": "PaddleOCR",
        "version": package_version("paddleocr"),
        "engine": config.OCR_ENGINE,
        "text_detection_model": config.OCR_TEXT_DETECTION_MODEL,
        "text_recognition_model": config.OCR_TEXT_RECOGNITION_MODEL,
    }


def queue_ocr(document_id):
    """Mark a document as queued for OCR. Return True if a job must be started."""
    should_run = {"value": False}

    def mark_queued(metadata):
        status = metadata.get("ocr", {}).get("status")
        if status in ("queued", "running"):
            raise ServiceError("OCR is already running for this document.", 409, "OCR_IN_PROGRESS")
        pages = metadata.get("pages", [])
        if metadata.get("conversion", {}).get("status") != "completed" or not pages:
            raise ServiceError("Page images are not ready for OCR.", 409, "PAGES_NOT_READY")

        pages_to_process = [p for p in pages if p.get("ocr_status") not in ("completed", "no_text")]
        if not pages_to_process:
            return
        for page in pages_to_process:
            page["ocr_status"] = "pending"
            page["ocr_error"] = None
        metadata["ocr"] = {
            "status": "queued",
            "engine": config.OCR_ENGINE,
            "models": [config.OCR_TEXT_DETECTION_MODEL, config.OCR_TEXT_RECOGNITION_MODEL],
            "queued_at": storage_service.utc_now(),
            "started_at": None,
            "completed_at": None,
            "current_page": None,
            "error": None,
        }
        should_run["value"] = True

    metadata = document_metadata_service.update_metadata(document_id, mark_queued)
    return should_run["value"], document_metadata_service.build_ocr_summary(metadata)


def finish_job(document_id, error_message=None):
    """Set the final OCR status of the document: completed, partial (some pages failed) or failed."""
    def apply_final_status(metadata):
        statuses = [page.get("ocr_status") for page in metadata.get("pages", [])]
        failed = statuses.count("failed")
        if error_message or (statuses and failed == len(statuses)):
            final_status = "failed"
        elif failed:
            final_status = "partial"
        else:
            final_status = "completed"
        metadata["ocr"]["status"] = final_status
        metadata["ocr"]["completed_at"] = storage_service.utc_now()
        metadata["ocr"]["current_page"] = None
        metadata["ocr"]["error"] = error_message

    return document_metadata_service.update_metadata(document_id, apply_final_status)


def mark_pending_pages_failed(document_id, message):
    """Mark every page that is still pending as failed, with the given message (used when the OCR engine cannot start)."""
    def apply(metadata):
        for page in metadata.get("pages", []):
            if page.get("ocr_status") == "pending":
                page["ocr_status"] = "failed"
                page["ocr_error"] = message

    document_metadata_service.update_metadata(document_id, apply)


def process_page(engine, document_id, page):
    """Run OCR for one page and store the result. Never raises."""
    result = read_page(engine, document_id, page)
    ocr_results_service.save_page_result(document_id, page["page_number"], result)
    return result


def read_page(engine, document_id, page, image_path=None):
    """Run OCR for one page image and return the result (not stored). Never raises."""
    image_path = image_path or storage_service.get_pages_dir(document_id) / page["image_filename"]
    started = time.time()
    try:
        if not image_path.is_file():
            raise FileNotFoundError(f"Page image not found: {image_path.name}")
        raw_results = engine.predict(str(image_path))
        raw_result = raw_results[0] if raw_results else {}
        result = ocr_results_service.build_page_result(
            document_id, page, raw_result, time.time() - started, engine_summary()
        )
    except Exception as error:
        logger.exception("Document %s page %s: OCR failed", document_id, page["page_number"])
        message = (
            "The page image file is missing."
            if isinstance(error, FileNotFoundError)
            else f"OCR failed for this page ({type(error).__name__})."
        )
        result = ocr_results_service.build_failed_result(document_id, page, message, engine_summary())
    return result


# ---------------------------------------------------------------------------
# Detail pass: read unclear pages again from a higher-resolution image
# ---------------------------------------------------------------------------
_detail_check = None


def set_detail_check(check):
    """
    check(document_id, page_result) -> True when the content of a page could not be read
    completely, so the page should be rendered at OCR_DETAIL_DPI and read again.
    """
    global _detail_check
    _detail_check = check


def pages_needing_detail(document_id, metadata):
    """Page numbers of a PDF whose result is incomplete and whose image is below OCR_DETAIL_DPI."""
    if not config.OCR_DETAIL_DPI or _detail_check is None or metadata.get("document_kind") != "pdf":
        return []
    numbers = []
    for page in metadata.get("pages", []):
        if page.get("ocr_status") != "completed" or (page.get("dpi") or 0) >= config.OCR_DETAIL_DPI:
            continue
        result = ocr_results_service.read_page_result_file(document_id, page["page_number"])
        if not result:
            continue
        try:
            if _detail_check(document_id, result):
                numbers.append(page["page_number"])
        except Exception:
            logger.exception("Document %s page %s: detail check failed", document_id, page["page_number"])
    return numbers


def read_page_in_detail(engine, document_id, page):
    """
    Render the page at OCR_DETAIL_DPI and read it again. The new image and result replace the old
    ones only when the new reading succeeded and found at least as much text. Returns True if replaced.
    """
    from app.services import pdf_processing_service  # imported here: the PDF module is optional for images

    number = page["page_number"]
    pages_dir = storage_service.get_pages_dir(document_id)
    temporary = pages_dir / f"detail_{page['image_filename']}"
    try:
        rendered = pdf_processing_service.render_page_image(document_id, number, config.OCR_DETAIL_DPI, temporary)
    except ServiceError as error:
        logger.warning("Document %s page %s: not rendered for the detail pass: %s", document_id, number, error.message)
        storage_service.remove_path(temporary)
        return False

    detailed_page = dict(page, **rendered)
    old = ocr_results_service.read_page_result_file(document_id, number) or {}
    result = read_page(engine, document_id, detailed_page, image_path=temporary)
    if result.get("status") != "completed" or result.get("line_count", 0) < 0.8 * (old.get("line_count") or 0):
        storage_service.remove_path(temporary)
        logger.info("Document %s page %s: detail pass kept the first reading", document_id, number)
        return False

    os.replace(temporary, pages_dir / page["image_filename"])
    ocr_results_service.save_page_result(document_id, number, result)

    def store(data):
        target = data["pages"][number - 1]
        target.update(rendered)
        target["ocr_status"] = result["status"]
        target["ocr_error"] = result["error"]

    document_metadata_service.update_metadata(document_id, store)
    logger.info("Document %s page %s: read again at %s DPI", document_id, number, config.OCR_DETAIL_DPI)
    return True


def run_detail_pass(engine, document_id):
    """Read every incomplete page of the document again in more detail. Returns the number of pages improved."""
    metadata = document_metadata_service.load_metadata(document_id)
    numbers = pages_needing_detail(document_id, metadata)
    if not numbers:
        return 0
    logger.info("Document %s: detail pass for page(s) %s", document_id, numbers)
    improved = 0
    for number in numbers:
        def mark_current(data, page_number=number):
            data["ocr"]["current_page"] = page_number

        metadata = document_metadata_service.update_metadata(document_id, mark_current)
        improved += read_page_in_detail(engine, document_id, metadata["pages"][number - 1])
    return improved


def mark_running(metadata):
    """Set the OCR status to "running" (used with update_metadata by both background jobs)."""
    metadata["ocr"]["status"] = "running"
    metadata["ocr"]["started_at"] = storage_service.utc_now()


def run_ocr_job(document_id):
    """Background job: OCR every pending page of a document."""
    with _job_lock:
        try:
            metadata = document_metadata_service.update_metadata(document_id, mark_running)
        except ServiceError as error:
            logger.warning("Document %s: OCR job cancelled: %s", document_id, error.message)
            return

        try:
            engine = get_engine()
        except Exception as error:
            message = error.message if isinstance(error, ServiceError) else "The OCR engine could not be started."
            logger.exception("Document %s: OCR engine could not be loaded", document_id)
            mark_pending_pages_failed(document_id, message)
            finish_job(document_id, message)
            return

        logger.info("Document %s: OCR started", document_id)
        for page in metadata.get("pages", []):
            if page.get("ocr_status") in ("completed", "no_text"):
                continue

            def mark_current(data, page_number=page["page_number"]):
                data["ocr"]["current_page"] = page_number

            document_metadata_service.update_metadata(document_id, mark_current)
            result = process_page(engine, document_id, page)

            def store_page_status(data, page_number=page["page_number"], page_result=result):
                target = data["pages"][page_number - 1]
                target["ocr_status"] = page_result["status"]
                target["ocr_error"] = page_result["error"]

            document_metadata_service.update_metadata(document_id, store_page_status)

        run_detail_pass(engine, document_id)
        final = finish_job(document_id)
        logger.info("Document %s: OCR finished with status %s", document_id, final["ocr"]["status"])


def run_detail_job(document_id):
    """Background job: only the detail pass, for a document that was read before."""
    with _job_lock:
        try:
            document_metadata_service.update_metadata(document_id, mark_running)
            engine = get_engine()
        except Exception as error:
            message = error.message if isinstance(error, ServiceError) else "The OCR engine could not be started."
            logger.exception("Document %s: detail pass could not start", document_id)
            finish_job(document_id, message)
            return
        improved = run_detail_pass(engine, document_id)
        finish_job(document_id)
        logger.info("Document %s: detail pass improved %d page(s)", document_id, improved)


def worker_loop():
    """Process queued jobs one after another, forever."""
    while True:
        kind, document_id = _job_queue.get()
        try:
            run_detail_job(document_id) if kind == "detail" else run_ocr_job(document_id)
        except Exception:
            logger.exception("Document %s: unexpected error in the OCR worker", document_id)
        finally:
            _job_queue.task_done()


def start_worker():
    """Start the OCR worker thread if it is not already running."""
    global _worker_thread
    with _worker_lock:
        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_thread = threading.Thread(target=worker_loop, name="ocr-worker", daemon=True)
            _worker_thread.start()


def submit_ocr(document_id):
    """Queue OCR for a document. Return (job_submitted, ocr_summary)."""
    should_run, summary = queue_ocr(document_id)
    if should_run:
        start_worker()
        _job_queue.put(("ocr", document_id))
    return should_run, summary


def submit_detail_pass(document_id):
    """
    Queue the detail pass for a document that was read before (for example before the detail pass
    existed, or with OCR_DETAIL_DPI turned off). Return (job_submitted, ocr_summary, page_numbers).
    """
    metadata = document_metadata_service.load_metadata(document_id)
    numbers = pages_needing_detail(document_id, metadata)
    if not numbers:
        return False, document_metadata_service.build_ocr_summary(metadata), []

    def mark_queued(data):
        ocr = data.setdefault("ocr", {})
        if ocr.get("status") in ("queued", "running"):
            raise ServiceError("OCR is already running for this document.", 409, "OCR_IN_PROGRESS")
        ocr.update({
            "status": "queued",
            "queued_at": storage_service.utc_now(),
            "started_at": None,
            "completed_at": None,
            "current_page": None,
            "error": None,
        })

    metadata = document_metadata_service.update_metadata(document_id, mark_queued)
    start_worker()
    _job_queue.put(("detail", document_id))
    return True, document_metadata_service.build_ocr_summary(metadata), numbers


def wait_until_idle():
    """Block until every queued OCR job has finished (used by tests and scripts)."""
    _job_queue.join()


def get_ocr_status(document_id):
    """Return the OCR progress of the document and the status of each page. The browser calls this repeatedly."""
    metadata = document_metadata_service.load_metadata(document_id)
    summary = document_metadata_service.build_ocr_summary(metadata)
    summary["document_id"] = document_id
    summary["pages"] = [
        {
            "page_number": page["page_number"],
            "ocr_status": page.get("ocr_status", "pending"),
            "ocr_error": page.get("ocr_error"),
        }
        for page in metadata.get("pages", [])
    ]
    return summary


def recover_interrupted_jobs():
    """At startup, mark OCR jobs that were cut off by a restart as interrupted."""
    recovered = 0
    for metadata in document_metadata_service.list_all_metadata():
        if metadata.get("ocr", {}).get("status") not in ("queued", "running"):
            continue

        def mark_interrupted(data):
            data["ocr"]["status"] = "interrupted"
            data["ocr"]["current_page"] = None
            data["ocr"]["error"] = (
                "The application stopped before OCR finished. Run OCR again to process the remaining pages."
            )

        document_metadata_service.update_metadata(metadata["document_id"], mark_interrupted)
        recovered += 1
    if recovered:
        logger.warning("Marked %d interrupted OCR job(s)", recovered)
    return recovered
