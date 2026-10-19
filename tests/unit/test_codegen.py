from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "codegen.py"


@pytest.fixture(scope="module")
def codegen() -> ModuleType:
    spec = importlib.util.spec_from_file_location("codegen_script", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_plain_http_spec_source_is_refused(
    codegen: ModuleType, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The spec becomes code committed to the repository: it must not be fetched unencrypted.
    def fetched(*_args: object, **_kwargs: object) -> None:
        pytest.fail("the spec was fetched over plain http")

    monkeypatch.setattr(codegen.urllib.request, "urlopen", fetched)
    with pytest.raises(SystemExit) as caught:
        codegen.stage_spec("http://spec.example/payments.yaml")
    assert caught.value.code == 1
    assert "https://" in capsys.readouterr().err
