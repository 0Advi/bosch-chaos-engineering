"""Real HTTP integration of UI transport and durable controller, copied fake flags."""
import json
import shutil
import threading
import time
import pytest
from controller.api import ControllerAPI, serve
from controller.executor.flag_store import FLAG_FILE
from controller.schemas import Plan
from tests.fake_env import FakeEnvironment, fast_limits, make_plan
from tests.test_api import Client
from workbench.bridge import WorkbenchBridge


@pytest.fixture
def connected(tmp_path):
    flag = tmp_path / 'demo.flagd.json'
    shutil.copy(FLAG_FILE, flag)
    api = ControllerAPI(db_path=tmp_path/'runs.sqlite3', root=tmp_path,
                        baselines_dir=tmp_path/'artifacts/baselines', env_factory=lambda: FakeEnvironment(flag),
                        limits=fast_limits(), gate_check=lambda plan, cap: [])
    api.workbench_bridge = WorkbenchBridge(api, preflight={'isolation_approved': False, 'errors': []})
    server = serve(api, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Client(server.server_address[1]), api, flag
    server.shutdown()
    server.server_close()


def test_connected_assets_readiness_and_no_repository_exposure(connected):
    client, api, _ = connected
    status, html = client.call('GET', '/')
    assert status == 200 and 'Payment workbench' in html
    status, body = client.call('GET', '/api/workbench')
    assert status == 200 and body['result']['mode'] == 'controller'
    assert body['result']['readiness']['execution_enabled'] is False
    assert len(body['result']['capabilities']) == 8
    assert client.call('GET', '/artifacts/results/controller.sqlite3')[0] == 404
    assert client.call('POST', '/api/workbench/baselines', {}, {'Origin': 'https://evil.example'})[0] == 403


def test_preview_plan_uses_real_contract_and_policy(connected):
    client, _, _ = connected
    plan = make_plan(duration=1).model_dump(mode='json')
    status, body = client.call('POST', '/api/workbench/plans/validate',
        {k: plan[k] for k in ('capability_id', 'parameters', 'duration_seconds', 'expected_impact')})
    assert status == 200
    parsed = Plan.model_validate(body['result']['plan'])
    assert parsed.requested_by == 'user' and parsed.baseline_ref is None
    assert client.call('POST', '/api/workbench/plans/validate', {'capability_id': 'invented'})[0] >= 400


def test_durable_idempotency_events_and_free_diagnosis(connected):
    client, api, flag = connected
    before = flag.read_bytes()
    body = {'plan': make_plan(duration=1).model_dump(mode='json')}
    headers = {'Idempotency-Key': 'browser-action-1'}
    status, result = client.call('POST', '/api/workbench/runs', body, headers)
    assert status == 200
    run_id = result['result']['run_id']
    assert client.call('POST', '/api/workbench/runs', body, headers)[1]['result']['run_id'] == run_id
    changed = json.loads(json.dumps(body))
    changed['plan']['duration_seconds'] = 2
    assert client.call('POST', '/api/workbench/runs', changed, headers)[0] == 409
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        detail = client.call('GET', f'/api/workbench/runs/{run_id}')[1]['result']
        if detail['run']['terminal']: break
        time.sleep(.05)
    assert detail['run']['terminal'] and flag.read_bytes() == before
    events = client.call('GET', f'/api/workbench/runs/{run_id}/events?after_seq=0')[1]['result']['events']
    assert [e['seq'] for e in events] == list(range(1,len(events)+1))
    assert client.call('POST', f'/api/workbench/runs/{run_id}/diagnosis', {})[0] == 200
    assert client.call('GET', f'/api/workbench/runs/{run_id}')[1]['result']['diagnosis']
    assert client.call('POST', '/api/workbench/patches/patch-1/deploy', {'passed': True})[0] == 409
    assert client.call('GET', f'/api/workbench/runs/{run_id}/events?after_seq=-1')[0] == 400


def test_baseline_run_mutex_and_boolean_execution(connected):
    client, api, _ = connected
    body = {'plan': make_plan(duration=1).model_dump(mode='json'), 'live': 'false'}
    assert client.call('POST', '/api/v1/runs', body)[0] == 400
    api.baseline_job = {'state': 'running'}
    body['live'] = True
    assert client.call('POST', '/api/v1/runs', body)[0] == 409
    assert not api.store().recent_runs(10)
