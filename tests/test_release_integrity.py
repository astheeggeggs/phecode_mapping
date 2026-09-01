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
# A source map's vocabulary_id is carried through as given rather than overridden,
# so an ICD10CM map in a release may be genuine ICD-10-CM or a WHO map whose
# vocabulary_id column was rewritten to ICD10CM before the build.
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


def test_two_builds_from_the_same_inputs_are_byte_identical(tmp_path: Path) -> None:
    """A federated study needs sites to prove they hold the same map.

    Every artefact was previously written by an unordered COPY, and the xlsx carried
    the wall clock in docProps and in each zip member's mtime -- so rebuilding from
    identical inputs produced different bytes and different checksums. Verification
    still passed (each release is checked against its own manifest), but two sites
    could not compare digests and conclude anything, which is most of the point of
    shipping digests. manifest.json itself is excluded: it records created_at_utc.
    """
    from conftest import write_csv
    from phecodex_mapper.vocabulary import build_vocabulary

    source = tmp_path / "repro.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"],
              [["CV_003", "A01.1", "ICD10CM"], ["GI_001", "B02.2", "ICD10"],
               ["ID_052", "123.4", "ICD9CM"]])
    info = tmp_path / "repro_info.csv"
    write_csv(info, ["phecode", "sex", "phecode_string", "category"],
              [["CV_003", "Both", "One", "Cardiovascular"], ["GI_001", "Female", "Two", "Digestive"],
               ["ID_052", "Male", "Three", "Infections"]])

    digests = []
    for name in ("build_one", "build_two"):
        release = tmp_path / name
        build_vocabulary(source, info, release, None)
        digests.append(json.loads((release / "manifest.json").read_text())["artifacts"])

    first, second = digests
    assert set(first) == set(second)
    differing = sorted(k for k in first if first[k]["sha256"] != second[k]["sha256"])
    assert not differing, f"not reproducible: {differing}"

    # Comparing two digests is not enough on its own: the workbook's timestamps have
    # one-second resolution, so two builds of a small fixture can match by landing in
    # the same second. Assert the pinned values directly.
    import zipfile
    book = tmp_path / "build_one" / "phecodex_reference_maps.xlsx"
    with zipfile.ZipFile(book) as archive:
        stamps = {item.date_time for item in archive.infolist()}
        core = archive.read("docProps/core.xml").decode()
    assert stamps == {(2000, 1, 1, 0, 0, 0)}, f"zip member mtimes not pinned: {sorted(stamps)}"
    assert core.count("2000-01-01T00:00:00Z") == 2, "docProps created/modified not both pinned"


def test_a_smuggled_snomed_map_fails_verification(tmp_path: Path, full_release: Path) -> None:
    """An unrecorded file the mapper reads by name is a behaviour change, not clutter.

    map-phecodes loads snomed_map.parquet if it merely EXISTS, so dropping one beside
    an --icd-only release silently restores the SNOMED mappings that release was built
    to withhold -- and verification previously reported it under unexpected_files and
    then returned "ok" with exit 0.
    """
    (full_release / "snomed_map.parquet").write_bytes(b"not really parquet")
    result = _verify(full_release)
    assert result.returncode != 0
    assert "snomed_map.parquet" in result.stderr
    assert "not recorded in the manifest" in result.stderr


def test_the_mapper_refuses_a_release_carrying_an_unrecorded_snomed_map(
        tmp_path: Path, full_release: Path) -> None:
    """Belt and braces: the mapper must not depend on anyone having run verify first."""
    from conftest import write_csv
    from phecodex_mapper.mapper import map_phecodes

    (full_release / "snomed_map.parquet").write_bytes(b"not really parquet")
    cohort, events = tmp_path / "c.csv", tmp_path / "e.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "A01.1", "ICD10CM"]])
    with pytest.raises(ValueError, match="not recorded in manifest.json"):
        map_phecodes(full_release, cohort, events, tmp_path / "out", min_cases=1, min_controls=1)


def test_the_manifest_names_the_upstream_phecodex_version_of_each_source(tmp_path: Path) -> None:
    """A checksum pins the file exactly but tells a reader nothing they can cite.

    It matters because a "PhecodeX 1.1" release covering both vocabularies is
    necessarily a hybrid: upstream ships no WHO map for 1.1, so CM comes from 1.1 and
    WHO from 1.0. Any cohort coded in WHO ICD-10 -- UK Biobank included -- is therefore
    phenotyped from 1.0. Digests below are the real published files.
    """
    from conftest import write_csv
    from phecodex_mapper.vocabulary import UPSTREAM_PHECODEX_FILES, _upstream, build_vocabulary

    who_10 = "50b06ef08d41a51a9b691cc2e60b2a63d4f54b8262e7688594f885c75f410796"
    cm_11 = "31705ab956267abee83bc363a5c8a0f7c1489daad8cc6f347facb1443dc6ffd5"
    assert _upstream(who_10) == {"version": "1.0", "file": "phecodeX_unrolled_ICD_WHO.csv"}
    assert _upstream(cm_11) == {"version": "1.1", "file": "phecodeX_unrolled_ICD_CM.csv"}
    assert not any(e["version"] == "1.1" and "WHO" in e["file"]
                   for e in UPSTREAM_PHECODEX_FILES.values()), \
        "upstream publishes no WHO map for 1.1; recording one would be a fabrication"

    # An unrecognised file is reported as such rather than guessed at.
    source = tmp_path / "local.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["CV_003", "A01.1", "ICD10CM"]])
    release = tmp_path / "rel"
    build_vocabulary(source, None, release, None)
    manifest = json.loads((release / "manifest.json").read_text())
    assert manifest["phecodex_map"]["upstream"] is None
    assert manifest["phecodex_upstream_versions"] == []
