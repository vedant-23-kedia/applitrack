"""Multi-user web service: anyone can connect their own Gmail."""

from .users import User, UserStore

__all__ = ["User", "UserStore", "create_app"]


def create_app(*args, **kwargs):
    """Imported lazily so the CLI does not need Flask installed."""
    from .app import create_app as _create_app

    return _create_app(*args, **kwargs)
