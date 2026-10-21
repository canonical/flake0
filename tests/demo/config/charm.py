"""A small stand-in for a charm's config handling."""


def render_config(options):
    """The workload's config file from the charm's config options."""
    port = options.get("port", 8080)
    return "listen " + port + "\n"
