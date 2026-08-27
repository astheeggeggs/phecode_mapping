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

from conftest import write_csv
from phecodex_mapper.vocabulary import build_vocabulary

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


# ---------------------------------------------------------------------------
# Vocabulary provenance.
#
# PhecodeX ships the WHO ICD-10 map twice: phecodeX_unrolled_ICD_WHO.csv labels
# its 20,255 rows ICD10, and phecodeX_unrolled_ICD_UKB.csv labels the
# byte-identical content ICD10CM (verified: 0 rows differ either way after
# normalisation). Both are upstream choices, and this tool carries the label
# through rather than overriding it.
#
# The consequence is that two releases are silently incompatible with the same
# events file, and the vocabulary label alone cannot tell you which you have --
# an ICD10CM map might be genuine CM or relabelled WHO. Recording the source
# file per vocabulary makes that visible at the release level.
# ---------------------------------------------------------------------------

def test_manifest_records_which_source_file_each_vocabulary_came_from(tmp_path: Path) -> None:
    """Two source files whose vocabulary labels overlap must stay distinguishable."""
    cm = tmp_path / "cm.csv"
    write_csv(cm, ["phecode", "ICD", "vocabulary_id"],
              [["EM_202", "E11", "ICD10CM"], ["EM_202", "E11.9", "ICD10CM"]])
    who = tmp_path / "who.csv"
    write_csv(who, ["phecode", "ICD", "vocabulary_id"], [["EM_202", "E11", "ICD10"]])

    release = tmp_path / "rel_prov"
    build_vocabulary([cm, who], None, release, None)
    vocabularies = json.loads((release / "manifest.json").read_text())["vocabularies"]

    assert vocabularies["ICD10CM"]["source_files"] == ["cm.csv"]
    assert vocabularies["ICD10"]["source_files"] == ["who.csv"]


def test_vocabulary_row_counts_reconcile_with_the_total(tmp_path: Path) -> None:
    """Two numbers in one manifest that describe the same thing must agree.

    The first version of this block counted DISTINCT codes under a key named
    `rows`, so it summed to less than counts.icd_map_rows -- the same class of
    non-reconciling pair this release metadata exists to prevent.
    """
    cm = tmp_path / "cm2.csv"
    write_csv(cm, ["phecode", "ICD", "vocabulary_id"],
              [["EM_202", "E11", "ICD10CM"],
               ["EM_202", "E11.9", "ICD10CM"],
               ["CV_401", "E11", "ICD10CM"],      # same code, second phecode
               ["CV_401", "123.4", "ICD9CM"]])

    release = tmp_path / "rel_rec"
    build_vocabulary(cm, None, release, None)
    manifest = json.loads((release / "manifest.json").read_text())

    vocabularies = manifest["vocabularies"]
    assert sum(v["rows"] for v in vocabularies.values()) == manifest["counts"]["icd_map_rows"]
    # distinct_codes is genuinely smaller where one code carries several phecodes,
    # which is why the two are reported separately rather than one standing in for
    # the other.
    assert vocabularies["ICD10CM"]["rows"] == 3
    assert vocabularies["ICD10CM"]["distinct_codes"] == 2
