"""Fail-closed errors for local application-document generation."""


class ReviewRequired(ValueError):
    """Human review is needed before document processing can continue."""
