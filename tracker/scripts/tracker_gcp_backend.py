"""Gemini on GCP using Application Default Credentials; no API-key fallback.

The optional SDK is imported only for live calls. Offline fixtures need no cloud
packages or credentials. Public pages are fetched by the collector; this module
never uses Google Search grounding as a source-discovery/crawling service.
"""
import copy
import hashlib
import inspect
import json
import os
import re
from importlib import metadata

DEFAULT_MODEL = 'gemini-3.5-flash'
PROFILE_VERSION = 1
GENERATION_PROFILE_VERSION = 1
DEFAULT_MAX_OUTPUT_TOKENS = 16384


class GeminiResponseError(ValueError):
    """Preserve the actual nonconforming response for an auditable failed call."""
    def __init__(self, message, response, request):
        super().__init__(message)
        self.response = response
        self.request = request
        self.diagnostics = response_diagnostics(response)


def response_diagnostics(raw):
    """Small audit record: no source content, thought text or thought signatures."""
    candidates = raw.get('candidates') or []
    first = candidates[0] if candidates else {}
    usage = raw.get('usageMetadata', raw.get('usage_metadata')) or {}
    result = {'finish_reason': first.get('finishReason', first.get('finish_reason', 'NO_CANDIDATE')),
              'returned_model': raw.get('modelVersion', raw.get('model_version'))}
    for camel, snake in [('promptTokenCount', 'prompt_token_count'),
                         ('thoughtsTokenCount', 'thoughts_token_count'),
                         ('candidatesTokenCount', 'candidates_token_count'),
                         ('totalTokenCount', 'total_token_count')]:
        value = usage.get(camel, usage.get(snake))
        if isinstance(value, int) and not isinstance(value, bool):
            result[snake] = value
    return result


def migrate_config(config):
    """One-time GCP migration; caller saves the original configuration in history."""
    updated = copy.deepcopy(config)
    if updated.get('gcp_profile_version') != PROFILE_VERSION:
        old_model = updated.get('extractors', {}).get('gemini')
        updated.update(
            gcp_profile_version=PROFILE_VERSION,
            gcp_project_env='GCP_ID',
            gcp_location='global',
            extractors={'openai': None, 'anthropic': None,
                        'gemini': old_model or DEFAULT_MODEL},
            perplexity_search=False,
        )
    if updated.get('gcp_generation_profile_version') != GENERATION_PROFILE_VERSION:
        # Upgrade the shipped legacy cap once; retain custom caps and choices.
        # Thinking and JSON share the generation limit on thinking models.
        if updated.get('max_output_tokens') in (None, 3500):
            updated['max_output_tokens'] = DEFAULT_MAX_OUTPUT_TOKENS
        if updated.get('extractors', {}).get('gemini') == DEFAULT_MODEL:
            updated.setdefault('gemini_thinking_level', 'LOW')
        updated['gcp_generation_profile_version'] = GENERATION_PROFILE_VERSION
    return updated, updated != config


def project_id(config):
    name = config.get('gcp_project_env', 'GCP_ID')
    if name != 'GCP_ID':
        raise ValueError('This GCP profile reads its project ID from GCP_ID.')
    value = os.environ.get(name, '').strip()
    if not re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]', value):
        raise ValueError('Set GCP_ID to a valid GCP project ID (not its name or number). '
                         'In Colab, enable notebook access to the GCP_ID secret.')
    return value


def runtime_record(config):
    """No credentials or environment-variable values are recorded."""
    versions = {}
    for name in ('google-genai', 'google-auth', 'pydantic', 'httpx', 'httpx2'):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return {'backend': 'gcp_adc', 'project_id_source': 'GCP_ID',
            'project_id_sha256': hashlib.sha256(project_id(config).encode()).hexdigest(),
            'location': config.get('gcp_location', 'global'),
            'api_version': 'v1', 'packages': versions,
            'search_grounding_used_for_collection': False}


def create_client(config):
    project = project_id(config)
    location = config.get('gcp_location', 'global')
    if not isinstance(location, str) or not re.fullmatch(r'[a-z0-9-]+', location):
        raise ValueError('Set a valid GCP model location.')
    try:
        import google.auth
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError('Install requirements-gcp.txt before live Gemini calls.') from exc
    credentials, _ = google.auth.default(
        scopes=['https://www.googleapis.com/auth/cloud-platform'],
        quota_project_id=project,
    )
    backend = 'enterprise' if 'enterprise' in inspect.signature(genai.Client).parameters else 'vertexai'
    # Explicit ADC and backend; unrelated Developer API keys cannot change routing.
    saved_keys = {k: os.environ.pop(k) for k in ('GEMINI_API_KEY', 'GOOGLE_API_KEY') if k in os.environ}
    try:
        return genai.Client(
            **{backend: True}, project=project, location=location,
            credentials=credentials,
            http_options=types.HttpOptions(api_version='v1', timeout=120_000,
                                           retry_options=types.HttpRetryOptions(attempts=1)),
        )
    finally:
        os.environ.update(saved_keys)


def extract(model, prompt, max_tokens, output_schema, config):
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9._-]+', model):
        raise ValueError('Set an explicit Gemini model ID.')
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
        raise ValueError('max_output_tokens must be a positive integer.')
    request_config = {'max_output_tokens': max_tokens,
                      'response_mime_type': 'application/json',
                      'response_json_schema': output_schema}
    level = config.get('gemini_thinking_level')
    if level is not None and level != '':
        if not isinstance(level, str) or level.upper() not in ('MINIMAL', 'LOW', 'MEDIUM', 'HIGH'):
            raise ValueError('gemini_thinking_level must be MINIMAL, LOW, MEDIUM, HIGH, or null.')
        if not re.match(r'gemini-[3-9](?:[.-]|$)', model):
            raise ValueError('gemini_thinking_level requires a supported Gemini 3+ model; '
                             'set it to null for older or custom models.')
        request_config['thinking_config'] = {'thinking_level': level.upper()}
    request = {'backend': 'gcp_adc', 'api_version': 'v1',
               'location': config.get('gcp_location', 'global'),
               'model': model, 'contents': prompt, 'config': request_config}
    client = create_client(config)
    try:
        response = client.models.generate_content(model=model, contents=prompt, config=request_config)
        raw = response.model_dump(mode='json', by_alias=True, exclude_none=True)
    finally:
        client.close()
    candidates = raw.get('candidates', [])
    if not candidates or candidates[0].get('finishReason', candidates[0].get('finish_reason')) != 'STOP':
        reason = response_diagnostics(raw)['finish_reason']
        hint = ('Increase max_output_tokens or reduce thinking level / source scope.'
                if reason == 'MAX_TOKENS' else 'Inspect the saved provider response.')
        raise GeminiResponseError('Gemini finish reason: '+str(reason)+'. '+hint, raw, request)
    text = ''.join(p.get('text', '') for p in candidates[0].get('content', {}).get('parts', [])
                   if not p.get('thought'))
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise GeminiResponseError('Gemini returned invalid JSON; inspect saved provider_io.', raw, request) from exc
    return parsed, raw, request


def smoke_test(config):
    """One live schema-constrained call; does not crawl or create evidence claims."""
    model = config.get('extractors', {}).get('gemini')
    output_schema = {'type': 'object', 'properties': {'status': {'type': 'string', 'enum': ['GCP_LLM_OK']}},
                     'required': ['status'], 'additionalProperties': False}
    value, raw, _ = extract(model, 'Return the status GCP_LLM_OK.',
                            config.get('max_output_tokens', DEFAULT_MAX_OUTPUT_TOKENS), output_schema, config)
    if value != {'status': 'GCP_LLM_OK'}:
        raise ValueError('Access call completed but the expected schema test did not pass.')
    return {'status': 'PASS', 'requested_model': model,
            'returned_model': raw.get('modelVersion', raw.get('model_version')), 'backend': 'gcp_adc'}
