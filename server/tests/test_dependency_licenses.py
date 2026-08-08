from __future__ import annotations

from importlib.metadata import distribution, metadata, version
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]


def _locked_requirements(path: Path) -> dict[str, str]:
    locked: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-r "):
            continue
        assert "==" in line, f"direct dependency must be exactly pinned: {line}"
        name, pinned = line.split("==", 1)
        normalized = re.sub(r"[-_.]+", "-", name.split("[", 1)[0]).casefold()
        locked[normalized] = pinned
    return locked


def test_offer_pdf_direct_dependencies_are_exactly_locked_and_installed() -> None:
    runtime = _locked_requirements(ROOT / "server/requirements.txt")
    development = _locked_requirements(ROOT / "server/requirements-dev.txt")

    assert runtime["weasyprint"] == "69.0"
    assert development["pip-licenses"] == "5.5.5"
    assert version("WeasyPrint") == runtime["weasyprint"]
    assert version("pip-licenses") == development["pip-licenses"]


def test_weasyprint_and_license_auditor_metadata_match_third_party_notice() -> None:
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    weasy_metadata = metadata("WeasyPrint")
    license_file = next(path for path in distribution("WeasyPrint").files or () if str(path).replace("\\", "/").endswith("dist-info/licenses/LICENSE"))
    license_text = distribution("WeasyPrint").locate_file(license_file).read_text(encoding="utf-8")

    assert "License :: OSI Approved :: BSD License" in (weasy_metadata.get_all("Classifier") or [])
    assert "BSD 3-Clause License" in license_text
    assert "| WeasyPrint | 69.0 | BSD-3-Clause |" in notices
    assert metadata("pip-licenses")["License-Expression"] == "MIT"
    assert "| pip-licenses | 5.5.5 | MIT |" in notices
    assert "AGPL" not in license_text and "SSPL" not in license_text


def test_docker_pdf_runtime_packages_and_font_notice_are_minimal_and_consistent() -> None:
    dockerfile = (ROOT / "server/Dockerfile").read_text(encoding="utf-8")
    renderer = (ROOT / "server/app/offers/pdf.py").read_text(encoding="utf-8")
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

    for package in ("fonts-noto-cjk", "libharfbuzz-subset0", "libpango-1.0-0", "libpangoft2-1.0-0"):
        assert package in dockerfile
    for forbidden_build_package in ("build-essential", "gcc", "libffi-dev", "libjpeg-dev", "libopenjp2-7-dev"):
        assert forbidden_build_package not in dockerfile
    assert 'font-family: "Noto Sans CJK SC"' in renderer
    assert "SIL Open Font License 1.1" in notices
    assert "LGPL-2.1-or-later" in notices
    assert "HarfBuzz" in notices and "| MIT |" in notices


def test_windows_runtime_bootstrap_pins_official_artifacts_and_hashes() -> None:
    installer = (ROOT / "server/scripts/install_weasyprint_windows.ps1").read_text(encoding="utf-8")

    assert '$weasyVersion = "69.0"' in installer
    assert "330101ff3ea50ebde4abf805283b6d703d5f3d71c77c983db94357ec4524a3ef" in installer
    assert "f9bc7d33fca891929aeeedcdf3a553fb1a26a7a45d9ee868fe6ec13dd10c514e" in installer
    assert "2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b" in installer
    assert "523d033d6cb47f4a80c58a35753646f5c3608a78" in installer
    assert "Assert-Sha256" in installer
