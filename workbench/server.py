"""Loopback-only fixture server. Does not forward to Docker or a model API."""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from controller.agent.tools import ToolRegistry

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT.parent / 'tests/fixtures/workbench/demo.json'


def route(method, path, body=None, case="normal"):
    """Answer an API request from the illustrative fixture, optionally simulating an error or failed
    cleanup case.
    """
    parsed = urlsplit(path)
    fixture = json.loads(FIXTURE.read_text())
    if case == 'api-error':
        return 503, {'ok': False, 'error': 'UNAVAILABLE', 'message': 'Illustrative API unavailable.'}
    if case == 'cleanup-failed':
        for run in [fixture['snapshot']['history'][0], fixture['detail']['run']]:
            run.update(state='NEEDS_ATTENTION', cleanup_verified=False, recovery_outcome='failed')
        fixture['events'][-1].update(state='NEEDS_ATTENTION',type='warning',
                                    data={'message':'Illustrative restoration could not be verified.'})
    if case == 'stale':
        for observation in fixture['detail']['observations']:
            observation.update(fresh=False,complete=False)
        fixture['detail']['diagnosis']=None
    if case == 'provider-error':
        fixture['detail']['diagnosis']=None
        fixture['snapshot']['readiness']['blockers'].append('Illustrative diagnosis provider unavailable.')
    if method == 'POST':
        if parsed.path == '/api/workbench/plans/validate':
            result = ToolRegistry().dispatch('propose_plan', body)
            if result['ok']:
                result['result'] = {'plan': result['result'], 'execution': 'preview'}
            return (200 if result['ok'] else 400), result
        return 409, {'ok': False, 'error': 'FIXTURE_ONLY',
                     'message': 'Live mutations are unavailable in the fixture workbench.'}
    if parsed.path == '/api/workbench':
        return 200, {'ok': True, 'result': fixture['snapshot']}
    base = '/api/workbench/runs/' + fixture['detail']['run']['run_id']
    if parsed.path == base:
        return 200, {'ok': True, 'result': fixture['detail']}
    if parsed.path == base + '/events':
        try:
            after = int(parse_qs(parsed.query).get('after_seq', ['0'])[0])
            if after < 0: raise ValueError()
        except ValueError:
            return 400, {'ok': False, 'error': 'INVALID_CURSOR', 'message': 'Invalid event sequence.'}
        return 200, {'ok': True, 'result': {'events': [e for e in fixture['events'] if e['seq'] > after]}}
    return 404, {'ok': False, 'error': 'NOT_FOUND', 'message': 'Record not found.'}


class Handler(BaseHTTPRequestHandler):
    """HTTP handler for the fixture-backed workbench preview."""
    def respond(self, status, data, mime='application/json'):
        """Send a response with strict security headers."""
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        """Serve allow-listed static assets or fixture API responses."""
        path = urlsplit(self.path).path
        if path.startswith('/api/'):
            status, result = route('GET', self.path, case=self.server.fixture_case)
            return self.respond(status, json.dumps(result).encode())
        # An explicit asset allowlist prevents repository/artifact traversal.
        assets = {'/': ('index.html', 'text/html'), '/index.html': ('index.html','text/html'),
                  '/style.css': ('style.css','text/css'), '/app.mjs': ('app.mjs','text/javascript'),
                  '/state.mjs': ('state.mjs','text/javascript'), '/api.mjs': ('api.mjs','text/javascript')}
        if path not in assets: return self.respond(404, b'Not found', 'text/plain')
        name, mime = assets[path]
        self.respond(200, (ROOT/name).read_bytes(), mime)

    def do_POST(self):
        """Handle a fixture API POST after checking the Origin and body size."""
        origin = self.headers.get('Origin')
        expected = f'http://127.0.0.1:{self.server.server_port}'
        if origin and origin not in (expected, expected.replace('127.0.0.1','localhost')):
            return self.respond(403, b'{"ok":false,"message":"Origin rejected."}')
        try:
            size = int(self.headers.get('Content-Length','0'))
            if not 0 < size <= 8192: raise ValueError()
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict): raise ValueError()
            status, result = route('POST', self.path, body, case=self.server.fixture_case)
            self.respond(status, json.dumps(result).encode())
        except (ValueError, TypeError):
            self.respond(400, b'{"ok":false,"message":"Invalid request."}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8095)
    parser.add_argument('--case', choices=['normal','cleanup-failed','stale','provider-error','api-error'], default='normal')
    args = parser.parse_args()
    print(f'Fixture workbench: http://127.0.0.1:{args.port} (no live execution)', flush=True)
    server=ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.fixture_case=args.case
    server.serve_forever()
