import py_compile
from pathlib import Path
import pytest


@pytest.mark.skip(reason="Legacy file `bot (1).py` is not part of the current runtime stack")
def test_bot1_file_parses_without_syntax_error():
    target = Path(__file__).resolve().parents[1] / "bot (1).py"
    py_compile.compile(str(target), doraise=True)
