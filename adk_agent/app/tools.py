import os
from typing import Union, Any
import asyncio
from google.adk.agents.readonly_context import ReadonlyContext
import dotenv
import google.auth
import google.auth.transport.requests
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.adk.tools.mcp_tool.mcp_tool import MCPTool
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
import httpx
from google.adk.auth import AuthCredential, AuthCredentialTypes, OAuth2Auth
import anyio
import time
import uuid
from google.adk.tools import ToolContext
from google.genai import client as genai_client, types as genai_types
import json
from fastapi.openapi.models import OAuth2, OAuthFlows, OAuthFlowAuthorizationCode

_orig_default = json.JSONEncoder.default
def _patched_default(self, obj):
    if isinstance(obj, genai_types.Part):
        return obj.model_dump(exclude_none=True)
    return _orig_default(self, obj)
json.JSONEncoder.default = _patched_default





def get_project_id():
    """Robustly retrieves the project ID from env, .env, or credentials."""
    # 1. Direct env
    pid = os.getenv("GOOGLE_CLOUD_PROJECT")
    if pid: return pid
    
    # 2. Try loading .env from root or package
    dotenv.load_dotenv()
    pid = os.getenv("GOOGLE_CLOUD_PROJECT")
    if pid: return pid
    
    # 3. Fallback to auth default
    try:
        _, pid = google.auth.default()
        if pid: return pid
    except: pass
    return "UNKNOWN"

# =============================================================================
# 🛡️ Stability Patches for Reasoning Engine (Mandatory)
# =============================================================================

_orig_client_init = httpx.AsyncClient.__init__
def _patched_client_init(self, *args, **kwargs):
    kwargs['http2'] = False 
    # Use long timeouts for stable MCP sessions (300s)
    kwargs['timeout'] = httpx.Timeout(300.0, connect=60.0)
    return _orig_client_init(self, *args, **kwargs)

_token_cache = {"token": None, "expiry": 0, "credentials": None}
_token_lock = asyncio.Lock()

async def _get_fresh_mcp_token():
    """Retrieves a fresh access token with async-safe caching."""
    global _token_cache
    async with _token_lock:
        now = time.time()
        if _token_cache["token"] and now < _token_cache["expiry"]:
            return _token_cache["token"]
        try:
            if _token_cache["credentials"] is None:
                # google.auth.default() makes blocking network calls. We run it in a thread
                # to prevent it from deadlocking the main asyncio event loop if the metadata server hangs.
                def _get_creds():
                    scopes = ["https://www.googleapis.com/auth/cloud-platform", "https://www.googleapis.com/auth/bigquery"]
                    creds, _ = google.auth.default(scopes=scopes)
                    return creds
                _token_cache["credentials"] = await anyio.to_thread.run_sync(_get_creds)
            
            credentials = _token_cache["credentials"]
            
            # CRITICAL: google.auth's Request does not accept a timeout in its constructor,
            # and defaults to infinite timeout. This hangs the worker thread and deadlocks the
            # entire asyncio TaskGroup on Cloud Run cold starts. We must inject a custom session.
            import requests
            class TimeoutSession(requests.Session):
                def request(self, *args, **kwargs):
                    kwargs.setdefault('timeout', 10.0)
                    return super().request(*args, **kwargs)
                    
            req = google.auth.transport.requests.Request(session=TimeoutSession())
            await anyio.to_thread.run_sync(credentials.refresh, req)
            _token_cache = {"token": credentials.token, "expiry": now + 1800, "credentials": credentials}
            return credentials.token
        except Exception as e: 
            import logging
            logging.warning(f"Failed to refresh MCP token: {e}")
            return ""

_orig_send = httpx.AsyncClient.send
async def _patched_send(self, request, *args, **kwargs):
    _url = str(request.url)
    
    # BigQuery & Firestore MCP Auth Injection
    if "bigquery.googleapis.com/mcp" in _url or "firestore.googleapis.com/mcp" in _url:
        token = await _get_fresh_mcp_token()
        if token: request.headers['Authorization'] = f"Bearer {token}"
            


    # Execute actual request
    response = await _orig_send(self, request, *args, **kwargs)
    
    # Error Transmutation (JSON-RPC Protocol Compliance)
    # MCP uses JSON-RPC, which requires all responses (including errors) to be HTTP 200.
    # Google's MCP endpoints sometimes return HTTP 400/403 for JSON-RPC errors (e.g., 
    # invalid SQL, permission denied, DML failures). If we don't convert these to HTTP 200,
    # the HTTP transport layer in ADK rejects them before the LLM can see the error details.
    # By converting to 200, the JSON-RPC error payload reaches the LLM, which can then
    # report the actual error (e.g., "Column not found") and attempt recovery.
    if response.status_code in [400, 403] and ("bigquery.googleapis.com/mcp" in _url or "firestore.googleapis.com/mcp" in _url):
        try:
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > 0 or not chunk:
                    break
            # Only transmute if the body is a valid JSON-RPC response
            if b'"jsonrpc":' in body: response.status_code = 200
            response._content = body
        except Exception: 
            pass
    return response

# Apply Stability Patches
try:
    # 1. HTTP/2 Disable for stability
    httpx.AsyncClient.__init__ = _patched_client_init
    httpx.AsyncClient.send = _patched_send
    
    # 2. MCP Cancel-Scope Fix (backport for ADK <=1.31.1)
    # ADK's SessionContext._run() wraps client context entry in asyncio.wait_for(),
    # which runs in a nested task. AnyIO's CancelScope must be entered/exited in the
    # same task, so this causes "Attempted to exit cancel scope in a different task".
    # The fix (from ADK main branch) removes the wait_for wrapper.
    # When ADK ships the _MCP_GRACEFUL_ERROR_HANDLING flag, the env var takes over.
    from google.adk.tools.mcp_tool.session_context import SessionContext as _SC
    _orig_sc_run = _SC._run
    async def _patched_sc_run(self):
        try:
            async with __import__('contextlib').AsyncExitStack() as exit_stack:
                # NO asyncio.wait_for here — this is the fix
                transports = await exit_stack.enter_async_context(self._client)
                from datetime import timedelta
                if self._is_stdio:
                    session = await exit_stack.enter_async_context(
                        __import__('mcp').ClientSession(
                            *transports[:2],
                            read_timeout_seconds=timedelta(seconds=self._timeout)
                            if self._timeout is not None else None,
                            sampling_callback=getattr(self, '_sampling_callback', None),
                            sampling_capabilities=getattr(self, '_sampling_capabilities', None),
                        )
                    )
                else:
                    _srt = getattr(self, '_sse_read_timeout', None) or self._timeout
                    session = await exit_stack.enter_async_context(
                        __import__('mcp').ClientSession(
                            *transports[:2],
                            read_timeout_seconds=timedelta(seconds=_srt)
                            if _srt is not None else None,
                            sampling_callback=getattr(self, '_sampling_callback', None),
                            sampling_capabilities=getattr(self, '_sampling_capabilities', None),
                        )
                    )
                _init_timeout = max(self._timeout or 60, 60)  # At least 60s for custom MCP sidecars
                await asyncio.wait_for(session.initialize(), timeout=_init_timeout)
                import logging as _log
                _log.getLogger('google_adk.session_context').debug('Session initialized (patched)')
                self._session = session
                self._ready_event.set()
                await self._close_event.wait()
        except BaseException as e:
            import logging as _log
            _logger = _log.getLogger('google_adk.session_context')
            _logger.warning(f'Error on session runner task: {e}')
            # Log sub-exceptions for TaskGroup/ExceptionGroup errors
            if hasattr(e, 'exceptions'):
                for i, sub_ex in enumerate(e.exceptions):
                    _logger.warning(f'  Sub-exception [{i}]: {type(sub_ex).__name__}: {sub_ex}')
                    if hasattr(sub_ex, 'exceptions'):
                        for j, sub_sub in enumerate(sub_ex.exceptions):
                            _logger.warning(f'    Sub-sub-exception [{i}.{j}]: {type(sub_sub).__name__}: {sub_sub}')
            import traceback
            _logger.debug(f'Full traceback: {traceback.format_exc()}')
            raise
        finally:
            self._ready_event.set()
            self._close_event.set()
    _SC._run = _patched_sc_run
except Exception as e:
    import logging; logging.warning(f"Stability patches not applied: {e}")

# =============================================================================
# 3. Deterministic-5xx Fast-Fail (v10.71)
# Vertex returns 500 INTERNAL "Limits exceeded while trying to flatten
# schema. Schema is too complex to process." when a tool declaration cannot
# be compiled server-side (e.g. a recursive custom-MCP schema reached the
# API raw). The error is DETERMINISTIC for a given toolset, yet
# HttpRetryOptions treats every 500 as transient: tenacity burns attempts=8
# (~4 min) per LLM call, and the synth-salvage repeats it 3 more times --
# a ~16-minute hang ending in ServerError (confirmed 2026-06-10,
# demo-demand-inventor + line-bot-mcp-server).
# Patching at the google.genai errors layer (not httpx) is transport-
# agnostic: it works whether the SDK picks aiohttp or httpx. Demoting the
# status to 400 takes it out of the retriable code set ([429, 500, 503]),
# so the call fails in ~1s and the executor's salvage path takes over.
# The original error message is preserved inside response_json for logs.
# =============================================================================
try:
    from google.genai import errors as _genai_errors

    _DETERMINISTIC_5XX_MARKERS = (
        "flatten schema",
        "Schema is too complex",
    )

    def _is_deterministic_5xx(status_code, response_json):
        try:
            if not (500 <= int(status_code or 0) < 600):
                return False
        except Exception:
            return False
        try:
            _msg = str((response_json or {}).get("error", {}).get("message", ""))
        except Exception:
            _msg = str(response_json)
        return any(_m in _msg for _m in _DETERMINISTIC_5XX_MARKERS)

    _orig_raise_error = _genai_errors.APIError.raise_error.__func__
    _orig_raise_error_async = _genai_errors.APIError.raise_error_async.__func__

    @classmethod
    def _patched_raise_error(cls, status_code, response_json, response):
        if _is_deterministic_5xx(status_code, response_json):
            raise _genai_errors.ClientError(400, response_json, response)
        _orig_raise_error(cls, status_code, response_json, response)

    @classmethod
    async def _patched_raise_error_async(cls, status_code, response_json, response):
        if _is_deterministic_5xx(status_code, response_json):
            raise _genai_errors.ClientError(400, response_json, response)
        await _orig_raise_error_async(cls, status_code, response_json, response)

    _genai_errors.APIError.raise_error = _patched_raise_error
    _genai_errors.APIError.raise_error_async = _patched_raise_error_async
except Exception as e:
    import logging; logging.warning(f"Deterministic-5xx fast-fail patch not applied: {e}")

# =============================================================================
# 🔧 MCP Toolset Configuration
# =============================================================================
def get_maps_mcp_url():
    """Returns the base Maps MCP URL."""
    return "https://mapstools.googleapis.com/mcp"

def get_firestore_mcp_url():
    """Returns the base Firestore MCP URL."""
    return "https://firestore.googleapis.com/mcp"

def get_bigquery_mcp_url():
    """Returns the project-scoped BigQuery MCP URL using a query parameter."""
    project_id = get_project_id()
    # Using ?project= query parameter as the header alone was insufficient for public datasets
    return f"https://bigquery.googleapis.com/mcp?project={project_id}"

def get_bigquery_mcp_toolset():
    """Creates a BigQuery MCP toolset. URL is project-scoped to ensure quota/perms."""
    project_id = get_project_id()
    url = get_bigquery_mcp_url()
    if project_id == "UNKNOWN":
        print("  [CRITICAL] GOOGLE_CLOUD_PROJECT is missing! MCP calls will likely fail.")
        
    return McpToolset(connection_params=StreamableHTTPConnectionParams(
        url=url, 
        headers={"x-goog-user-project": project_id},
        timeout=300
    ))

def get_firestore_mcp_toolset():
    """Creates a Firestore MCP toolset (data ops only; DB/index admin excluded
    to reduce the agent's function-declaration count -- admin ops are handled by
    the setup script, never by the runtime agent)."""
    project_id = get_project_id()
    url = get_firestore_mcp_url()
    return McpToolset(connection_params=StreamableHTTPConnectionParams(
        url=url,
        headers={"x-goog-user-project": project_id},
        timeout=300
    ), tool_filter=[
        'get_document', 'add_document', 'update_document', 'delete_document',
        'list_documents', 'list_collections',
    ])

def get_maps_mcp_toolset():
    """Creates a Google Maps MCP toolset."""
    dotenv.load_dotenv()
    maps_api_key = os.getenv('MAPS_API_KEY')
    project_id = get_project_id()
    url = get_maps_mcp_url()
    return McpToolset(connection_params=StreamableHTTPConnectionParams(
        url=url, 
        headers={
            "x-goog-api-key": maps_api_key
        },
        timeout=300
    ))

# Initialize Firestore client for background task management
# Stored on builtins so tools.py functions can access it without circular imports
# NOTE: This MUST be outside the enableWorkspaceMcp conditional block
# so background task tools work regardless of Workspace MCP configuration.
import builtins
if not hasattr(builtins, '_firestore_client'):
    try:
        from google.cloud import firestore as _firestore_mod
        builtins._firestore_client = _firestore_mod.Client()
        print("[tools.py] Firestore client initialized successfully for background tasks", flush=True)
    except Exception as _fs_init_err:
        builtins._firestore_client = None
        print("[tools.py] FAILED to initialize Firestore client: " + type(_fs_init_err).__name__ + ": " + str(_fs_init_err), flush=True)



async def generate_image(prompt: str, tool_context: ToolContext) -> dict:
    """Generates a professional business image or presentation slide based on the given prompt.
    
    This tool creates visual assets like infographics, charts, or slides. It automatically 
    stores the image in the current environment's artifact service to be rendered in the chat.
    
    Args:
        prompt: A highly detailed, descriptive prompt for the image. Include stylistic instructions (e.g., 'photorealistic', 'flat design').
                CRITICAL: The prompt text MUST be written in the EXACT SAME language that the user is using in the current chat session.
                If the conversation is in Japanese, you MUST write the entire prompt in Japanese (e.g., '武田電気株式会社の見積状況をまとめたスライド...').
                This ensures all text inside the generated image is rendered in the user's language.
        
    Returns:
        A dictionary with status and detail keys.
    """
    filename = f"image_{uuid.uuid4().hex[:8]}.jpeg"
    
    import os
    import logging
    import re
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    
    logging.info(f"generate_image called with prompt: {prompt}")
    logging.info(f"Using location: {location}, project: {project}")
    
    # 1. Automatic language detection on the prompt text (Detect Japanese characters)
    is_japanese = bool(re.search(r'[぀-ゟ゠-ヿ一-龯]', prompt))
    
    # 2. Construct robust system-level style and language guidelines based on detected language
    base_style_rule = "\n\nCRITICAL STYLE RULE: NEVER include headers, watermarks, logos, or any text reading 'Consulting Firm' in the generated image."
    
    if is_japanese:
        # Heavy reinforcement for Japanese rendering (Forces Imagen 3 to use Japanese fonts and text labels exclusively)
        lang_rule = (
            "\n\nCRITICAL LANGUAGE RULE: ALL text elements inside the generated image "
            "(including presentation titles, headers, table labels, chart legends, data points, bullet points, annotations, and company names) "
            "MUST be rendered EXCLUSIVELY in Japanese. Do NOT use any English or Latin characters. "
            "For example, render company names as '武田電気株式会社' (not Takeden Co), "
            "and use Japanese for headers like 'エグゼクティブサマリー' or '保留中の見積処理状況'. "
            "This is a strict requirement."
        )
    else:
        lang_rule = (
            "\n\nCRITICAL LANGUAGE RULE: ALL text elements inside the generated image "
            "(including titles, labels, axis names, legends, bullet points, annotations, captions) "
            "MUST be rendered in the SAME language as the prompt text above. Do NOT mix languages."
        )
        
    final_prompt = prompt + base_style_rule + lang_rule
    
    client = genai_client.Client(
        vertexai=True, 
        location=location, 
        project=project,
        http_options={'api_version': 'v1'}
    )
    from google.genai import types
    
    try:
        logging.info("Calling Gemini API for image generation...")
        # Generate image via the GenerateContent API
        result = await asyncio.to_thread(
            client.models.generate_content,
            model='gemini-3.1-flash-image',
            contents=[
                types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=final_prompt)]
                )
            ],
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(
                    aspect_ratio="16:9",
                    output_mime_type="image/jpeg",
                )
            )
        )
        logging.info("Gemini API call returned.")
    except Exception as e:
        logging.error(f"API Error generating image: {e}", exc_info=True)
        return {'status': 'error', 'detail': f'API Error generating image: {str(e)}'}
    
    if not result.candidates or not result.candidates[0].content.parts:
        logging.warning(f"Failed to generate image for prompt: {prompt}. No candidates or parts.")
        return {'status': 'error', 'detail': f'Failed to generate image for prompt: {prompt}'}
        
    image_bytes = None
    for part in result.candidates[0].content.parts:
        if part.inline_data:
            image_bytes = part.inline_data.data
            break
            
    if not image_bytes:
        logging.warning(f"No image bytes found in the response for prompt: {prompt}")
        return {'status': 'error', 'detail': f'No image bytes found in the response for prompt: {prompt}'}
    
    # Store the image bytes in the session state so the callback can pick it up later
    tool_context.session.state['pending_generated_image'] = image_bytes
    
    return {
        'status': 'success',
        'detail': 'Image generated successfully. It will be attached to your final response automatically.',
    }

def get_custom_mcp_toolsets():

    return []




# =============================================================================
# Background Task Management (Long-Running Agent Orchestration)
# =============================================================================
import uuid as _task_uuid
import datetime as _task_dt
from google.adk.tools import LongRunningFunctionTool

def register_background_task(
    task_name: str,
    task_description: str,
    task_prompt: str,
    tool_context: ToolContext,
) -> dict:
    """Register a background task for async execution. CRITICAL RULES:
    1. Call this tool EXACTLY ONCE per user request — never split a workflow into multiple tasks.
    2. task_prompt MUST contain ALL workflow steps (SCAN, CLASSIFY, PROCESS, AUDIT, etc.)
       as a complete, self-contained instruction. The background agent uses ONLY task_prompt
       to execute the entire workflow autonomously.
    3. A second call while a task is still ACTIVE (pending/working/submitted) will be BLOCKED.
       Completed, failed, or cancelled tasks CAN be re-registered with a new call.

    Args:
        task_name: Short identifier for the ENTIRE workflow (e.g. 'store_optimization_workflow').
        task_description: Summary of the complete workflow scope.
        task_prompt: COMPLETE, SELF-CONTAINED instruction covering ALL steps from scan to audit.
                     This is the ONLY input the background agent receives. Include data queries,
                     business rules, success criteria, and reporting requirements for every step.

    Returns:
        dict with ticket-id and status.
    """
    # --- Structural guard: block recursive delegation ---
    # Background workers (user_id="background-worker") must execute tasks
    # directly using data tools, not re-register them as new background tasks.
    if tool_context.user_id == "background-worker":
        return {
            "status": "blocked",
            "message": "Cannot register background tasks from within a background worker. "
                       "Execute operations directly using data tools (get_document, update_document, list_documents, execute_sql, etc.).",
        }

    # --- F1 (v10.64): block the inline deep_analysis specialist from escalating ---
    # deep_analysis_agent runs INLINE. It sometimes self-escalates a long analysis
    # into a background task and then polls it, so the inline turn NEVER returns
    # (the user sees a permanent "thinking" hang). It is the inline EXECUTOR, not a
    # scheduler: forbid background registration from this agent.
    _caller_agent = getattr(tool_context, 'agent_name', None) or ''
    if not _caller_agent:
        try:
            _caller_agent = tool_context._invocation_context.agent.name
        except Exception:
            _caller_agent = ''
    if _caller_agent == 'deep_analysis_agent':
        return {
            "status": "blocked",
            "message": "deep_analysis_agent runs INLINE and must NOT register background tasks. "
                       "Complete the analysis directly with data tools and deliver the FINAL report "
                       "in THIS turn. Do not call register_background_task, get_task_result, or "
                       "list_background_tasks.",
        }

    return submit_background_task_now(task_name, task_description, task_prompt)


def submit_background_task_now(task_name: str, task_description: str, task_prompt: str) -> dict:
    """Create and fire a background task WITHOUT the agent-level guards.

    Shared core of register_background_task (the agent tool). Also called
    directly (no ToolContext) by the inline-overrun conversion watchdog in
    fast_api_app.py: when an inline turn exceeds the chat rendering deadline,
    the executor moves the pressed "Run Inline:" intent here so the user
    still receives the full report as a background task.
    """
    import builtins
    _fs = getattr(builtins, '_firestore_client', None)
    _demo_id = os.environ.get("DEMO_ID", "")

    _task_id = str(_task_uuid.uuid4())[:8]
    _now = _task_dt.datetime.now(_task_dt.timezone.utc)
    _now_iso = _now.isoformat()


    _def_doc = {
        "task_id": _task_id,
        "task_name": task_name,
        "task_description": task_description,
        "task_prompt": task_prompt,
        "task_type": "immediate",
        "created_at": _now_iso,
    }
    _exec_doc = {
        "task_id": _task_id,
        "definition_id": _task_id,
        "status": "submitted",
        "progress_pct": 0,
        "log_tail": "",
        "result_summary": "",
        "started_at": "",
        "completed_at": "",
        "reported_to_user": False,
    }

    import logging as _flog
    _bg_logger = _flog.getLogger("bg_task")

    if not _fs or not _demo_id:
        _bg_logger.error("register_background_task: PRECONDITION FAILED fs=%s demo_id=%s", bool(_fs), repr(_demo_id))
        return {
            "status": "error",
            "message": "Cannot register background task: Firestore client unavailable (client="
                       + str(bool(_fs)) + ", demo_id=" + repr(_demo_id) + "). "
                       + "The task management backend is not configured for this demo.",
        }

    # --- Duplicate task guard ---
    # Block registration if an ACTIVE task with the same task_name exists.
    # This prevents button-spam from creating duplicate tasks.
    # Names are NORMALIZED (lowercased, all non-alphanumerics stripped) before
    # comparison so cosmetic variants are treated as the SAME task. The model
    # sometimes emits register_background_task twice in one turn with names that
    # differ only in case/separators (e.g. "Apex_Contract_Health_Analysis" vs
    # "apex_contract_health_analysis"), which an exact-match guard let through.
    def _norm_task_name(_s):
        return "".join(_c for _c in str(_s).lower() if _c.isalnum())
    _norm_new_name = _norm_task_name(task_name)
    try:
        _active_statuses = ("submitted", "working")
        _existing_execs = _fs.collection(_demo_id + "_task_executions").where(
            "status", "in", list(_active_statuses)
        ).stream()
        for _edoc in _existing_execs:
            _edata = _edoc.to_dict()
            _existing_def_id = _edata.get("definition_id", "")
            if _existing_def_id:
                try:
                    _def_ref = _fs.collection(_demo_id + "_task_definitions").document(_existing_def_id).get()
                    if _def_ref.exists:
                        _def_data = _def_ref.to_dict()
                        if _norm_task_name(_def_data.get("task_name")) == _norm_new_name:
                            _bg_logger.warning(
                                "register_background_task: BLOCKED duplicate task_name=%s (existing task_id=%s status=%s)",
                                task_name, _edata.get("task_id", "?"), _edata.get("status", "?")
                            )
                            return {
                                "status": "already_active",
                                "ticket-id": _edata.get("task_id", _existing_def_id),
                                "task_name": task_name,
                                "message": "A task with the same name is already active (status: "
                                           + _edata.get("status", "unknown") + "). "
                                           + "Use get_task_result or list_background_tasks to check its progress.",
                            }
                except Exception:
                    pass
    except Exception as _dup_err:
        _bg_logger.warning("register_background_task: duplicate check failed (non-fatal): %s", str(_dup_err)[:200])

    try:
        _fs.collection(_demo_id + "_task_definitions").document(_task_id).set(_def_doc)
        _fs.collection(_demo_id + "_task_executions").document(_task_id).set(_exec_doc)
        _bg_logger.warning("register_background_task: Firestore docs written task_id=%s", _task_id)
    except Exception as _fs_err:
        _bg_logger.error("register_background_task: Firestore write FAILED: %s", str(_fs_err)[:300])
        return {
            "status": "error",
            "message": "Failed to register task: Firestore write error. " + str(_fs_err)[:200],
        }

    # Fire-and-forget: trigger worker endpoint via localhost
    # IMPORTANT: Do NOT use SELF_URL (public *.run.app URL) for self-calls.
    # Cloud Run --ingress internal blocks requests from the container's own
    # public URL because they exit via the internet and re-enter as "external".
    # Using localhost:PORT keeps the request inside the container.
    import threading as _threading
    import requests as _requests
    _port = os.environ.get("PORT", "8080")
    _worker_url = "http://localhost:" + _port + "/execute_task"

    def _fire():
        import logging as _log
        _logger = _log.getLogger("bg_task")
        _logger.warning("_fire: SENDING request worker_url=%s task_id=%s demo_id=%s", _worker_url, _task_id, _demo_id)
        try:
            _headers = {"Content-Type": "application/json"}
            # Use short read timeout (0.5s): this is fire-and-forget.
            # The execute_task endpoint runs the agent asynchronously;
            # we only need to confirm the request was accepted, not wait for completion.
            _resp = _requests.post(_worker_url + "?task_id=" + _task_id + "&demo_id=" + _demo_id, json={"task_id": _task_id, "demo_id": _demo_id}, headers=_headers, timeout=(5, 0.5))
            _logger.warning("_fire: response status=%s body=%s", _resp.status_code, _resp.text[:300])
        except _requests.exceptions.ReadTimeout:
            # Expected: the worker is processing asynchronously.
            _logger.warning("_fire: request accepted (ReadTimeout expected for async), task_id=%s", _task_id)
        except _requests.exceptions.ConnectionError as _ce:
            _logger.error("_fire CONNECTION_ERROR: server may not be ready. task_id=%s err=%s", _task_id, str(_ce)[:300])
        except Exception as _e:
            _logger.error("_fire FAILED: %s: %s", type(_e).__name__, str(_e)[:500])
    _threading.Thread(target=_fire, daemon=True).start()



    return {
        "status": "submitted",
        "ticket-id": _task_id,
        "task_name": task_name,
        "message": "Task registered. Processing started in background.",
    }

background_task_tool = LongRunningFunctionTool(func=register_background_task)


def list_background_tasks(tool_context: ToolContext) -> dict:
    """Lists all background tasks and their current status.

    Returns:
        dict with list of tasks.
    """
    import builtins
    _fs = getattr(builtins, '_firestore_client', None)
    _demo_id = os.environ.get("DEMO_ID", "")
    if not _fs or not _demo_id:
        return {"tasks": [], "error": "Firestore not available (client=" + str(bool(_fs)) + ", demo_id=" + repr(_demo_id) + ")"}
    try:
        _docs = _fs.collection(_demo_id + "_task_executions").order_by(
            "started_at", direction="DESCENDING"
        ).limit(20).stream()
        _tasks = []
        for _doc in _docs:
            _d = _doc.to_dict()
            _tasks.append({
                "task_id": _d.get("task_id"),
                "status": _d.get("status"),
                "progress_pct": _d.get("progress_pct", 0),
                "result_summary": _d.get("result_summary", "")[:200],
            })
        return {"tasks": _tasks, "total": len(_tasks)}
    except Exception as _fs_err:
        return {"tasks": [], "error": "Firestore query failed: " + str(_fs_err)[:200]}


def get_task_result(task_id: str, tool_context: ToolContext) -> dict:
    """Gets the detailed result of a specific background task.

    Args:
        task_id: The ticket-id returned from register_background_task.

    Returns:
        dict with status, progress, and result.
    """
    import builtins
    _fs = getattr(builtins, '_firestore_client', None)
    _demo_id = os.environ.get("DEMO_ID", "")
    if not _fs or not _demo_id:
        return {"error": "Firestore not available (client=" + str(bool(_fs)) + ", demo_id=" + repr(_demo_id) + ")"}
    try:
        _ref = _fs.collection(_demo_id + "_task_executions").document(task_id)
        _doc = _ref.get()
        if not _doc.exists:
            return {"error": "Task not found: " + task_id}
        _d = _doc.to_dict()
        # Mark as reported
        if _d.get("status") in ("completed", "failed") and not _d.get("reported_to_user"):
            try:
                _ref.update({"reported_to_user": True})
            except Exception:
                pass  # Non-critical: best-effort mark
        return {
            "task_id": _d.get("task_id"),
            "status": _d.get("status"),
            "progress_pct": _d.get("progress_pct", 0),
            "result_summary": _d.get("result_summary", ""),
            "log_tail": _d.get("log_tail", ""),
            "started_at": _d.get("started_at", ""),
            "completed_at": _d.get("completed_at", ""),
            "_MANDATORY_ACTION": "YOU MUST present result_summary below as formatted markdown text in your response. "
                "Output the result_summary content VERBATIM as text. Do NOT skip it. Do NOT output only suggestion chips. "
                "If your response contains NO text and only A2UI JSON, you have FAILED.",
        }
    except Exception as _fs_err:
        return {"error": "Firestore read failed: " + str(_fs_err)[:200]}


def cancel_background_task(task_id: str, tool_context: ToolContext) -> dict:
    """Cancels a pending or running background task.

    Args:
        task_id: The ticket-id of the task to cancel.

    Returns:
        dict with cancellation status.
    """
    import builtins
    _fs = getattr(builtins, '_firestore_client', None)
    _demo_id = os.environ.get("DEMO_ID", "")
    if not _fs or not _demo_id:
        return {"error": "Firestore not available (client=" + str(bool(_fs)) + ", demo_id=" + repr(_demo_id) + ")"}
    try:
        _ref = _fs.collection(_demo_id + "_task_executions").document(task_id)
        _doc = _ref.get()
        if not _doc.exists:
            return {"error": "Task not found: " + task_id}
        _status = _doc.to_dict().get("status", "")
        if _status in ("completed", "failed", "cancelled"):
            return {"error": "Task already in terminal state: " + _status}
        _ref.update({"status": "cancelled"})
        return {"status": "cancelled", "task_id": task_id}
    except Exception as _fs_err:
        return {"error": "Firestore operation failed: " + str(_fs_err)[:200]}


def update_task_progress(
    task_id: str,
    current_step: str,
    progress_pct: int,
    log_entry: str,
    tool_context: ToolContext,
    workflow_state: dict | None = None,
) -> dict:
    """Updates progress of a running background task. Call this after each
    major workflow step completes to report real-time progress.

    Args:
        task_id: The ticket-id of the background task.
        current_step: Name of the step just completed (e.g. 'CLASSIFY').
        progress_pct: Estimated completion percentage (10-90, not 0 or 100).
        log_entry: Brief description of what was done and key metrics.
        workflow_state: Optional structured state for workflow tracking.
            Keys: completed_steps (list of step names), pending_items (int),
            auto_processed (int), deferred_for_approval (int),
            errors (int), current_phase (str).

    Returns:
        dict with update status.
    """
    import builtins
    import datetime as _dt
    _fs = getattr(builtins, '_firestore_client', None)
    _demo_id = os.environ.get("DEMO_ID", "")
    if not _fs or not _demo_id:
        return {"error": "Firestore not available"}
    try:
        _ref = _fs.collection(_demo_id + "_task_executions").document(task_id)
        _doc = _ref.get()
        if not _doc.exists:
            return {"error": "Task not found: " + task_id}
        _current = _doc.to_dict()
        if _current.get("status") not in ("working", "pending"):
            return {"error": "Task not in active state: " + _current.get("status", "")}
        _now = _dt.datetime.now(_dt.timezone.utc).strftime("%H:%M:%S")
        _existing_log = _current.get("log_tail", "")
        _new_log = _existing_log + ("[" + _now + "] " + current_step + ": " + log_entry + chr(10)) if _existing_log else ("[" + _now + "] " + current_step + ": " + log_entry + chr(10))
        # Keep log_tail to last 1500 chars to prevent unbounded growth
        if len(_new_log) > 1500:
            _new_log = _new_log[-1500:]
        _pct = max(10, min(90, progress_pct))
        _update_data = {
            "progress_pct": _pct,
            "log_tail": _new_log,
        }
        if workflow_state and isinstance(workflow_state, dict):
            _update_data["workflow_state"] = workflow_state
        _ref.update(_update_data)
        return {"status": "updated", "task_id": task_id, "progress_pct": _pct, "step": current_step}
    except Exception as _fs_err:
        return {"error": "Firestore update failed: " + str(_fs_err)[:200]}

def register_scheduled_task(
    task_name: str,
    task_description: str,
    task_prompt: str,
    schedule_cron: str,
    tool_context: ToolContext,
) -> dict:
    """Registers a new scheduled task with automatic Cloud Scheduler job creation.

    Args:
        task_name: Short identifier (e.g. 'daily_report').
        task_description: What the task does.
        task_prompt: Detailed instruction for each execution.
        schedule_cron: Cron expression (e.g. '0 9 * * 1-5' for weekdays 9am).

    Returns:
        dict with task_id, schedule, and job_name.
    """
    import builtins, json as _json, logging as _logging
    _fs = getattr(builtins, '_firestore_client', None)
    _demo_id = os.environ.get("DEMO_ID", "")
    _project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    _region = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    if _region == "global":
        _region = "us-central1"
    _task_id = str(_task_uuid.uuid4())[:8]
    _now = _task_dt.datetime.now(_task_dt.timezone.utc).isoformat()

    # 1. Save definition to Firestore
    _def_doc = {
        "task_id": _task_id,
        "task_name": task_name,
        "task_description": task_description,
        "task_prompt": task_prompt,
        "task_type": "scheduled",
        "schedule_cron": schedule_cron,
        "created_at": _now,
    }
    if _fs and _demo_id:
        _fs.collection(_demo_id + "_task_definitions").document(_task_id).set(_def_doc)
        # Create initial execution document so Data Viewer shows correct status
        _exec_doc = {
            "task_id": _task_id,
            "definition_id": _task_id,
            "status": "scheduled",
            "progress_pct": 0,
            "log_tail": "",
            "result_summary": "",
            "started_at": "",
            "completed_at": "",
            "reported_to_user": False,
        }
        _fs.collection(_demo_id + "_task_executions").document(_task_id).set(_exec_doc)

    # 2. Create Cloud Scheduler job
    _job_name = ""
    _sched_topic = _demo_id + "-sched-tasks"
    try:
        from google.cloud import scheduler_v1
        _sched_client = scheduler_v1.CloudSchedulerClient()
        _parent = "projects/" + _project_id + "/locations/" + _region
        _job_id = _demo_id + "-sched-" + _task_id

        _payload = _json.dumps({"task_id": _task_id, "demo_id": _demo_id}).encode("utf-8")
        _topic_path = "projects/" + _project_id + "/topics/" + _sched_topic

        _job = scheduler_v1.Job(
            name=_parent + "/jobs/" + _job_id,
            schedule=schedule_cron,
            time_zone="Asia/Tokyo",
            pubsub_target=scheduler_v1.PubsubTarget(
                topic_name=_topic_path,
                data=_payload,
            ),
        )
        _created = _sched_client.create_job(parent=_parent, job=_job)
        _job_name = _created.name
        _logging.warning("Created Cloud Scheduler job: " + _job_name)
    except Exception as _e:
        _logging.error("Failed to create scheduler job: " + str(_e))
        return {
            "status": "partial",
            "task_id": _task_id,
            "error": "Firestore saved but scheduler creation failed: " + str(_e)[:200],
        }

    return {
        "status": "scheduled",
        "task_id": _task_id,
        "task_name": task_name,
        "schedule": schedule_cron,
        "job_name": _job_name,
        "message": "Scheduled task registered. Will execute at: " + schedule_cron,
    }


def update_scheduled_task(
    task_id: str,
    schedule_cron: str,
    tool_context: ToolContext,
) -> dict:
    """Updates the schedule of an existing scheduled task.

    Args:
        task_id: The task_id of the scheduled task to update.
        schedule_cron: New cron expression (e.g. '0 18 * * 1-5' for weekdays 6pm).

    Returns:
        dict with updated schedule info.
    """
    import builtins, logging as _logging
    _fs = getattr(builtins, '_firestore_client', None)
    _demo_id = os.environ.get("DEMO_ID", "")
    _project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    _region = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    if _region == "global":
        _region = "us-central1"

    if not _fs or not _demo_id:
        return {"error": "Firestore not available (client=" + str(bool(_fs)) + ", demo_id=" + repr(_demo_id) + ")"}

    # Update Firestore definition
    _def_ref = _fs.collection(_demo_id + "_task_definitions").document(task_id)
    _def_doc = _def_ref.get()
    if not _def_doc.exists:
        return {"error": "Task not found: " + task_id}
    _def_data = _def_doc.to_dict()
    if _def_data.get("task_type") != "scheduled":
        return {"error": "Task is not a scheduled task"}

    _def_ref.update({"schedule_cron": schedule_cron})

    # Update Cloud Scheduler job
    _job_id = _demo_id + "-sched-" + task_id
    try:
        from google.cloud import scheduler_v1
        from google.protobuf import field_mask_pb2
        _client = scheduler_v1.CloudSchedulerClient()
        _job_name = "projects/" + _project_id + "/locations/" + _region + "/jobs/" + _job_id
        _job = scheduler_v1.Job(name=_job_name, schedule=schedule_cron)
        _mask = field_mask_pb2.FieldMask(paths=["schedule"])
        _updated = _client.update_job(job=_job, update_mask=_mask)
        _logging.warning("Updated scheduler job: " + _updated.name + " -> " + schedule_cron)
        return {
            "status": "updated",
            "task_id": task_id,
            "new_schedule": schedule_cron,
            "job_name": _updated.name,
        }
    except Exception as _e:
        _logging.error("Failed to update scheduler job: " + str(_e))
        return {
            "status": "partial",
            "task_id": task_id,
            "message": "Firestore updated but scheduler update failed: " + str(_e)[:200],
        }


def delete_scheduled_task(
    task_id: str,
    tool_context: ToolContext,
) -> dict:
    """Deletes a scheduled task and its Cloud Scheduler job.

    Args:
        task_id: The task_id of the scheduled task to delete.

    Returns:
        dict with deletion status.
    """
    import builtins, logging as _logging
    _fs = getattr(builtins, '_firestore_client', None)
    _demo_id = os.environ.get("DEMO_ID", "")
    _project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    _region = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    if _region == "global":
        _region = "us-central1"

    if not _fs or not _demo_id:
        return {"error": "Firestore not available (client=" + str(bool(_fs)) + ", demo_id=" + repr(_demo_id) + ")"}

    # Check definition exists
    _def_ref = _fs.collection(_demo_id + "_task_definitions").document(task_id)
    _def_doc = _def_ref.get()
    if not _def_doc.exists:
        return {"error": "Task not found: " + task_id}

    # Delete Cloud Scheduler job
    _job_id = _demo_id + "-sched-" + task_id
    try:
        from google.cloud import scheduler_v1
        _client = scheduler_v1.CloudSchedulerClient()
        _job_name = "projects/" + _project_id + "/locations/" + _region + "/jobs/" + _job_id
        _client.delete_job(name=_job_name)
        _logging.warning("Deleted scheduler job: " + _job_name)
    except Exception as _e:
        _logging.warning("Scheduler job deletion failed (may not exist): " + str(_e)[:200])

    # Delete Firestore documents (definition + execution)
    _def_ref.delete()
    _fs.collection(_demo_id + "_task_executions").document(task_id).delete()

    return {
        "status": "deleted",
        "task_id": task_id,
        "message": "Scheduled task, execution record, and Cloud Scheduler job deleted.",
    }


def run_scheduled_task_now(
    task_id: str,
    tool_context: ToolContext,
) -> dict:
    """Triggers ONE immediate background execution of a registered scheduled task.

    Use this for manual test runs ("run it now") of a scheduled task. The task's
    stored task_prompt is executed by the background worker exactly like a
    Cloud Scheduler fire. Returns immediately with a ticket. Results are written
    to the operations console on completion; the chat summary is announced at the
    start of the user's NEXT message turn (there is no push notification). Use
    get_task_result for on-demand progress checks.

    Args:
        task_id: The task_id of the registered scheduled task to execute now.

    Returns:
        dict with trigger status and ticket id.
    """
    import builtins
    _fs = getattr(builtins, '_firestore_client', None)
    _demo_id = os.environ.get("DEMO_ID", "")
    if not _fs or not _demo_id:
        return {"error": "Firestore not available (client=" + str(bool(_fs)) + ", demo_id=" + repr(_demo_id) + ")"}

    _def_doc = _fs.collection(_demo_id + "_task_definitions").document(task_id).get()
    if not _def_doc.exists:
        return {"error": "Scheduled task not found: " + task_id}

    _exec_snap = _fs.collection(_demo_id + "_task_executions").document(task_id).get()
    if _exec_snap.exists and (_exec_snap.to_dict() or {}).get("status") == "working":
        return {
            "status": "already_running",
            "ticket-id": task_id,
            "message": "This task is already executing. Use get_task_result to check progress.",
        }

    # Fire-and-forget: trigger the /execute_task worker via localhost (same
    # pattern as register_background_task; see the comment there for why the
    # public SELF_URL must NOT be used for self-calls). force_run lets the
    # worker re-run a task whose single per-definition execution doc still
    # holds a terminal status from a previous run.
    import threading as _threading
    import requests as _requests
    _port = os.environ.get("PORT", "8080")
    _worker_url = "http://localhost:" + _port + "/execute_task"

    def _fire_now():
        import logging as _log
        _logger = _log.getLogger("sched_test_run")
        try:
            _resp = _requests.post(
                _worker_url + "?task_id=" + task_id + "&demo_id=" + _demo_id + "&force_run=1",
                json={"task_id": task_id, "demo_id": _demo_id, "force_run": True},
                headers={"Content-Type": "application/json"},
                timeout=(5, 0.5),
            )
            _logger.warning("run_now fire: status=%s task_id=%s", _resp.status_code, task_id)
        except _requests.exceptions.ReadTimeout:
            # Expected: the worker processes asynchronously.
            _logger.warning("run_now fire: accepted (ReadTimeout expected), task_id=%s", task_id)
        except Exception as _e:
            _logger.error("run_now fire FAILED: %s: %s", type(_e).__name__, str(_e)[:500])
    _threading.Thread(target=_fire_now, daemon=True).start()

    return {
        "status": "triggered",
        "ticket-id": task_id,
        "message": "Test execution started in the background. Results are written "
                   "to the operations console immediately upon completion, and a "
                   "chat summary will be announced at the start of the user's next "
                   "message turn (there is NO push notification — never promise "
                   "one, and never promise a completion time). Use get_task_result "
                   "for on-demand progress checks.",
    }


def write_operational_alert(
    alert_title: str,
    alert_message: str,
    status: str = "pending",
    tool_context: ToolContext = None
) -> dict:
    """Writes a high-priority operational alert or outreach task into the Firestore database.
    ALWAYS use this tool when you need to record a high-risk client alert, outreach task, 
    manual verification workflow, or log a manual review flag. Do NOT use raw MCP add_document.
    
    Args:
        alert_title: Clear, descriptive title of the alert (e.g., 'High-Priority Outreach: Satoru Gojo').
        alert_message: Detailed description of rules triggered, client profile, AUM, and required actions.
        status: Initial status of the alert, defaults to 'pending'.
        
    Returns:
        dict with write status and created alert_id.
    """
    import builtins, uuid, datetime
    _fs = getattr(builtins, '_firestore_client', None)
    _demo_id = os.environ.get("DEMO_ID", "")
    if not _fs or not _demo_id:
        return {"status": "error", "message": "Firestore operational database is not configured."}
    
    alert_id = f"alert_{uuid.uuid4().hex[:8]}"
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    doc_data = {
        "alert_id": alert_id,
        "title": alert_title,
        "message": alert_message,
        "status": status,
        "created_at": now_iso,
        "updated_at": now_iso
    }
    try:
        _fs.collection(f"{_demo_id}_alerts").document(alert_id).set(doc_data)
        return {"status": "success", "alert_id": alert_id, "message": "Alert recorded successfully in operational database."}
    except Exception as e:
        return {"status": "error", "message": f"Failed to record alert: {str(e)}"}


_FS_REST_SCALAR_KEYS = ("stringValue", "booleanValue", "timestampValue", "bytesValue", "referenceValue", "geoPointValue")

def _normalize_rest_values(v):
    # The agent is instructed to use Firestore REST typed-value format
    # (e.g. {"stringValue": "X"}) for the Firestore MCP. When that same format is
    # passed to this SDK-based tool it would be stored literally as a map field,
    # breaking downstream consumers (e.g. the Data Viewer). Unwrap to native values.
    if isinstance(v, dict):
        ks = list(v.keys())
        if len(ks) == 1:
            k = ks[0]
            inner = v[k]
            if k in _FS_REST_SCALAR_KEYS:
                return inner
            if k == "nullValue":
                return None
            if k == "integerValue":
                try:
                    return int(inner)
                except Exception:
                    return inner
            if k == "doubleValue":
                try:
                    return float(inner)
                except Exception:
                    return inner
            if k == "mapValue":
                fields = (inner or {}).get("fields", {}) if isinstance(inner, dict) else {}
                return {kk: _normalize_rest_values(vv) for kk, vv in (fields or {}).items()}
            if k == "arrayValue":
                vals = (inner or {}).get("values", []) if isinstance(inner, dict) else []
                return [_normalize_rest_values(x) for x in (vals or [])]
        return {kk: _normalize_rest_values(vv) for kk, vv in v.items()}
    if isinstance(v, list):
        return [_normalize_rest_values(x) for x in v]
    return v

def save_document_to_db(
    collection_name: str,
    document_id: str,
    document_json_string: str,
    tool_context: ToolContext = None
) -> dict:
    """Saves or updates a structured document in the Firestore operational database.
    Use this general tool to write structured records (orders, client updates, tasks).
    Accepts either a raw dictionary or a clean JSON-serialized string.
    
    Args:
        collection_name: Target collection name (e.g., 'outreach_tasks', 'client_status').
        document_id: Unique document identifier (e.g., 'client_102').
        document_json_string: Document body serialized as a JSON string OR a raw key-value dictionary.
        
    Returns:
        dict with database write status.
    """
    import builtins, json
    _fs = getattr(builtins, '_firestore_client', None)
    _demo_id = os.environ.get("DEMO_ID", "")
    if not _fs or not _demo_id:
        return {"status": "error", "message": "Firestore database is not configured."}
        
    try:
        if isinstance(document_json_string, dict):
            data = document_json_string
        else:
            data = json.loads(document_json_string)
            
        if not isinstance(data, dict):
            return {"status": "error", "message": "JSON body must represent a key-value dictionary."}

        # Defensively unwrap any Firestore REST typed-value wrappers so they are
        # stored as native scalars rather than literal {"stringValue": ...} maps.
        data = _normalize_rest_values(data)

        # Automatically inject ISO-8601 timestamp representing last update time for dynamic sorting in Data Viewer
        import datetime
        data["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        
        full_coll = f"{_demo_id}_{collection_name}" if not collection_name.startswith(_demo_id) else collection_name
        _fs.collection(full_coll).document(document_id).set(data)
        return {"status": "success", "document_id": document_id, "message": f"Document saved successfully in {full_coll}."}
    except Exception as e:
        return {"status": "error", "message": f"Failed to save document: {str(e)}"}
