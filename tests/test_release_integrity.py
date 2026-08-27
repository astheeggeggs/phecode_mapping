"""Regression tests: release verification must actually verify the release.

verify_release.py previously checked that four filenames existed and that
manifest.json had two keys. It hashed manifest.json and compared that hash to
nothing. Replacing icd_map.parquet with arbitrary bytes still reported
`"status": "ok"` -- and this is step one of the documented analyst workflow.

The manifest's pre-existing checksums describe the *source CSVs on the build
machine*, which the receiving site does not have, so they could never be checked.
Releases now carry a digest for every file they ship.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

VERIFY = Path(__file__).resolve().parents[1] / "scripts" / "verify_release.py"


def _verify(release: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(VERIFY), "--release", str(release)],
                          capture_output=True, text=True)


def test_build_records_a_digest_for_every_shipped_file(full_release: Path) -> None:
    manifest = json.loads((full_release / "manifest.json").read_text())
    artifacts = manifest["artifacts"]
    shipped = {p.name for p in full_release.iterdir() if p.is_file() and p.name != "manifest.json"}
    assert set(artifacts) == shipped, "every shipped file must carry a digest, and vice versa"
    for name, meta in artifacts.items():
        assert len(meta["sha256"]) == 64
        assert meta["bytes"] == (full_release / name).stat().st_size


def test_clean_release_verifies(full_release: Path) -> None:
    result = _verify(full_release)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "ok"
    assert report["artifacts_verified"] >= 3
    assert report["unexpected_files"] == []


@pytest.mark.parametrize("target", ["icd_map.parquet", "phecode_info.parquet"])
def test_corrupted_artifact_fails_verification(full_release: Path, target: str) -> None:
    """The exact scenario that used to report ok."""
    (full_release / target).write_bytes(b"GARBAGE NOT A PARQUET FILE")
    result = _verify(full_release)
    assert result.returncode != 0
    assert "FAILED" in result.stderr
    assert target in result.stderr


def test_truncated_artifact_fails_verification(full_release: Path) -> None:
    """A size change is caught before the digest, so the message names the cause."""
    path = full_release / "icd_map.parquet"
    path.write_bytes(path.read_bytes()[:-32])
    result = _verify(full_release)
    assert result.returncode != 0
    assert "size is" in result.stderr


def test_deleted_artifact_fails_verification(full_release: Path) -> None:
    (full_release / "icd_map.csv").unlink()
    result = _verify(full_release)
    assert result.returncode != 0
    assert "missing from the release" in result.stderr


def test_added_file_is_reported(full_release: Path) -> None:
    """An unrecorded file is surfaced, not silently accepted."""
    (full_release / "notes.txt").write_text("added after the build\n")
    result = _verify(full_release)
    assert result.returncode == 0, "an extra file is not itself a corruption"
    assert json.loads(result.stdout)["unexpected_files"] == ["notes.txt"]


def test_release_without_artifact_digests_is_refused(full_release: Path) -> None:
    """An old release cannot be verified, and must say so rather than pass."""
    manifest = json.loads((full_release / "manifest.json").read_text())
    del manifest["artifacts"]
    (full_release / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    result = _verify(full_release)
    assert result.returncode != 0
    assert "records no artifact checksums" in result.stderr


def test_tampering_with_the_manifest_digest_is_caught(full_release: Path) -> None:
    """Editing a recorded digest must not make a mismatched file verify."""
    manifest = json.loads((full_release / "manifest.json").read_text())
    manifest["artifacts"]["icd_map.parquet"]["sha256"] = "0" * 64
    (full_release / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    result = _verify(full_release)
    assert result.returncode != 0
    assert "icd_map.parquet" in result.stderr
