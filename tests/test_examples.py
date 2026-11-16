from pathlib import Path

from scripts.check_examples import load_records, scan_secrets, validate_records

SAMPLE = Path("examples/sft_sample.jsonl")


def test_sample_loads_and_validates():
    records = load_records(SAMPLE)
    assert len(records) >= 50
    assert validate_records(records) == []


def test_sample_has_no_secrets():
    assert scan_secrets(SAMPLE.read_text(encoding="utf-8")) == []


def test_scanner_catches_secrets():
    text = "key=sk-abcdefghijklmnopqrstuvwxyz phone=13912345678 mail=a@b.com /Users/someone/x"
    findings = scan_secrets(text)
    assert any(item.startswith("api_key") for item in findings)
    assert any(item.startswith("mobile") for item in findings)
    assert any(item.startswith("email") for item in findings)
    assert any(item.startswith("home_path") for item in findings)


def test_placeholders_are_allowed():
    assert scan_secrets("13800000000 010-00000000 user@example.com") == []
