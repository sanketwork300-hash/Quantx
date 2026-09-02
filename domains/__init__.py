"""Domain layer.

Layering rule: a domain may import ``quant`` and ``infrastructure``, and may
import the *contracts* of other domains, but never another domain's ORM models.
"""
