import utils


def test_utils_package_docstring_exists():
    assert utils.__doc__ is not None
