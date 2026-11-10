"""Optional Claude on Google Cloud, ADC only; no Anthropic API key or web tools.

The project must separately enable the selected partner model and structured
outputs. The caller supplies a frozen evidence packet, not another model's answer.
"""
import json
import re

from tracker_gcp_backend import project_id

DEFAULT_MODEL = 'claude-sonnet-5-5'


def diagnostics(raw):
    return {'finish_reason':raw.get('stop_reason'), 'returned_model':raw.get('model'),
            'usage':raw.get('usage',{}), 'http_status':raw.get('http_status')}


class ClaudeResponseError(RuntimeError):
    def __init__(self, message, response, request):
        super().__init__(message)
        self.response=response; self.request=request; self.diagnostics=diagnostics(response)


def extract(model, prompt, max_output_tokens, schema, config):
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    project = project_id(config)
    region = config.get('claude_location','global')
    if not re.fullmatch(r'[a-z][a-z0-9-]{1,63}',region):
        raise ValueError('Invalid Claude region.')
    if not re.fullmatch(r'claude-[a-z0-9@._-]+',model):
        raise ValueError('Use the exact Claude model ID from Google Cloud Model Garden.')
    endpoint = 'aiplatform.googleapis.com' if region == 'global' else region+'-aiplatform.googleapis.com'
    url = f'https://{endpoint}/v1/projects/{project}/locations/{region}/publishers/anthropic/models/{model}:rawPredict'
    payload = {'anthropic_version':'vertex-2023-10-16', 'messages':[{'role':'user','content':prompt}],
               'max_tokens':max_output_tokens, 'stream':False,
               'output_config':{'format':{'type':'json_schema','schema':schema}}}
    # This setting is documented for Sonnet 5.5; never send it blindly to older models.
    if model == 'claude-sonnet-5-5':
        payload['thinking']={'type':'between_tools'}
    request = {'provider':'anthropic','backend':'gcp_adc','model':model,'location':region,
               'max_output_tokens':max_output_tokens,'structured_output_schema':schema,
               'thinking':payload.get('thinking'), 'tools':[]}
    credentials,_ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'],quota_project_id=project)
    session = AuthorizedSession(credentials)
    try:
        response = session.post(url,json=payload,timeout=120,allow_redirects=False)
        try:
            raw = response.json()
        except (ValueError, TypeError):
            raw = {'http_status':response.status_code,'body':response.text[:4000]}
        if not isinstance(raw,dict):
            raw = {'http_status':response.status_code,'unexpected_response':raw}
        if response.status_code != 200:
            raw['http_status']=response.status_code
            raise ClaudeResponseError(f'Claude on GCP returned HTTP {response.status_code}; check model enablement, region and partner-feature policy in provider_io.json.',raw,request)
    finally:
        session.close()
    if raw.get('stop_reason') != 'end_turn':
        raise ClaudeResponseError('Claude response did not complete; no partial recommendations will be imported.',raw,request)
    text = ''.join(part.get('text','') for part in raw.get('content',[]) if part.get('type') == 'text')
    try:
        candidate = json.loads(text)
    except ValueError as exc:
        raise ClaudeResponseError('Claude returned invalid JSON; response retained without repair.',raw,request) from exc
    return candidate,raw,request


def smoke_test(config):
    model = config.get('claude_model',DEFAULT_MODEL)
    schema = {'type':'object','properties':{'ready':{'type':'boolean'}},'required':['ready'],'additionalProperties':False}
    parsed,raw,_ = extract(model,'Return exactly {"ready":true}.',256,schema,config)
    if parsed != {'ready':True}:
        raise ValueError('Claude smoke response did not match the requested object.')
    return {'status':'PASS','backend':'gcp_adc','requested_model':model,'returned_model':raw.get('model'),
            'region':config.get('claude_location','global')}
