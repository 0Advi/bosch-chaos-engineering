import json
from pathlib import Path

import pytest

from controller.schemas import Run, Event, Observation, Diagnosis, Patch
from workbench.server import route

ROOT=Path(__file__).resolve().parents[1]
FIXTURE=json.loads((ROOT/'tests/fixtures/workbench/demo.json').read_text())


def test_fixture_records_validate_shared_contracts():
    detail=FIXTURE['detail']
    Run.model_validate(detail['run'])
    for event in FIXTURE['events']: Event.model_validate(event)
    for observation in detail['observations']: Observation.model_validate(observation)
    Diagnosis.model_validate(detail['diagnosis'])
    Patch.model_validate(detail['patch_review']['patch'])
    assert FIXTURE['snapshot']['mode']=='fixture'
    assert FIXTURE['snapshot']['readiness']['execution_enabled'] is False


@pytest.mark.parametrize('path',['/runs','/runs/run-0001/abort','/patches/patch-candidate-01/approve','/patches/patch-candidate-01/deploy'])
def test_fixture_mutations_always_refused(path):
    status,body=route('POST','/api/workbench'+path,{})
    assert status==409 and body['error']=='FIXTURE_ONLY'


def test_resume_events_exact_cursor():
    status,body=route('GET','/api/workbench/runs/run-0001/events?after_seq=4')
    assert status==200 and [e['seq'] for e in body['result']['events']]==[5,6,7]
    assert route('GET','/api/workbench/runs/run-0001/events?after_seq=-1')[0]==400


def test_disabled_capability_and_untrusted_plan_input_refused():
    args={'capability_id':'payment-process-outage','duration_seconds':15,'expected_impact':'Test'}
    assert route('POST','/api/workbench/plans/validate',args)[0]==400
    args.update(capability_id='payment-unreachable',command='docker stop payment')
    assert route('POST','/api/workbench/plans/validate',args)[0]==400


def test_fixture_plan_is_typed_preview():
    status,body=route('POST','/api/workbench/plans/validate',{'capability_id':'payment-unreachable',
        'duration_seconds':15,'expected_impact':'Checkout fails but cart stays healthy'})
    assert status==200 and body['result']['execution']=='preview'


def test_cleanup_stale_and_provider_failure_fixtures():
    detail='/api/workbench/runs/run-0001'
    _,result=route('GET',detail,case='cleanup-failed')
    assert result['result']['run']['state']=='NEEDS_ATTENTION'
    assert result['result']['run']['cleanup_verified'] is False
    _,result=route('GET',detail,case='stale')
    assert all(not o['fresh'] for o in result['result']['observations'])
    _,result=route('GET',detail,case='provider-error')
    assert result['result']['diagnosis'] is None
    assert route('GET','/api/workbench',case='api-error')[0]==503
