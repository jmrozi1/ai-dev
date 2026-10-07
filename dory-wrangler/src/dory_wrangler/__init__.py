"""Dory-wrangler v0.1: the chat shell and its durable store.

Built against `dory-wrangler/contract/v0.1/contract.md`, whose executable form
is `dory-wrangler/validator/validate_contract.py`. This package implements the
chat shell (#86). It implements no launch boundary (#87) and no event rendering
or diagnostic surface (#88).
"""

__all__ = ["atomic", "contract", "errors", "ids", "service", "store", "webapp"]
