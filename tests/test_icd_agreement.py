"""scripts/phecodex_icd_agreement.py: planted-phenotype self-test (exact code, union, sex-restricted,
null, ignored vocabulary) and reconciliation of the count-based phi with numpy's correlation."""
import importlib.util, pathlib, pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "phecodex_icd_agreement.py"


pytest.importorskip("scipy"); pytest.importorskip("pandas")


def test_selftest_passes():
    spec = importlib.util.spec_from_file_location("icd_corr", SCRIPT)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    with pytest.raises(SystemExit) as e:
        mod.selftest()
    assert e.value.code == 0
