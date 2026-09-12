"""Dory-wrangler v0.1 harness: the swappable single-agent launch boundary (#87).

Normative source: ../contract/v0.1/contract.md. Every module here implements
against that document; none of them amends it.

Layering, which is the point of this package:

    session_manager  ->  launch_boundary  <-  launchers/*
         |                                        (environment-specific)
         v
       store

`session_manager` and `store` contain no environment-specific code and import no
launcher. `launch_boundary` is the seam and contains no host, transport, or
bridge mechanics. Everything that knows how an agent is actually started lives
under `launchers/` and is selected by configuration alone.
"""
