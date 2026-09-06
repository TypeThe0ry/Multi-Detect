from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_python_sources_are_checked_out_with_lf_line_endings() -> None:
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()

    assert "*.py text eol=lf" in attributes
    assert "*.pyi text eol=lf" in attributes


def test_manual_validation_installs_the_full_test_dependency_set() -> None:
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")

    assert 'python -m pip install -e ".[dev,vision,pixhawk,security,model-tools]"' in workflow
