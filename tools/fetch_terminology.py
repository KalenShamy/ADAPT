"""Download the free terminology files the converter uses to code dose forms, routes, drugs and conditions.

Run once: python tools/fetch_terminology.py. Files go to resources/terminology/ (not committed).
"""

import io
import urllib.request
import zipfile
from pathlib import Path

NCIT_URL = "https://evs.nci.nih.gov/ftp1/NCI_Thesaurus/Thesaurus.FLAT.zip"
TARGET = Path(__file__).resolve().parents[1] / "resources" / "terminology"


def main() -> None:
    TARGET.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(NCIT_URL) as response:
        archive_bytes = response.read()
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        archive.extractall(TARGET)
    print(f"NCI Thesaurus -> {TARGET}")


if __name__ == "__main__":
    main()
