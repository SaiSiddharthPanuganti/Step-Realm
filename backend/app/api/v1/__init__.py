"""v1 of the public HTTP API.

Routers are mounted under ``/api/v1``. The root-level ``/health`` endpoint
lives in app.main rather than here, because orchestrators probe it before
the API surface is meaningful.
"""
