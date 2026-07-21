"""Shared, dependency-light utilities for the webhooks app.

Modules in this package must stay importable without Django settings or
database access so that plugins can safely import them as well.
"""
