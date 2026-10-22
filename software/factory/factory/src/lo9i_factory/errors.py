"""Errors the service raises. They reach the model as tool errors and the
page as messages, so they name the valid choices."""


class FactoryError(Exception):
    pass


class ValidationError(FactoryError):
    pass


class NotFoundError(FactoryError):
    pass


class ConflictError(FactoryError):
    """The task isn't in a state that allows it."""
