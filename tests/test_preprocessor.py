from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from escsim.renode.generator import WANTED
from escsim.target.preprocessor import PreprocessorError, preprocess_macros
from escsim.target.source import validate_targets_header


def test_conditionals_definitions_and_undefinitions():
    text = """
#define DEFAULT 1
#ifdef BOARD
#define FILE_NAME "BOARD"
#define VALUE 7
#else
#error wrong branch
#endif
#if defined(BOARD) && VALUE == 7
#define SELECTED yes
#endif
#ifndef MISSING
#undef DEFAULT
#define DEFAULT 2
#endif
"""
    macros = preprocess_macros(text, "BOARD")
    assert macros["FILE_NAME"] == '"BOARD"'
    assert macros["SELECTED"] == "yes"
    assert macros["DEFAULT"] == "2"


@pytest.mark.parametrize(
    "text, message",
    [
        ("#include <x>\n", "not permitted"),
        ("#ifdef A\n", "unterminated"),
        ("#else\n", "without #if"),
        ("#if 1 + 1\n#endif\n", "unsupported conditional"),
        ("#error stop\n", "stop"),
    ],
)
def test_rejects_unsupported_or_malformed_input(text, message):
    with pytest.raises(PreprocessorError, match=message):
        preprocess_macros(text, "BOARD")


def test_rejects_invalid_target_macro():
    with pytest.raises(PreprocessorError, match="invalid target"):
        preprocess_macros("", "BOARD; include secrets")


def test_matches_gcc_for_every_target_when_checkout_and_compiler_exist():
    gcc = shutil.which("arm-none-eabi-gcc")
    header = Path(__file__).parents[2] / "AM32.renode" / "Inc" / "targets.h"
    if gcc is None or not header.exists():
        pytest.skip("GCC parity prerequisites are not available")

    content = header.read_bytes()
    _, targets = validate_targets_header(content)
    text = content.decode()
    differences = []
    with tempfile.TemporaryDirectory() as directory:
        probe = Path(directory) / "target_probe.c"
        for target in targets:
            probe.write_text(f'#define {target}\n#include "targets.h"\n')
            result = subprocess.run(
                [gcc, "-E", "-dM", "-I", str(header.parent), str(probe)],
                check=True,
                capture_output=True,
                text=True,
            )
            expected = {}
            for line in result.stdout.splitlines():
                fields = line.split(None, 2)
                if len(fields) >= 2 and fields[0] == "#define" and fields[1] in WANTED:
                    expected[fields[1]] = fields[2].strip() if len(fields) > 2 else ""
            resolved = preprocess_macros(text, target)
            actual = {name: resolved[name] for name in WANTED if name in resolved}
            if actual != expected:
                differences.append(target)
    assert differences == []
