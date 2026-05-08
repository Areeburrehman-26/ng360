import services


def test_services_package_docstring_exists():
    assert services.__doc__ is not None
