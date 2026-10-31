# native_gateway

Read-only HTTPS gateway for the AICC Native Mac/iPhone client — see
`docs/aicc_native_gateway/GATEWAY_V1.md` for architecture, boundary
invariants and operations, and `docs/aicc_native_gateway/openapi.json` for
the frozen API contract.

Quick verification:

```
uv run --no-project --with-requirements requirements-gateway.txt \
    --with pytest --with 'httpx>=0.27,<1.0' \
    python -m pytest tests/native_gateway --confcutdir=tests/native_gateway
```

(`--confcutdir` keeps the hermetic run from importing the Streamlit app's
root conftest; in full CI, where those dependencies exist, the suite runs as
part of `tests/` normally.)
