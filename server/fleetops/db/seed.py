"""Permanent bootstrap identities, generated once as UUIDv7 in Python.

These are identifiers, never credentials. Keeping them fixed lets the separately invoked
bootstrap command find precisely the HUMAN actor seeded by revision 0002.
"""

from uuid import UUID

ORGANIZATION_ID = UUID("01a0744a-17c4-7497-a69b-9da2aa8403da")
ACTOR_ID = UUID("01a0744a-17c5-72b0-a295-fdf4f7feb13d")
PARTY_ID = UUID("01a0744a-17c6-7996-bcbc-8d39271a6e0d")
PARTY_ROLE_ID = UUID("01a0744a-17c7-7ebb-b7b7-d212d20fc3a1")
