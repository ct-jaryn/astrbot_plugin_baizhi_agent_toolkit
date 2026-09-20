"""Real MCP SDK, synthetic credentials and in-memory HTTP only; no sockets."""
from contextlib import asynccontextmanager
import importlib
import inspect
import json
import logging
from pathlib import Path
import sys
from types import SimpleNamespace

import anyio
import jsonschema
import pytest
from mcp import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import baizhi_client as client
from baizhi_arguments import build_arguments

HISTORY = json.loads((Path(__file__).parent / 'fixtures/tools-history-2026-09-16.json').read_text())
DUMMY_KEY = 'SYNTHETIC_AUDIT_KEY_NOT_REAL'


def run(factory):
    return anyio.run(factory)


@pytest.fixture(autouse=True)
def deny_real_network(monkeypatch):
    async def denied(*args, **kwargs):
        raise AssertionError('Real network forbidden in offline tests')
    for name in ('httpx', 'httpx2'):
        try:
            http = importlib.import_module(name)
        except ImportError:
            continue
        monkeypatch.setattr(http.AsyncHTTPTransport, 'handle_async_request', denied)


class MockMCP:
    def __init__(self, http):
        self.http = http
        self.requests = []
        self.headers = []
        self.status = None
        self.location = None
        self.rpc_error = False
        self.result = {'content': [{'type': 'text', 'text': 'mock page'}], 'isError': False}
        self.tools = [{'name': t['name'], 'inputSchema': t['input_schema']} for t in HISTORY['tools']]
        self.paginate = False
        self.repeat_cursor = False
        self.sse = False

    async def handle(self, request):
        assert str(request.url) == client.DEFAULT_ENDPOINT
        self.headers.append(request.headers.get('Authorization'))
        if request.method != 'POST':
            return self.http.Response(405)
        message = json.loads(request.content)
        self.requests.append(message)
        if self.status:
            return self.http.Response(self.status,
                headers={'Location': self.location} if self.location else {},
                text=DUMMY_KEY, extensions={'reason_phrase': DUMMY_KEY.encode()})
        if request.headers.get('Authorization') != f'Bearer {DUMMY_KEY}':
            return self.http.Response(401, text=DUMMY_KEY)
        if 'id' not in message:
            return self.http.Response(202)
        method = message['method']
        if method == 'initialize':
            result = {'protocolVersion': HISTORY['protocol'], 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'offline', 'version': '0'}}
        elif method == 'tools/list':
            cursor = message.get('params', {}).get('cursor')
            if self.repeat_cursor:
                result = {'tools': [], 'nextCursor': 'same'}
            elif self.paginate and not cursor:
                result = {'tools': self.tools[:1], 'nextCursor': 'next'}
            else:
                result = {'tools': self.tools[1:] if self.paginate else self.tools}
        elif method == 'tools/call':
            name = message['params']['name']
            schema = next(t['input_schema'] for t in HISTORY['tools'] if t['name'] == name)
            args = message['params']['arguments']
            jsonschema.validate(args, schema)
            assert not set(args) - set(schema['properties']), 'undeclared wire arguments'
            result = self.result
        else:
            raise AssertionError(method)
        response = {'jsonrpc': '2.0', 'id': message['id'], 'result': result}
        if self.rpc_error and method == 'tools/list':
            response = {'jsonrpc': '2.0', 'id': message['id'],
                        'error': {'code': -32603, 'message': DUMMY_KEY}}
        if self.sse:
            return self.http.Response(200, headers={'Content-Type':'text/event-stream'},
                content=('event: message\ndata: '+json.dumps(response)+'\n\n').encode())
        return self.http.Response(200, json=response)


@pytest.fixture
def service(monkeypatch):
    _, transport = client._load_sdk()
    annotation = inspect.signature(transport).parameters['http_client'].annotation
    http = importlib.import_module('httpx2' if 'httpx2' in str(annotation) else 'httpx')
    server = MockMCP(http)
    original = http.AsyncClient
    def factory(**kwargs):
        assert kwargs['trust_env'] is False
        assert kwargs['follow_redirects'] is False
        return original(**kwargs, transport=http.MockTransport(server.handle))
    monkeypatch.setattr(http, 'AsyncClient', factory)
    return server


@pytest.mark.parametrize('sse', [False, True])
def test_probe_and_authentication_do_not_execute_tools(service, sse):
    service.sse = sse
    ok, text = run(lambda: client.probe(api_key=DUMMY_KEY))
    assert ok and '3 个支持的工具' in text
    assert set(service.headers) == {f'Bearer {DUMMY_KEY}'}
    assert not any(r['method'] == 'tools/call' for r in service.requests)


@pytest.mark.parametrize('name,args,expected', [
    ('websearch_search', {'query': 'q', 'count': 3, 'domains_json':'["example.com"]',
      'exclude_domains_json':'["skip.example.com"]'},
      {'query':'q','count':3,'need_summary':False,'time_range':'month',
       'filter':{'domains':['example.com'],'exclude_domains':['skip.example.com']}}),
    ('web_scrape', {'url':'https://example.com','accept_language':''},
      {'url':'https://example.com','download':False,'return_format':'markdown'}),
    ('web_extract', {'url':'https://example.com','fields_json':'{"title":"string"}'},
      {'url':'https://example.com','download':False,'fields':{'title':'string'}}),
])
def test_real_sdk_wire_mappings(service, name, args, expected):
    assert run(lambda: client.call_tool(name,args,api_key=DUMMY_KEY)) == 'mock page'
    calls = [r for r in service.requests if r['method'] == 'tools/call']
    assert len(calls) == 1
    assert calls[0]['params']['arguments'] == expected


@pytest.mark.parametrize('url', [
    'http://127.0.0.1', 'http://[::1]', 'http://127.1', 'http://0x7f.0.0.1',
    'http://localhost.', 'http://private.internal', 'http://192.168.1.2',
    'https://user:password@example.com', 'https://@example.com', 'file:///etc/passwd',
    'https://example.com:8443', 'https://example.com/#', 'https://example.com/a\nb',
])
def test_nonpublic_or_unsafe_urls_never_connect(service, url):
    result = run(lambda: client.call_tool('web_scrape', {'url':url}, api_key=DUMMY_KEY))
    assert 'input validation failed' in result
    assert service.requests == []


@pytest.mark.parametrize('name,args', [
    ('websearch_search',{'query':''}), ('websearch_search',{'query':'q','count':True}),
    ('websearch_search',{'query':'q','count':51}), ('websearch_search',{'query':'q','count':2.5}),
    ('websearch_search',{'query':'q','domains_json':'{}'}),
    ('websearch_search',{'query':'q','domains_json':'["https://example.com"]'}),
    ('web_extract',{'url':'https://example.com','fields_json':'{"a":"object"}'}),
    ('web_extract',{'url':'https://example.com','fields_json':'{'}),
    ('web_extract',{'url':'https://example.com'}),
    ('web_scrape',{'url':'https://example.com','download':'true'}),
    ('web_scrape',{'url':'https://example.com','hidden':'extra'}), ('execute_code',{}),
])
def test_invalid_arguments_never_connect(service, name, args):
    assert 'input validation failed' in run(lambda: client.call_tool(name,args,api_key=DUMMY_KEY))
    assert service.requests == []


@pytest.mark.parametrize('key', ['', 'Bearer '+DUMMY_KEY, DUMMY_KEY+'\r\nInjected: true', ' '])
def test_bad_keys_do_not_connect(service, key):
    text=run(lambda: client.call_tool('websearch_search',{'query':'q'},api_key=key))
    assert DUMMY_KEY not in text and service.requests == []


def test_wrong_key_fails_authentication(service):
    ok,text=run(lambda: client.probe(api_key='ANOTHER_SYNTHETIC_KEY'))
    assert not ok and DUMMY_KEY not in text
    assert not any(r['method']=='tools/call' for r in service.requests)


@pytest.mark.parametrize('endpoint',[client.DEFAULT_ENDPOINT+'/', 'http://127.0.0.1/mcp', 'https://evil.example/mcp'])
def test_endpoint_rejected_before_credentials_are_sent(service,endpoint):
    ok,_=run(lambda:client.probe(api_key=DUMMY_KEY,endpoint=endpoint))
    assert not ok and service.requests==[]


@pytest.mark.parametrize('status',[301,307,401,403,429,500])
def test_http_errors_and_redirects_never_echo_key(service,status,caplog):
    caplog.set_level(logging.DEBUG)
    service.status=status;service.location=client.DEFAULT_ENDPOINT+'/'
    ok,text=run(lambda:client.probe(api_key=DUMMY_KEY))
    assert not ok and DUMMY_KEY not in text+caplog.text
    assert len(service.requests)==1


def test_protocol_errors_never_echo_key(service,caplog):
    caplog.set_level(logging.DEBUG)
    service.rpc_error=True
    ok,text=run(lambda:client.probe(api_key=DUMMY_KEY))
    assert not ok and DUMMY_KEY not in text+caplog.text


def test_success_redacts_text_and_structured_keys_values(service):
    service.result={'content':[{'type':'text','text':DUMMY_KEY}],
      'structuredContent':{DUMMY_KEY:[DUMMY_KEY]},'isError':False}
    result=run(lambda:client.call_tool('websearch_search',{'query':'q'},api_key=DUMMY_KEY))
    assert DUMMY_KEY not in result and '[REDACTED]' in result


def test_sdk_structured_only_and_error_semantics(service):
    service.result={'content':[],'structuredContent':{'title':'expected title'}}
    result=run(lambda:client.call_tool('websearch_search',{'query':'q'},api_key=DUMMY_KEY))
    assert json.loads(result)=={'title':'expected title'}
    service.requests.clear()
    service.result={'content':[{'type':'text','text':DUMMY_KEY}],'isError':True}
    result=run(lambda:client.call_tool('websearch_search',{'query':'q'},api_key=DUMMY_KEY))
    assert 'tool reported an error' in result and DUMMY_KEY not in result
    assert sum(r['method']=='tools/call' for r in service.requests)==1


def test_both_attribute_spellings_are_supported():
    for structured,error in [('structuredContent','isError'),('structured_content','is_error')]:
        result=SimpleNamespace(content=[],**{structured:{'a':'b'},error:False})
        assert json.loads(client._result_to_text(result))=={'a':'b'}
        setattr(result,error,True)
        assert 'tool reported an error' in client._result_to_text(result)


def test_dependency_and_nested_errors_are_static(monkeypatch,caplog):
    for exc in [client.BaizhiDependencyError(DUMMY_KEY), RuntimeError(DUMMY_KEY),
                ExceptionGroup(DUMMY_KEY,[RuntimeError(DUMMY_KEY)])]:
        def fail():
            raise exc
        monkeypatch.setattr(client,'_load_sdk',fail)
        ok,text=run(lambda:client.probe(api_key=DUMMY_KEY))
        call=run(lambda:client.call_tool('websearch_search',{'query':'q'},api_key=DUMMY_KEY))
        assert not ok and DUMMY_KEY not in text+call+caplog.text
        if isinstance(exc,client.BaizhiDependencyError):
            assert 'dependency' in text


def test_paginated_discovery_and_repeat_limit(service):
    service.paginate=True
    assert run(lambda:client.probe(api_key=DUMMY_KEY))[0]
    assert sum(r['method']=='tools/list' for r in service.requests)==2
    service.requests.clear();service.repeat_cursor=True
    assert not run(lambda:client.probe(api_key=DUMMY_KEY))[0]
    assert sum(r['method']=='tools/list' for r in service.requests)==2


def test_result_limit(service,monkeypatch):
    monkeypatch.setattr(client,'MAX_RESULT_CHARS',10)
    service.result={'content':[{'type':'text','text':'x'*11}]}
    assert 'output limit' in run(lambda:client.call_tool('websearch_search',{'query':'q'},api_key=DUMMY_KEY))


def test_redaction_cannot_expand_output_beyond_limit(monkeypatch):
    monkeypatch.setattr(client, 'MAX_RESULT_CHARS', 10)
    result = types.CallToolResult(content=[types.TextContent(type='text', text='KK')])
    assert 'output limit' in client._result_to_text(result, 'K')


def test_total_timeout_is_bounded_and_does_not_retry(monkeypatch):
    opened=[]
    @asynccontextmanager
    async def stall(*args,**kwargs):
        opened.append(True)
        await anyio.sleep(2)
        yield
    monkeypatch.setattr(client,'_open_streams',stall)
    text=run(lambda:client.call_tool('websearch_search',{'query':'q'},api_key=DUMMY_KEY,timeout_seconds=1))
    assert 'timed out' in text and len(opened)==1


def test_diagnostic_filter_does_not_suppress_other_clients(caplog):
    caplog.set_level(logging.INFO)
    logging.getLogger('httpx').info('another-client-visible')
    assert 'another-client-visible' in caplog.text


def test_missing_http_library_returns_safe_dependency_message(monkeypatch):
    original = importlib.import_module
    def unavailable(name, *args, **kwargs):
        if name in {'httpx', 'httpx2'}:
            raise ImportError(DUMMY_KEY)
        return original(name, *args, **kwargs)
    monkeypatch.setattr(client.importlib, 'import_module', unavailable)
    ok,text=run(lambda:client.probe(api_key=DUMMY_KEY))
    assert not ok and 'dependency' in text and DUMMY_KEY not in text


def test_caller_cancellation_propagates_and_restores_diagnostic_context(monkeypatch):
    started = []
    cancelled = []
    @asynccontextmanager
    async def stall(*args, **kwargs):
        started.append(True)
        try:
            await anyio.sleep_forever()
        finally:
            cancelled.append(True)
        yield
    monkeypatch.setattr(client, '_open_streams', stall)
    async def cancel():
        with anyio.move_on_after(0.01) as scope:
            await client.call_tool('websearch_search', {'query':'q'}, api_key=DUMMY_KEY)
        assert scope.cancel_called
        assert not client._private_exchange.get()
    run(cancel)
    assert started == [True] and cancelled == [True]


def test_real_transport_is_blocked():
    import httpx
    async def attempt():
        async with httpx.AsyncClient(trust_env=False) as http:
            await http.get('https://must-not-connect.invalid')
    with pytest.raises(AssertionError,match='Real network forbidden'):
        run(attempt)
