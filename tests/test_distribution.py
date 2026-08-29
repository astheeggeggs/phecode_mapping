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


def test_every_bundled_python_script_starts_from_the_bundle(tmp_path: Path, full_release: Path) -> None:
    """Not just verify_release.py, which is stdlib-only and so cannot catch this.

    The DuckDB scripts import phecodex_mapper.io/.retention rather than re-implementing
    the connection and the path->SQL rules. That is only safe if the bundle layout makes
    the package importable -- it ships src/ and the docs say `pip install .` -- so an
    import that resolves in the repo but not in the bundle would ship a broken tool. A
    --help run is enough: it executes every module-level import.
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

    scripts = sorted(s for s in documented_scripts() if s.endswith(".py"))
    assert len(scripts) > 1, f"expected several bundled python scripts, found {scripts}"
    broken = {}
    for script in scripts:
        result = subprocess.run(
            [sys.executable, str(root / script), "--help"], capture_output=True, text=True,
            env={"PYTHONPATH": str(root / "src"), "PATH": "/usr/bin:/bin"})
        if result.returncode != 0:
            broken[script] = result.stderr.strip().splitlines()[-1:]
    assert broken == {}, f"bundled scripts that cannot start: {broken}"


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


def _icd_only_release(tmp_path: Path, name: str):
    """A recovered release built with --icd-only: bridge used as evidence, not shipped.

    B02.1 is unmapped everywhere and reaches a SNOMED concept bridged from the ICD-9
    side, so it can ONLY be recovered via the bridge. If --icd-only disabled the bridge
    rather than merely withholding it, that row would vanish and this fixture would
    stop proving anything.
    """
    from conftest import write_csv
    from phecodex_mapper.vocabulary import build_vocabulary

    source = tmp_path / f"src_{name}.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["ID_052", "003.3", "ICD9CM"]])
    athena = tmp_path / f"ath_{name}"
    athena.mkdir()
    write_csv(athena / "CONCEPT.csv",
              ["concept_id", "concept_code", "vocabulary_id", "domain_id", "standard_concept", "invalid_reason"],
              [[4, "003.3", "ICD9CM", "Condition", "", ""], [3, "B02.1", "ICD10", "Condition", "", ""],
               [5, "77386006", "SNOMED", "Condition", "S", ""]])
    write_csv(athena / "CONCEPT_RELATIONSHIP.csv",
              ["concept_id_1", "concept_id_2", "relationship_id", "invalid_reason"],
              [[4, 5, "Maps to", ""], [3, 5, "Maps to", ""]])
    release = tmp_path / name
    build_vocabulary(source, None, release, athena, recover_unmapped=True, icd_only=True)
    return release


def test_icd_only_withholds_the_bridge_but_still_recovers_through_it(tmp_path: Path) -> None:
    """The distinction the flag exists for.

    snomed_map.* is Athena-derived content the analyst distribution must not
    redistribute. The bridge itself is how some recovered ICD rows were justified, and
    dropping it would silently shrink the map instead of just withholding a table.
    """
    import duckdb
    release = _icd_only_release(tmp_path, "icdonly")

    assert not (release / "snomed_map.parquet").exists()
    assert not (release / "snomed_map.csv").exists()
    manifest = json.loads((release / "manifest.json").read_text())
    assert manifest["icd_only"] is True
    assert manifest["counts"]["snomed_map_rows"] == 0
    assert manifest["snomed_bridge_rows_built_but_withheld"] > 0, \
        "the bridge was not built, so recovery lost its evidence rather than just its table"

    recovered = {r[0] for r in duckdb.sql(
        f"SELECT normalized_code FROM read_csv_auto('{release / 'recovered_codes.csv'}')").fetchall()}
    assert "B021" in recovered, "a bridge-justified recovery disappeared under --icd-only"
    assert manifest["recovery"]["assignments_resting_solely_on_athena_evidence"] >= 1


def test_the_manifest_never_lists_a_file_it_did_not_ship(tmp_path: Path) -> None:
    """artifacts is derived from the directory, so withholding a file must not desync it."""
    release = _icd_only_release(tmp_path, "icdonly2")
    manifest = json.loads((release / "manifest.json").read_text())
    on_disk = {p.name for p in release.iterdir() if p.is_file() and p.name != "manifest.json"}
    assert set(manifest["artifacts"]) == on_disk
    assert not any("snomed" in name for name in manifest["artifacts"])


def test_an_icd_only_release_can_actually_be_packaged(tmp_path: Path) -> None:
    """The whole point: package_distribution.py refuses anything carrying SNOMED tables.

    Before --icd-only existed there was no way to reach this state, because recovery
    requires --athena-dir and that always wrote snomed_map.* -- so the recovered map
    could not be given to analysts at all.
    """
    release = _icd_only_release(tmp_path, "icdonly3")
    bundle = tmp_path / "bundle.tar.gz"
    result = subprocess.run([sys.executable, str(ROOT / "scripts/package_distribution.py"),
                             "--release", str(release), "--output", str(bundle)],
                            capture_output=True, text=True)
    assert result.returncode == 0, f"packaging refused an ICD-only release: {result.stdout}{result.stderr}"
    assert bundle.is_file()
    with tarfile.open(bundle) as archive:
        names = archive.getnames()
    assert not any("snomed" in n for n in names), "SNOMED content reached the analyst bundle"
    assert any(n.endswith("release/icd_map.parquet") for n in names)


def test_verify_release_accepts_an_icd_only_release(tmp_path: Path) -> None:
    """An analyst's first documented step must pass on what they were actually sent."""
    release = _icd_only_release(tmp_path, "icdonly4")
    result = subprocess.run([sys.executable, str(ROOT / "scripts/verify_release.py"),
                             "--release", str(release)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "ok"


def test_a_release_built_without_phecodex_info_is_still_usable(tmp_path: Path) -> None:
    """--phecodex-info is documented as optional; a site that omits it must not be stranded.

    It used to ship no phecode_info.parquet, and verify_release.py requires that file --
    so following the documentation produced a release that failed the analyst's first
    documented step. The documented default (sex=Both, blank text) is now materialised.
    """
    from conftest import write_csv
    from phecodex_mapper.vocabulary import build_vocabulary
    import duckdb

    source = tmp_path / "noinfo.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"],
              [["CV_003", "A01.1", "ICD10CM"], ["GI_001", "B02.2", "ICD10"]])
    release = tmp_path / "noinfo_rel"
    build_vocabulary(source, None, release, None)

    assert (release / "phecode_info.parquet").is_file()
    result = subprocess.run([sys.executable, str(ROOT / "scripts/verify_release.py"),
                             "--release", str(release)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr

    info = duckdb.sql(f"SELECT * FROM read_parquet('{release / 'phecode_info.parquet'}') "
                      "ORDER BY phecode").fetchall()
    assert info == [("CV_003",), ("GI_001",)], info
    # Deliberately phecode-only. A synthesised `sex` column of 'Both' would make a
    # release carrying no sex knowledge report release_has_sex_metadata=true, hiding
    # the condition that flag exists to expose.
    columns = {c[0] for c in duckdb.sql(
        f"DESCRIBE SELECT * FROM read_parquet('{release / 'phecode_info.parquet'}')").fetchall()}
    assert columns == {"phecode"}, f"default info must assert nothing it does not know, got {columns}"


def test_the_default_info_covers_every_phecode_in_the_map(tmp_path: Path) -> None:
    """A phecode present in the map but absent from info would be nameless and unsexed."""
    from conftest import write_csv
    from phecodex_mapper.vocabulary import build_vocabulary
    import duckdb

    source = tmp_path / "cover.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"],
              [[f"XX_{i:03d}", f"A0{i}.1", "ICD10CM"] for i in range(1, 6)])
    release = tmp_path / "cover_rel"
    build_vocabulary(source, None, release, None)
    missing = duckdb.sql(
        f"SELECT DISTINCT phecode FROM read_parquet('{release / 'icd_map.parquet'}') "
        f"EXCEPT SELECT phecode FROM read_parquet('{release / 'phecode_info.parquet'}')").fetchall()
    assert missing == [], f"phecodes in the map with no info row: {missing}"


def test_every_documented_command_is_one_the_cli_accepts() -> None:
    """Docs drift away from argparse silently, and the analyst finds out, not us.

    test_the_docs_reference_scripts_that_exist covers scripts/*; this covers the
    phecodex-map invocations, which are the commands an analyst actually types.
    """
    import shutil
    binary = shutil.which("phecodex-map") or str(ROOT / ".venv/bin/phecodex-map")
    invocations = []
    for doc in ("README.md", "ANALYST_GUIDE.md"):
        text = (ROOT / doc).read_text()
        for block in re.findall(r"```bash\n(.*?)```", text, re.S):
            joined = re.sub(r"\\\n\s*", " ", block)
            for line in joined.splitlines():
                # \b stops this matching the image names phecodex-mapper:1.1 / -1.1.sif
                m = re.search(r"phecodex-map\b(.*)", line.strip())
                if m and not line.strip().startswith("#"):
                    invocations.append((doc, m.group(1).split()))
    assert invocations, "no documented commands found; the extraction regex is probably wrong"

    problems = []
    for doc, args in invocations:
        if not args:
            continue
        sub = args[0]
        if sub.startswith("--"):
            continue
        help_text = subprocess.run([binary, sub, "--help"], capture_output=True, text=True)
        if help_text.returncode != 0:
            problems.append(f"{doc}: unknown subcommand {sub!r}")
            continue
        known = set(re.findall(r"(--[a-z0-9-]+)", help_text.stdout))
        unknown = [a for a in args if a.startswith("--") and a not in known]
        if unknown:
            problems.append(f"{doc}: {sub} does not accept {unknown}")
    assert problems == [], "documented commands the CLI would reject: " + "; ".join(problems)


def test_the_docs_show_how_to_build_the_release_analysts_receive() -> None:
    """The documented build must be the one that produces a packageable release.

    A build-vocabulary example without --icd-only produces a release
    package_distribution.py refuses, so an analyst following the docs cannot
    reach a bundle at all.
    """
    text = (ROOT / "README.md").read_text()
    builds = [b for b in re.findall(r"```bash\n(.*?)```", text, re.S) if "build-vocabulary" in b]
    assert builds, "README documents no build-vocabulary command"
    assert any("--icd-only" in b for b in builds), \
        "no documented build produces a release that package_distribution.py will accept"


def test_the_bundle_ships_the_licence(tmp_path: Path) -> None:
    """A permissive licence the recipient never receives grants them nothing."""
    release = _icd_only_release(tmp_path, "lic")
    bundle = tmp_path / "lic.tar.gz"
    subprocess.run([sys.executable, str(ROOT / "scripts/package_distribution.py"),
                    "--release", str(release), "--output", str(bundle)], check=True,
                   capture_output=True, text=True)
    with tarfile.open(bundle) as archive:
        names = archive.getnames()
        licence = archive.extractfile("phecodex-distribution/LICENSE").read().decode()
    assert "phecodex-distribution/LICENSE" in names
    assert "MIT License" in licence


def test_the_repo_declares_a_licence() -> None:
    """Without one the default is all-rights-reserved and no site may use this."""
    text = (ROOT / "LICENSE").read_text()
    assert "MIT License" in text and "Copyright (c)" in text
    assert 'license = "MIT"' in (ROOT / "pyproject.toml").read_text()

def test_every_phecode_named_by_the_prevalence_check_exists_and_is_restricted() -> None:
    """A named phecode that does not exist skips silently and proves nothing.

    Two identifiers were wrong on the first real run: CV_400 is rheumatic heart
    disease rather than atrial fibrillation and GI_530 is disease of anus and rectum
    rather than GORD, so both reported "out of band" on a run that was correct. A
    third, CA_111, is not a phecode at all and its sex check never executed -- a
    vacuous guard wearing the clothes of a real one.
    """
    import duckdb
    import importlib.util

    release = ROOT / "releases" / "phecodex-1.1-analyst"
    if not (release / "phecode_info.parquet").is_file():
        pytest.skip("no built release on this machine")

    spec = importlib.util.spec_from_file_location("cp", ROOT / "scripts/check_prevalence.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    info = duckdb.sql(
        f"SELECT phecode, sex FROM read_parquet('{release / 'phecode_info.parquet'}')").fetchall()
    known = {row[0]: row[1] for row in info}

    missing = sorted(p for p in module.EXPECTED if p not in known)
    assert missing == [], f"named phecodes absent from the release: {missing}"

    for phecode, expected_sex in module.SEX_CHECKS.items():
        assert phecode in known, f"{phecode} is not a phecode, so its sex check never runs"
        assert known[phecode] == expected_sex, \
            f"{phecode} is restricted to {known[phecode]}, not {expected_sex}"
