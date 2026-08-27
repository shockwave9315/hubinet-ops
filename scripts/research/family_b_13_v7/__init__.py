"""Family B experiment #13 -- dormant v7 authority-core foundation (S2).

Non-normative research package. Not architecture authority, not a
production contract, not an experiment result, not any part of a running
analyzer. See
``docs/architecture/research/blocker-b-family-b-13-authority-core-redesign.md``
for the redesign checkpoint this package is one stage of.

S2 scope (this package, current stage)
---------------------------------------
S2 builds STRUCTURAL FACTS only, in one direction::

    sealed decoded values (S1 primitives)
            -> typed structural records (records.py)
            -> PhysicalPos                (physical.py)
            -> ParticipantLifetime        (participants.py)
            -> ParticipantTable           (participants.py)
            -> STOP

S2 never admits evidence and never classifies authority. There is no
``admit()``, no observer ledger, no ``(ORIGIN x LEVEL)`` state, no
``ChronologySpec``/``PhaseSpec``, no ``CaptureValidity``/``T1Result``
classifier, and no analyzer-outcome projection anywhere in this package.
``ParticipantTable`` construction depends only on typed harness lifecycle
records; it takes no ground truth, no observer records, and no manifest
T0/T1/phase/interval context.

Layering (one-way only)
------------------------
::

    scripts/research/family_b_13_primitives.py (S1)
            |
            v
    scripts/research/family_b_13_v7/ (S2, this package)

This package may import ``scripts.research.family_b_13_primitives``. It
must never import the frozen v6 oracle, any S0 test fixture as executable
code, production ``app``/``custom_components`` code, or any later S3+
module (none of which exist yet). Neither S1 nor the frozen oracle may
import this package.
"""

from __future__ import annotations
