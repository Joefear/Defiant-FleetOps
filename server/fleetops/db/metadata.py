"""Empty migration metadata; tenant-owned tables arrive in later slices (D18)."""

from sqlalchemy import MetaData

metadata = MetaData(schema="fleetops")
