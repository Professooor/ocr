"""
Download the official PaddleOCR ONNX models into the project and make them
loadable by onnxruntime 1.16.3.

Why this script is needed:
Oracle Linux 7.9 is too old for PaddlePaddle, so the app uses onnxruntime 1.16.3,
the newest version that still runs there. Some official ONNX models are labelled
"IR version 10", but onnxruntime 1.16.3 only loads models labelled 9 or lower.

The IR version is only a label of the file format. Changing it from 10 to 9 does
not change the model, as long as the model uses nothing that needs version 10.
This script checks that first and refuses to change a model when it is not safe.

Usage (run from the project folder):
    Windows: .\\.venv\\Scripts\\python.exe scripts\\prepare_ocr_models.py
    Linux:   .venv/bin/python scripts/prepare_ocr_models.py

Options:
    --skip-download   Only check/patch models that already exist in OCR_MODEL_DIR
                      (use this on a server without internet access after
                      copying the models folder from another machine).
"""

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from app import config  # noqa: E402

MAX_IR_VERSION = 9        # Highest IR version that onnxruntime 1.16.3 can load
MAX_DEFAULT_OPSET = 19    # Highest operator set (opset) version onnxruntime 1.16.3 supports
FIELD_IR_VERSION = 1      # Field number of the IR version inside an ONNX file
FIELD_OPSET_IMPORT = 8    # Field number of the opsets the model uses
FIELD_FUNCTIONS = 25      # Field number of model functions (needs IR version 10)


def read_varint(data, position):
    """Read one number stored in protobuf format (the file format of ONNX). Return (value, new_position)."""
    result = 0
    shift = 0
    while True:
        byte = data[position]
        position += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, position
        shift += 7


def encode_varint(value):
    output = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            output.append(byte | 0x80)
        else:
            output.append(byte)
            return bytes(output)


def read_top_level_fields(data):
    """Return a list of (field_number, wire_type, value_start, value_end) for a protobuf message."""
    fields = []
    position = 0
    while position < len(data):
        tag, position = read_varint(data, position)
        field_number, wire_type = tag >> 3, tag & 0x07
        start = position
        if wire_type == 0:
            _, position = read_varint(data, position)
        elif wire_type == 1:
            position += 8
        elif wire_type == 2:
            length, start = read_varint(data, position)
            position = start + length
        elif wire_type == 5:
            position += 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire_type}")
        fields.append((field_number, wire_type, start, position))
    return fields


def read_opsets(data, fields):
    """Return a list of (domain, version) from ModelProto.opset_import."""
    opsets = []
    for field_number, wire_type, start, end in fields:
        if field_number != FIELD_OPSET_IMPORT or wire_type != 2:
            continue
        domain, version = "", 0
        for sub_number, sub_wire, sub_start, sub_end in read_top_level_fields(data[start:end]):
            chunk = data[start:end]
            if sub_number == 1 and sub_wire == 2:
                domain = chunk[sub_start:sub_end].decode("utf-8")
            elif sub_number == 2 and sub_wire == 0:
                version, _ = read_varint(chunk, sub_start)
        opsets.append((domain or "ai.onnx", version))
    return opsets


def patch_model_ir_version(model_path):
    """Lower the IR version label of one ONNX file when it is safe to do so."""
    data = model_path.read_bytes()
    fields = read_top_level_fields(data)

    ir_fields = [f for f in fields if f[0] == FIELD_IR_VERSION and f[1] == 0]
    if not ir_fields:
        raise ValueError(f"{model_path} has no ir_version field; it may not be an ONNX model.")
    _, _, ir_start, ir_end = ir_fields[0]
    ir_version, _ = read_varint(data, ir_start)
    opsets = read_opsets(data, fields)
    has_functions = any(f[0] == FIELD_FUNCTIONS for f in fields)

    print(f"  ir_version={ir_version} opsets={opsets} functions={'yes' if has_functions else 'no'}")
    if ir_version <= MAX_IR_VERSION:
        print("  OK: already compatible with onnxruntime 1.16.3")
        return False

    default_opsets = [version for domain, version in opsets if domain in ("ai.onnx", "")]
    if has_functions:
        raise ValueError("Model contains ONNX functions (IR 10 feature); refusing to patch.")
    if any(domain not in ("ai.onnx", "") for domain, _ in opsets):
        raise ValueError(f"Model uses custom operator domains {opsets}; refusing to patch.")
    if not default_opsets or max(default_opsets) > MAX_DEFAULT_OPSET:
        raise ValueError(f"Model needs opset {default_opsets}; onnxruntime 1.16.3 supports up to {MAX_DEFAULT_OPSET}.")

    patched = data[:ir_start] + encode_varint(MAX_IR_VERSION) + data[ir_end:]
    temp_path = model_path.with_suffix(".onnx.tmp")
    temp_path.write_bytes(patched)
    os.replace(temp_path, model_path)

    note_path = model_path.parent / "ir_version_patch.json"
    note_path.write_text(
        json.dumps(
            {"original_ir_version": ir_version, "patched_ir_version": MAX_IR_VERSION, "opsets": opsets},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"  PATCHED: ir_version {ir_version} -> {MAX_IR_VERSION}")
    return True


def download_model(model_name, target_dir):
    """Download PaddlePaddle/<model_name>_onnx from Hugging Face into target_dir."""
    from huggingface_hub import snapshot_download

    repo_id = f"PaddlePaddle/{model_name}_onnx"
    print(f"  downloading {repo_id} ...")
    snapshot_download(repo_id=repo_id, local_dir=str(target_dir))


def main():
    parser = argparse.ArgumentParser(description="Prepare PaddleOCR ONNX models.")
    parser.add_argument("--skip-download", action="store_true")
    args = parser.parse_args()

    model_names = [config.OCR_TEXT_DETECTION_MODEL, config.OCR_TEXT_RECOGNITION_MODEL]
    config.OCR_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Model folder: {config.OCR_MODEL_DIR}")

    failures = 0
    for model_name in model_names:
        target_dir = config.OCR_MODEL_DIR / f"{model_name}_onnx"
        model_file = target_dir / "inference.onnx"
        print(f"\n{model_name}")
        try:
            if not model_file.exists():
                if args.skip_download:
                    raise FileNotFoundError(f"{model_file} not found and --skip-download was given.")
                download_model(model_name, target_dir)
            if not (target_dir / "inference.yml").exists():
                raise FileNotFoundError(f"{target_dir / 'inference.yml'} is missing.")
            patch_model_ir_version(model_file)
        except Exception as error:  # Report every model, then fail at the end.
            failures += 1
            print(f"  ERROR: {error}")

    print()
    if failures:
        print(f"{failures} model(s) could not be prepared.")
        return 1
    print("All OCR models are ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
