"""Bounded independent public-source collection with robots and host checks."""
from __future__ import annotations
import io
import ipaddress
import json
import re
import socket
import time
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import urljoin,urlsplit
from urllib.request import Request,build_opener,HTTPRedirectHandler
from urllib.robotparser import RobotFileParser
from evidence_store import safe_url

PARSER_VERSION='nih-tracker-public-text/v1'

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None

class Page(HTMLParser):
    def __init__(self):
        super().__init__();self.parts=[];self.links=[];self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','noscript'):self.skip+=1
        if tag=='a' and dict(attrs).get('href'):self.links.append(dict(attrs)['href'])
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript'):self.skip=max(0,self.skip-1)
    def handle_data(self,text):
        if not self.skip:self.parts.append(text)

def parse_body(body,content_type):
    media=content_type.split(';')[0].lower()
    if media=='application/pdf':
        try:from pypdf import PdfReader
        except ImportError:return '',[],'MISSING_PDF_PARSER'
        try:
            reader=PdfReader(io.BytesIO(body))
            if reader.is_encrypted:return '',[],'ENCRYPTED_PDF'
            if len(reader.pages)>50:return '',[],'PDF_PAGE_CAP'
            text='\n'.join(p.extract_text() or '' for p in reader.pages)
            return text,[],'PARSED' if text.strip() else 'MISSING_OCR'
        except Exception:return '',[],'PDF_PARSE_FAILED'
    decoded=body.decode('utf-8','replace')
    if media in ('application/json','application/ld+json'):
        try:return json.dumps(json.loads(decoded),ensure_ascii=False,indent=2),[],'PARSED'
        except ValueError:return '',[],'INVALID_JSON'
    if 'html' in media:
        p=Page();p.feed(decoded);text=' '.join(' '.join(p.parts).split())
        if not text or re.fullmatch(r'loading(?:,?\s+please\s+wait)?[.!\u2026]*',text.casefold()):
            return text,[],'MISSING_BROWSER_RENDERING'
        return text,p.links,'PARSED'
    if media.startswith('text/') or media in ('application/xml','application/rdf+xml'):
        return decoded,[],'PARSED'
    return '',[],'UNSUPPORTED_MEDIA'

class PublicCollector:
    def __init__(self,config):
        self.config=config;self.robots={};self.last={};self.events=[]
        contact=config.get('user_agent','')
        if not contact or 'ADD_REAL' in contact:raise ValueError('Set a real contact in the collector user_agent.')

    def request(self,url,hosts):
        url=safe_url(url)
        for _ in range(5):
            host=urlsplit(url).hostname
            if host not in hosts:raise ValueError('Unapproved redirect/source host: '+str(host))
            addresses=socket.getaddrinfo(host,None,type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                raise ValueError('Nonpublic network destination blocked.')
            pause=max(0,self.config.get('request_gap_seconds',1.2)-(time.monotonic()-self.last.get(host,0)))
            if pause:time.sleep(pause)
            self.last[host]=time.monotonic()
            try:
                with build_opener(NoRedirect()).open(Request(url,headers={'User-Agent':self.config['user_agent']}),
                                                      timeout=self.config.get('timeout_seconds',20)) as r:
                    body=r.read(self.config.get('max_bytes',2_000_000)+1)
                    if len(body)>self.config.get('max_bytes',2_000_000):raise ValueError('Source byte cap exceeded.')
                    return body,url,dict(r.headers),r.status
            except HTTPError as exc:
                if exc.code in (301,302,303,307,308) and exc.headers.get('Location'):
                    url=safe_url(urljoin(url,exc.headers['Location']));continue
                raise
        raise ValueError('Redirect limit exceeded.')

    def allowed(self,url,hosts):
        p=urlsplit(url);origin=p.scheme+'://'+p.netloc
        if origin not in self.robots:
            try:
                body,final,headers,status=self.request(origin+'/robots.txt',hosts)
                robot=RobotFileParser();robot.parse(body.decode('utf-8','replace').splitlines())
                self.robots[origin]=robot
                # Record the policy body and observation; public robots only.
                self.events.append({'kind':'robots','url':origin+'/robots.txt','status':status,'policy':body.decode('utf-8','replace')})
            except HTTPError as e:
                self.robots[origin]=e.code in (404,410)
                self.events.append({'kind':'robots','url':origin+'/robots.txt','status':e.code})
            except Exception as e:
                self.robots[origin]=False
                self.events.append({'kind':'robots','url':origin+'/robots.txt','error_type':type(e).__name__})
        value=self.robots[origin]
        return value if isinstance(value,bool) else value.can_fetch(self.config['user_agent'],url)

    def collect(self,store,repo_uid,spec):
        # Validate admission before making a network request.
        from evidence_store import ALLOWED_KINDS
        if spec.get('source_kind') not in ALLOWED_KINDS or spec.get('cache_permission')!='public_cache_permitted':
            raise ValueError('Source needs explicit public cache permission and an independent source kind.')
        url=safe_url(spec['url']); hosts={urlsplit(url).hostname,*spec.get('allowed_redirect_hosts',[])}
        if not self.allowed(url,hosts):raise ValueError('Robots policy unavailable or disallows collection.')
        body,final,headers,status=self.request(url,hosts)
        content_type=next((v for k,v in headers.items() if k.lower()=='content-type'),'')
        text,links,parse_status=parse_body(body,content_type)
        record=store.record(repo_uid=repo_uid,url=url,body=body,text=text,final_url=final,headers=headers,
                            content_type=content_type,source_kind=spec['source_kind'],cache_permission=spec['cache_permission'],
                            parser_version=PARSER_VERSION,parse_status=parse_status,source_version=spec.get('source_version',''),
                            identity_binding=spec.get('identity_binding','operator_selected'),http_status=status)
        # Links are only from independent source pages. The caller enforces scope.
        approved=[]
        for link in links:
            try:
                if link and not link.startswith(('mailto:','javascript:','#')):approved.append(safe_url(urljoin(final,link)))
            except ValueError:continue
        return record,approved
