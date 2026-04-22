"""HTTP/API layer — every FastAPI-related concern lives in this package.

Outside imports: use `from etfpulse.api.foo import ...` for web infrastructure.
Domain code (models/, adapters/, pipeline/, bot/) must never import from
etfpulse.api — keeps domain HTTP-agnostic and testable without FastAPI.
"""
