"""Pydantic request/response models.

These are the wire contract. They are separate from the ORM models on
purpose: the ORM classes carry internal bookkeeping (password_hash,
last_mana_regen_at) that must never reach a client, and the client must
never be able to set server-owned fields by posting a body.
"""
