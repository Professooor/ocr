"""
Check Linux wheels against the Oracle Linux 7.9 system library ceiling.

Oracle Linux 7.9 ships:
    glibc      2.17   -> highest symbol version GLIBC_2.17
    libstdc++  4.8.5  -> highest symbol version GLIBCXX_3.4.19 / CXXABI_1.3.7
    libgcc_s   4.8.5  -> highest symbol version GCC_4.8.0

The script opens every .whl file in a folder, reads the ELF "version needs"
section (.gnu.version_r) of every shared library inside it and reports any
library that requires a newer symbol version than Oracle Linux 7.9 provides.

A requirement on libstdc++ / libgcc_s is reported as satisfied when the wheel
bundles its own copy of that library.

Usage (Windows or Linux):
    python scripts/check_linux_wheels.py <folder-with-linux-wheels>
"""

import re
import struct
import sys
import zipfile
from pathlib import Path

# Highest symbol versions available on Oracle Linux 7.9.
OL7_CEILING = {
    "GLIBC": (2, 17),
    "GLIBCXX": (3, 4, 19),
    "CXXABI": (1, 3, 7),
    "GCC": (4, 8, 0),
}

SHT_GNU_VERNEED = 0x6FFFFFFE
VERSION_PATTERN = re.compile(r"^(GLIBCXX|GLIBC|CXXABI|GCC)_([0-9.]+)$")


def parse_version_needs(data):
    """Return a list of (library_name, version_name) pairs from an ELF64 file."""
    if data[:4] != b"\x7fELF" or data[4] != 2:
        return []
    endian = "<" if data[5] == 1 else ">"
    section_offset = struct.unpack_from(endian + "Q", data, 0x28)[0]
    section_size = struct.unpack_from(endian + "H", data, 0x3A)[0]
    section_count = struct.unpack_from(endian + "H", data, 0x3C)[0]

    sections = []
    for index in range(section_count):
        base = section_offset + index * section_size
        sh_type = struct.unpack_from(endian + "I", data, base + 4)[0]
        sh_offset = struct.unpack_from(endian + "Q", data, base + 0x18)[0]
        sh_size = struct.unpack_from(endian + "Q", data, base + 0x20)[0]
        sh_link = struct.unpack_from(endian + "I", data, base + 0x28)[0]
        sh_info = struct.unpack_from(endian + "I", data, base + 0x2C)[0]
        sections.append((sh_type, sh_offset, sh_size, sh_link, sh_info))

    results = []
    for sh_type, sh_offset, _sh_size, sh_link, sh_info in sections:
        if sh_type != SHT_GNU_VERNEED:
            continue
        string_table_offset = sections[sh_link][1]

        def read_string(offset):
            start = string_table_offset + offset
            end = data.index(b"\x00", start)
            return data[start:end].decode("ascii", "replace")

        entry = sh_offset
        for _ in range(sh_info):
            _vn_version, vn_cnt, vn_file, vn_aux, vn_next = struct.unpack_from(
                endian + "HHIII", data, entry
            )
            library_name = read_string(vn_file)
            aux = entry + vn_aux
            for _ in range(vn_cnt):
                _hash, _flags, _other, vna_name, vna_next = struct.unpack_from(
                    endian + "IHHII", data, aux
                )
                results.append((library_name, read_string(vna_name)))
                if vna_next == 0:
                    break
                aux += vna_next
            if vn_next == 0:
                break
            entry += vn_next
    return results


def version_tuple(text):
    return tuple(int(part) for part in text.split(".") if part.isdigit())


def check_wheel(wheel_path):
    """Return (problems, highest_seen) for one wheel."""
    problems = []
    highest_seen = {}
    with zipfile.ZipFile(wheel_path) as wheel:
        names = wheel.namelist()
        bundled = " ".join(Path(name).name for name in names)
        bundles_libstdcxx = "libstdc++" in bundled
        bundles_libgcc = "libgcc_s" in bundled

        for name in names:
            base_name = Path(name).name
            if not (base_name.endswith(".so") or ".so." in base_name):
                continue
            data = wheel.read(name)
            for library_name, version_name in parse_version_needs(data):
                match = VERSION_PATTERN.match(version_name)
                if not match:
                    continue
                family, number = match.group(1), version_tuple(match.group(2))
                if number > highest_seen.get(family, ()):
                    highest_seen[family] = number
                if number <= OL7_CEILING[family]:
                    continue
                if family in ("GLIBCXX", "CXXABI") and bundles_libstdcxx:
                    continue
                if family == "GCC" and bundles_libgcc:
                    continue
                problems.append(f"{name} needs {version_name} from {library_name}")
    return problems, highest_seen


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    folder = Path(sys.argv[1])
    wheels = sorted(folder.glob("*.whl"))
    if not wheels:
        print(f"No .whl files found in {folder}")
        return 2

    failed = 0
    for wheel_path in wheels:
        problems, highest_seen = check_wheel(wheel_path)
        if not highest_seen:
            continue
        summary = ", ".join(
            f"{family}_{'.'.join(str(n) for n in number)}"
            for family, number in sorted(highest_seen.items())
        )
        status = "FAIL" if problems else "OK  "
        print(f"[{status}] {wheel_path.name}  (highest: {summary})")
        for problem in problems[:10]:
            print(f"         {problem}")
        if len(problems) > 10:
            print(f"         ... {len(problems) - 10} more")
        if problems:
            failed += 1

    print()
    print(f"Wheels with native code above the Oracle Linux 7.9 ceiling: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
