"""Regression tests for what ships, and for packaging not damaging the release.

B5  The analyst bundle, the Docker image and the Apptainer def all shipped only
    verify_release.py, while the bundled README told analysts to run
    prepare_ukb_for_mapping.R. The
    required set is derived from the docs here rather than hard-coded, so
    documenting a new command without shipping its script fails the suite.

S14 package_release.py rewrote manifest.json to add a `bundle_contents` key. That
    changed the manifest's own hash, retroactively invalidating the
    release_manifest_sha256 recorded in every audit.json produced against that
    release -- so validate-phecodex then rejected already-completed runs. The key
    was redundant: manifest["artifacts"] already lists every shipped file.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_RE = re.compile(r"scripts/[A-Za-z0-9_]+\.(?:py|R)")


def documented_scripts() -> set[str]:
    """Every scripts/* path README.md or ANALYST_GUIDE.md tells an analyst to run."""
    found: set[str] = set()
    for doc in ("README.md", "ANALYST_GUIDE.md"):
        found |= set(SCRIPT_RE.findall((ROOT / doc).read_text()))
    assert found, "docs reference no scripts; the extraction regex is probably wrong"
    return found


def test_the_docs_reference_scripts_that_exist() -> None:
    """A documented path that does not exist would make the other tests vacuous."""
    missing = sorted(s for s in documented_scripts() if not (ROOT / s).is_file())
    assert missing == [], f"documentation references non-existent scripts: {missing}"


def test_analyst_bundle_contains_every_documented_script(tmp_path: Path, full_release: Path) -> None:
    bundle = tmp_path / "dist.tar.gz"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "package_distribution.py"),
         "--release", str(full_release), "--output", str(bundle)],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    with tarfile.open(bundle, "r:gz") as archive:
        members = {m.name for m in archive.getmembers()}
    missing = sorted(s for s in documented_scripts()
                     if f"phecodex-distribution/{s}" not in members)
    assert missing == [], f"bundle omits documented scripts: {missing}"

    # The bundle must also carry the things those scripts and the workflow need.
    assert "phecodex-distribution/release/manifest.json" in members
    assert "phecodex-distribution/src/phecodex_mapper/mapper.py" in members
    assert "phecodex-distribution/src/phecodex_mapper/data/recommended_exclusions.csv" in members
    assert (bundle.with_name(bundle.name + ".sha256")).is_file()


@pytest.mark.parametrize("container", ["containers/Dockerfile", "containers/Singularity.def"])
def test_container_images_contain_every_documented_script(container: str) -> None:
    text = (ROOT / container).read_text()
    missing = sorted(s for s in documented_scripts() if s not in text)
    assert missing == [], f"{container} does not copy in: {missing}"


def test_bundled_scripts_run_from_the_extracted_bundle(tmp_path: Path, full_release: Path) -> None:
    """A shipped script must be runnable from the bundle layout, not just present.

    verify_release.py is the first step of the documented workflow, so a bundle in
    which it cannot start is useless even though the file is there.
    """
    bundle = tmp_path / "dist.tar.gz"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "package_distribution.py"),
         "--release", str(full_release), "--output", str(bundle)],
        capture_output=True, text=True, check=True)
    extracted = tmp_path / "unpacked"
    with tarfile.open(bundle, "r:gz") as archive:
        archive.extractall(extracted)
    root = extracted / "phecodex-distribution"
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "verify_release.py"), "--release", str(root / "release")],
        capture_output=True, text=True, env={"PYTHONPATH": str(root / "src"), "PATH": "/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "ok"


def test_packaging_a_release_does_not_modify_it(tmp_path: Path, full_release: Path) -> None:
    """The manifest must be byte-identical before and after packaging."""
    manifest = full_release / "manifest.json"
    before = manifest.read_bytes()
    listing = sorted(p.name for p in full_release.iterdir())

    output = tmp_path / "release.tar.gz"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "package_release.py"),
         "--release", str(full_release), "--output", str(output)],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    assert manifest.read_bytes() == before, "packaging rewrote manifest.json"
    assert sorted(p.name for p in full_release.iterdir()) == listing
    assert output.is_file() and output.with_name(output.name + ".sha256").is_file()
    assert "verified:" in result.stdout


def test_audit_provenance_survives_packaging(tmp_path: Path, full_release: Path) -> None:
    """The end-to-end consequence: a run's recorded manifest hash must still match.

    This is what actually broke -- validate-phecodex compares audit.json's
    release_manifest_sha256 against the live manifest, and packaging changed it.
    """
    from conftest import write_csv
    from phecodex_mapper.io import checksum
    from phecodex_mapper.mapper import map_phecodes

    cohort, events, run = tmp_path / "cohort.csv", tmp_path / "events.csv", tmp_path / "run"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "A01.1", "ICD10CM"]])
    map_phecodes(full_release, cohort, events, run, min_cases=1, min_controls=0)
    recorded = json.loads((run / "audit.json").read_text())["release_manifest_sha256"]

    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "package_release.py"),
         "--release", str(full_release), "--output", str(tmp_path / "release.tar.gz")],
        capture_output=True, text=True, check=True)

    assert checksum(full_release / "manifest.json") == recorded, \
        "packaging invalidated the provenance chain of an existing run"


def test_packaging_refuses_a_corrupted_release(tmp_path: Path, full_release: Path) -> None:
    """Do not ship a release that would fail verification at the receiving site."""
    (full_release / "icd_map.parquet").write_bytes(b"GARBAGE")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "package_release.py"),
         "--release", str(full_release), "--output", str(tmp_path / "release.tar.gz")],
        capture_output=True, text=True)
    assert result.returncode != 0
    assert "does not verify" in result.stderr
    assert not (tmp_path / "release.tar.gz").exists()
