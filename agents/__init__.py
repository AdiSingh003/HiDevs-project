"""Autonomous B2B negotiation agents.

Layers (inside-out):

* ``agents.core``        - domain models, multi-attribute utility, Pareto / Nash maths, concession strategy
* ``agents.guardrails``  - Legal Arbiter / Safe AI: legal rulebook, private policy envelopes, message safety
* ``agents.negotiation`` - bounded negotiator agents, alternating-offers engine, mediator, RFQ, renegotiation
* ``agents.contract``    - contract compiler (JSON + PDF), Lyzr Automata drafting pipeline, signatures, SLA engine
* ``agents.audit``       - hash-chained audit ledger and the Lyzr AIMS sink
* ``agents.lyzr``        - Lyzr Agent API / RAI clients and the Studio provisioning CLI
"""

__version__ = "1.0.0"
