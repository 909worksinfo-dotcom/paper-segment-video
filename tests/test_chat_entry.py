"""Only the conversation CLI turns saved citations into video jobs."""
import importlib.util
import json
from pathlib import Path
import pytest


def cli():
    spec = importlib.util.spec_from_file_location("studio_cli", Path(__file__).parents[1] / "scripts/studio.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_creates_from_saved_exact_selection(monkeypatch, capsys):
    module = cli()
    quote_id = "a" * 32
    selection = {"document_id": "doc", "page": 2, "text_range": {"start": {"page": 2, "line": 0, "offset": 1}, "end": {"page": 3, "line": 0, "offset": 3}}}
    calls = []
    def request(port, path, body=None):
        calls.append((path, body))
        if path == "/quotes/" + quote_id:
            return {"request": selection}
        return {"id": "job", "panel_path": "/?panel=1&job=job"}
    monkeypatch.setattr(module, "request", request)
    monkeypatch.setattr(module.sys, "argv", ["studio", "explain", "--quote-id", quote_id, "--port", "8767"])
    module.main()
    assert calls == [("/quotes/" + quote_id, None), ("/jobs", selection)]
    assert json.loads(capsys.readouterr().out)["panel_url"] == "http://127.0.0.1:8767/?panel=1&job=job"


def test_cli_rejects_invalid_reference_before_requests(monkeypatch):
    module = cli()
    monkeypatch.setattr(module.sys, "argv", ["studio", "explain", "--quote-id", "../jobs"])
    monkeypatch.setattr(module, "request", lambda *args: pytest.fail("invalid reference made a request"))
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
