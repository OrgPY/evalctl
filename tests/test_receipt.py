from __future__ import annotations

import json
import os
import stat

CHECKS = [{"id": "has_report", "type": "file", "path": "report.md", "min_size": 1}]


def passed(proj, **top):
    proj.write("report.md", "# Report\nfine\n")
    proj.write("data/rows.csv", "a,b\n1,2\n")
    cfg = proj.config(CHECKS, **top)
    code, data = proj.check()
    assert code == 0 and data["receipt"]
    return cfg


def test_pass_writes_signed_receipt_with_expected_content(proj, home):
    cfg = passed(proj)
    rec = json.loads((proj.dir / ".eval" / "receipt.json").read_text())
    assert {a["path"] for a in rec["artifacts"]} == {"report.md", "data/rows.csv"}
    assert all(len(a["sha256"]) == 64 for a in rec["artifacts"])
    assert rec["config"]["name"] == "t" and rec["config"]["path"] == str(cfg.resolve())
    assert len(rec["config"]["sha256"]) == 64
    assert rec["passed_at"] and rec["round"] == 1
    assert rec["checks"] == [{"id": "has_report", "type": "file", "status": "pass", "summary": "1 file(s) OK"}]
    assert rec["signature"].startswith("hmac-sha256:")
    assert proj.verify() == (0, proj.verify()[1]) and proj.verify()[1]["valid"] is True


def test_key_is_created_once_with_mode_600(proj, home):
    passed(proj)
    key = home / "key"
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600
    first = key.read_bytes()
    proj.check()
    assert key.read_bytes() == first and len(first.strip()) >= 32


def test_no_receipt_when_checks_fail(proj):
    proj.config(CHECKS)
    proj.write("other.txt", "x")
    code, data = proj.check()
    assert code == 1 and data["receipt"] is None
    assert not (proj.dir / ".eval" / "receipt.json").exists()
    code, v = proj.verify()
    assert code == 1 and "has not passed" in v["reasons"][0]


def test_verify_fails_when_any_artifact_changes(proj):
    passed(proj)
    (proj.dir / "data" / "rows.csv").write_text("a,b\n1,3\n")
    code, v = proj.verify()
    assert code == 1 and v["reasons"] == ["data/rows.csv: content changed since the receipt was written"]


def test_verify_fails_when_artifact_deleted_or_added(proj):
    passed(proj)
    (proj.dir / "data" / "rows.csv").unlink()
    proj.write("extra.txt", "new")
    code, v = proj.verify()
    assert code == 1 and set(v["reasons"]) == {
        "data/rows.csv: deleted since the receipt was written",
        "extra.txt: new file since the receipt was written",
    }


def test_verify_fails_when_config_changes(proj):
    cfg = passed(proj)
    cfg.write_text(cfg.read_text().replace("min_size: 1", "min_size: 0"))
    code, v = proj.verify()
    assert code == 1 and v["reasons"] == ["check config 't' changed since the receipt was written"]


def test_verify_fails_when_python_check_module_changes(proj, home):
    mod = home / "checks" / "mod.py"
    mod.write_text("def ok(dir):\n    return True, 'ok'\n")
    proj.write("report.md", "x")
    proj.config([{"type": "python", "module": "mod.py", "function": "ok"}])
    assert proj.check()[0] == 0 and proj.verify()[0] == 0
    mod.write_text("def ok(dir):\n    return True, 'always'\n")
    code, v = proj.verify()
    assert code == 1 and "changed since the receipt" in v["reasons"][0]


def test_verify_fails_when_pointer_switches_to_another_config(proj):
    passed(proj)
    proj.config([{"type": "file", "path": "*"}], name="easy", pointer=True)
    code, v = proj.verify()
    assert code == 1 and "this directory now uses 'easy'" in v["reasons"][0]


def test_verify_detects_edited_or_forged_receipt(proj, home):
    passed(proj)
    rp = proj.dir / ".eval" / "receipt.json"
    rec = json.loads(rp.read_text())

    # Edit the output, then "fix" its hash in the receipt.
    (proj.dir / "report.md").write_text("changed")
    import hashlib
    for a in rec["artifacts"]:
        if a["path"] == "report.md":
            a["sha256"] = hashlib.sha256(b"changed").hexdigest()
    rp.write_text(json.dumps(rec))
    code, v = proj.verify()
    assert code == 1 and "signature does not match" in v["reasons"][0]

    # A receipt signed with a different key (e.g. produced under another EVALCTL_HOME).
    (home / "key").write_text("f" * 64 + "\n")
    code, v = proj.verify()
    assert code == 1 and "signature does not match" in v["reasons"][0]

    rp.write_text("not json")
    assert proj.verify()[0] == 1


def test_artifact_patterns_limit_what_the_receipt_covers(proj):
    passed(proj, artifacts=["report.md"])
    rec = json.loads((proj.dir / ".eval" / "receipt.json").read_text())
    assert [a["path"] for a in rec["artifacts"]] == ["report.md"]
    proj.write("scratch/notes.txt", "not an artifact")
    (proj.dir / "data" / "rows.csv").write_text("changed")
    assert proj.verify()[0] == 0
    (proj.dir / "report.md").write_text("changed")
    assert proj.verify()[0] == 1


def test_failed_recheck_removes_the_old_receipt(proj):
    passed(proj)
    (proj.dir / "report.md").write_text("")
    assert proj.check()[0] == 1
    assert not (proj.dir / ".eval" / "receipt.json").exists()


def test_receipt_copied_to_another_directory_is_invalid(proj, tmp_path):
    passed(proj)
    other = tmp_path / "other"
    (other / ".eval").mkdir(parents=True)
    (other / "report.md").write_text((proj.dir / "report.md").read_text())
    (other / "data").mkdir()
    (other / "data" / "rows.csv").write_text((proj.dir / "data" / "rows.csv").read_text())
    (other / ".evalctl.yaml").write_text("config: t\n")
    (other / ".eval" / "receipt.json").write_text((proj.dir / ".eval" / "receipt.json").read_text())
    code, out, _ = proj.run("verify", "--dir", str(other), "--json")
    assert code == 1 and "receipt was written for" in json.loads(out)["reasons"][0]
