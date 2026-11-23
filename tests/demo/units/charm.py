"""A small stand-in for a charm's unit helpers."""


def unit_names(app, count):
    """Juju unit names of an application: app/0, app/1, ..."""
    return [f"{app}/{i}" for i in range(count)]
