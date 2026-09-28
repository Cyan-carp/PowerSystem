"""Download pinned original PV fault data outside Git and verify SHA-256."""

from __future__ import annotations

import argparse
import hashlib
import urllib.request
from pathlib import Path

REVISION = "e9ed8ad8072dcb3ffc643b0f3604219798708551"
BASE = f"https://raw.githubusercontent.com/clayton-h-costa/pv_fault_dataset/{REVISION}/"
FILES = {
    "LICENSE": "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4",
    "README.md": "5204f6da3e825f093779e8df4f19f66efb7268a2ecb3a3ae81e298caac9e01ca",
    "dataset_amb.mat": "e4619812a5b6c1331332248f052cb09938f9a43ccac8bcc512796c6e906da7e0",
    "dataset_elec.mat": "c04ed3fcc0e338703e87d387b1ce6ef51da1848e77b4d491e62afe08934ce494",
}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def download(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for name, expected in FILES.items():
        destination = output / name
        if destination.is_file() and digest(destination) == expected:
            print(f"present: {name}")
            continue
        temporary = output / (name + ".part")
        try:
            with urllib.request.urlopen(BASE + name, timeout=120) as response, temporary.open("wb") as stream:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    stream.write(chunk)
            if digest(temporary) != expected:
                raise ValueError(f"SHA-256 mismatch: {name}")
            temporary.replace(destination)
            print(f"downloaded: {name}")
        finally:
            temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    download(args.output)


if __name__ == "__main__":
    main()
