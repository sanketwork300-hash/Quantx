"""Numerical libraries.

Layering rule, enforced by ``scripts/check_layering.py``: this package imports
only the standard library and the scientific stack. It knows nothing about
users, portfolios, HTTP or databases. That is what allows
``tests/quant_validation`` to run with no infrastructure at all.
"""
