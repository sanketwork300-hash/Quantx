"""The historical data warehouse.

Everything that grows with market activity rather than user activity, held as
partitioned Parquet in the object store and queried analytically rather than
row by row. `docs/database.md` draws the line this package sits on: a tick tape
does not go in PostgreSQL.

What is in PostgreSQL is the **registry** — which datasets exist, what they
cover, how good they are, and where their partitions live. That is user-activity
sized, it is transactional, and it is what makes a dataset findable without
listing an object store.
"""
