"""Microstructure: order-book analytics, arrival intensity and queue outlook.

Phase 10, and the phase with the strictest entry condition in the platform. The
analytics here are the ones most often quoted without their preconditions —
"the book is imbalanced", "flow is clustering", "we are third in the queue" —
and each of them needs data of a specific shape to mean anything. So a dataset
is assessed *before* anything is computed on it, every capability is granted or
refused with a reason, and a refused capability has no endpoint that will
answer anyway.

The order of the modules is the order of the pipeline:

``models`` -> ``importer`` -> ``storage`` -> ``availability`` -> ``analytics`` /
``intensity`` / ``queue``.
"""
