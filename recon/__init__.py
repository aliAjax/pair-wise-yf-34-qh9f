"""Spot-check reconciliation package.

Layers, kept separate on purpose:

* :mod:`recon.judge`     — pure decision logic (判定)
* :mod:`recon.storage`   — persistence and schema (存储)
* :mod:`recon.service`   — orchestration, concurrency and retry semantics
* HTTP entry point lives in ``app.py`` and only delegates to the service.
"""
from .service import ReconciliationService

__all__ = ["ReconciliationService"]
