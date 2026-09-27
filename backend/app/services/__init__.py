"""Business logic. One module per domain area.

Services own transaction boundaries and all validation. Route handlers are
expected to be thin: authenticate, delegate, translate to a schema.
"""
