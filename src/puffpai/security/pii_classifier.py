"""
PuffPAI PII Classifier — shim module.

The ingestion pipeline imports ``PIIClassifier`` from this module to scan
uploaded documents for personally identifiable information so that
anything with high/critical severity gets auto-routed to the Zone 1
Vault instead of being indexed as a searchable note in plain text.

The actual implementation lives in ``data_classifier.DataClassifier``,
which already provides ``scan_for_pii`` and ``redact_pii``. This module
just re-exports it under the expected name, so ``ingestion.py`` never hits
an ``ImportError`` that would silently skip PII detection on every upload.
"""

from __future__ import annotations

from .data_classifier import DataClassifier, PIIDetection


# Public alias — the ingestion pipeline imports ``PIIClassifier``.
PIIClassifier = DataClassifier

__all__ = ["PIIClassifier", "PIIDetection"]
