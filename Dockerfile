FROM python:3.11.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /uvx /bin/
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt pyproject.toml ./
RUN uv pip install --system -r requirements.txt
COPY . .
# Dependency smoke test: fail build if critical interface missing
RUN python -c "from a2ui.a2a.parts import create_a2ui_part; import inspect; assert 'version' in inspect.signature(create_a2ui_part).parameters, 'FAIL: a2ui version param missing'; print('Dep smoke test OK')"
# Record installed versions for debugging
RUN uv pip freeze | grep -iE "^(google-adk|a2ui|mcp|google-genai|a2a-sdk)" | tee /app/.dep-versions
ENV PORT 8080
ENV GOOGLE_GENAI_USE_VERTEXAI=1
ENV PYTHONUNBUFFERED=1
ENV ADK_ENABLE_MCP_GRACEFUL_ERROR_HANDLING=1
# ADK 2.x: JSON_SCHEMA_FOR_FUNC_DECL (default ON) sends raw MCP JSON Schemas
# via parameters_json_schema, bypassing _to_gemini_schema and the
# _safe_dereference_schema patch. Recursive custom-MCP schemas (e.g. LINE
# flex messages) then hit Vertex's server-side flattener limit: deterministic
# 500 "Limits exceeded while trying to flatten schema" on EVERY call.
# Disabling restores the sanitized legacy conversion path.
ENV ADK_DISABLE_JSON_SCHEMA_FOR_FUNC_DECL=1
ENV OTEL_SDK_DISABLED=true
CMD ["uvicorn", "adk_agent.app.fast_api_app:app", "--host", "0.0.0.0", "--port", "8080"]
