import core


def test_core_package_docstring_exists():
    assert core.__doc__ is not None
