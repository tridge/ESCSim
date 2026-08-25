from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def load_runner():
    path = ROOT / "scripts" / "run-parity-tests.py"
    spec = importlib.util.spec_from_file_location("escsim_parity_runner", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_parity_sweep_records_all_results_before_failing(tmp_path, monkeypatch):
    runner = load_runner()

    class Repository:
        def catalog(self, refresh=False):
            assert refresh
            return {"channels": {"stable": {"firmware": "2.21"}}}

    calls = []

    def run_one(_repository, _renode, target, protocol, release, image_format):
        calls.append((target, protocol, release, image_format))
        if target == "FIRST" and protocol == "pwm" and image_format == "elf":
            raise RuntimeError("deliberate first-case failure")
        return {
            "target": target,
            "protocol": protocol,
            "format": image_format,
            "status": "passed",
        }

    monkeypatch.setattr(runner, "ArtifactRepository", lambda _url: Repository())
    monkeypatch.setattr(runner, "run_one", run_one)
    output = tmp_path / "report.json"
    status = runner.main(
        [
            "--base-url",
            "https://example.invalid/v1/",
            "--renode",
            str(tmp_path / "renode"),
            "--targets",
            "FIRST",
            "SECOND",
            "--protocols",
            "pwm",
            "dshot600",
            "--output",
            str(output),
        ]
    )

    assert status == 1
    assert calls == [
        ("FIRST", "pwm", "2.21", "elf"),
        ("FIRST", "dshot600", "2.21", "elf"),
        ("FIRST", "pwm", "2.21", "hex"),
        ("FIRST", "dshot600", "2.21", "hex"),
        ("SECOND", "pwm", "2.21", "elf"),
        ("SECOND", "dshot600", "2.21", "elf"),
        ("SECOND", "pwm", "2.21", "hex"),
        ("SECOND", "dshot600", "2.21", "hex"),
    ]
    report = json.loads(output.read_text())
    assert len(report["results"]) == 8
    assert report["results"][0] == {
        "target": "FIRST",
        "protocol": "pwm",
        "format": "elf",
        "status": "failed",
        "error": "deliberate first-case failure",
    }
    assert all(item["status"] == "passed" for item in report["results"][1:])


def test_parity_runner_finds_makefile_native_output(tmp_path, monkeypatch):
    runner = load_runner()
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner.platform, "system", lambda: "Linux")
    library = tmp_path / "build" / "package-native-linux" / "libam32sim.so"
    library.parent.mkdir(parents=True)
    library.write_bytes(b"native")
    assert runner.built_native_library() == library
