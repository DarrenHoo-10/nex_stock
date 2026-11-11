"""Paid screening owner/config/usage isolation, using the actual isolated engine."""
from __future__ import annotations
import hashlib
import json
import os
import time
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from tests.fixtures.finance_screening_worker import screening_worker
from src.services import finance_identity as identity
from src.services.finance_user_screening import FinanceUserScreening

if identity.user_system_available():
    from tests.multi_user_helpers import (
        USER_A, USER_B, build_multi_user_app, teardown_multi_user_env,
        set_system_provider_env, give_user_model, stub_dns,
    )
else:
    pytestmark = pytest.mark.skip(reason='paid screening ownership needs the Stock P2 user system')


def wait(check, timeout=90):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.04)
    raise AssertionError('isolated screening fixture timed out')


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    from src.services.task_queue import AnalysisTaskQueue, get_task_queue
    monkeypatch.setattr(AnalysisTaskQueue, '_instance', None)
    monkeypatch.setenv('MAX_WORKERS', '3')
    monkeypatch.setenv('LITELLM_LOCAL_MODEL_COST_MAP', 'True')
    app = build_multi_user_app(tmp_path, monkeypatch)
    set_system_provider_env(monkeypatch)
    stub_dns(monkeypatch)
    monkeypatch.setenv('SCREENING_ENABLED', 'true')
    monkeypatch.setenv('AUTH_MODE', 'user_center')
    from src.config import Config
    Config.reset_instance()
    from src.services.user_settings_service import UserSettingsService
    for user in (USER_A, USER_B):
        give_user_model(user, key='sk-' + user, model='user-model')
        UserSettingsService().set_settings(user, {'TAVILY_API_KEYS': user + '-search', 'TUSHARE_TOKEN': user + '-data'})
    values = {'FINANCE_SERVICE_TOKEN': 'synthetic-service-' + 'x' * 40, 'FINANCE_INSTANCE_ID': 'test-stock', 'FINANCE_CORE_ID': 'test-core'}
    monkeypatch.setattr(identity, 'setting', lambda key: values.get(key, ''))
    monkeypatch.setattr(identity, 'is_user_center_mode', lambda: True)
    from src import user_center_auth
    monkeypatch.setattr(user_center_auth, 'load_settings', lambda: SimpleNamespace(base_url='https://identity.test', required_role='user'))
    active = {USER_A: True, USER_B: True}
    monkeypatch.setattr(user_center_auth, 'resolve_session', lambda cookies: SimpleNamespace(user_id=cookies[0], role='user') if active.get(cookies[0]) else None)
    store = identity.FinanceIdentityStore(tmp_path / 'finance.db')
    owners = {}
    for user in active:
        owner = {'issuer': 'https://identity.test', 'subject': user, 'instanceId': 'test-stock', 'coreId': 'test-core'}
        owners[user] = store.create(owner, user)
    service = FinanceUserScreening(store=store, worker_target=screening_worker)
    queue = get_task_queue()
    try:
        yield SimpleNamespace(app=app, store=store, service=service, owners=owners, active=active, tmp=tmp_path)
    finally:
        for task_cache in (tmp_path / 'screening-users').glob('*/*'):
            task_cache.joinpath('release').touch()
        queue.shutdown()
        teardown_multi_user_env()


def test_two_users_run_real_strategy_with_own_models_and_private_history(fixture):
    f = fixture
    before = dict(os.environ)
    tasks = {}
    for user in (USER_A, USER_B):
        tasks[user] = f.service.start({'strategy': 'dual_low', 'max_results': 2}, owner=f.owners[user], session=SimpleNamespace(user_id=user), operation_id='screen-one')
        replay = f.service.start({'strategy': 'dual_low', 'max_results': 2}, owner=f.owners[user], session=SimpleNamespace(user_id=user), operation_id='screen-one')
        assert replay == tasks[user]
    results = {}
    for user in tasks:

        def completed():
            row = f.service.get({'task_id': tasks[user]['task_id']}, owner=f.owners[user])
            return row if row['status'] not in {'pending', 'processing'} else None
        results[user] = wait(completed)
        assert results[user]['status'] == 'completed', results[user]
        result = results[user]['result']
        assert result['requester_user_id'] == user and result['audience'] == 'private'
        assert [row['code'] for row in result['candidates']] == ['600001']
        proof = json.dumps(result)

        def digest(value):
            return hashlib.sha256(value.encode()).hexdigest()[:12]
        assert digest('sk-' + user) in proof
        assert digest(user + '-search') in proof
        assert digest(user + '-data') in proof
        other = USER_B if user == USER_A else USER_A
        assert digest('sk-' + other) not in proof
        with pytest.raises(HTTPException) as error:
            f.service.get({'task_id': tasks[user]['task_id']}, owner=f.owners[other])
        assert error.value.status_code == 404
        assert [r['task_id'] for r in f.service.get({}, owner=f.owners[user])['tasks']] == [tasks[user]['task_id']]
    assert dict(os.environ) == before
    from src.storage import LLMUsage, ScreeningRun
    with f.app.db.get_session() as session:
        usage = session.query(LLMUsage).filter_by(call_type='screening').all()
        assert {row.user_id for row in usage} == {USER_A, USER_B}
        assert all((row.total_tokens == 16 for row in usage))
        assert session.query(ScreeningRun).count() == 0
    from src.services.task_queue import get_task_queue
    queue = get_task_queue()
    assert {row.task_id for row in queue.list_all_tasks(viewer_user_id=USER_A)} == {tasks[USER_A]['task_id']}
    assert {row.task_id for row in queue.list_all_tasks(viewer_user_id=USER_B)} == {tasks[USER_B]['task_id']}


def test_missing_model_wrong_owner_and_revocation_never_queue_system_credential_work(fixture):
    f = fixture
    from src.services.user_settings_service import UserSettingsService
    from src.services.user_config_service import invalidate_user_config
    UserSettingsService().set_settings(USER_B, {'LLM_CHANNELS': '', 'LLM_MINE_API_KEY': '', 'LLM_MINE_MODELS': ''})
    invalidate_user_config(USER_B)
    with pytest.raises(Exception):
        f.service.start({'strategy': 'dual_low'}, owner=f.owners[USER_B], session=SimpleNamespace(user_id=USER_B), operation_id='no-model')
    assert f.service.get({}, owner=f.owners[USER_B])['tasks'] == []
    with pytest.raises(HTTPException) as error:
        f.service.start({'strategy': 'dual_low'}, owner=f.owners[USER_A], session=SimpleNamespace(user_id=USER_B), operation_id='spoof')
    assert error.value.status_code == 403
    f.active[USER_A] = False
    with pytest.raises(HTTPException):
        f.service.start({'strategy': 'dual_low'}, owner=f.owners[USER_A], session=SimpleNamespace(user_id=USER_A), operation_id='revoked')
    assert f.service.get({}, owner=f.owners[USER_A])['tasks'] == []


def test_same_user_concurrent_jobs_other_process_and_durable_authority(fixture):
    import multiprocessing
    from tests.fixtures.finance_screening_worker import gated_worker, observe_tasks
    from src.services.finance_user_screening import _cache_directory, _user_config
    f = fixture
    owner = f.owners[USER_A]
    first_service = FinanceUserScreening(store=f.store, worker_target=gated_worker)
    second_service = FinanceUserScreening(store=f.store, worker_target=gated_worker)
    tasks = [service.start({'strategy': 'dual_low'}, owner=owner, session=SimpleNamespace(user_id=USER_A), operation_id=f'parallel-{index}') for index, service in enumerate((first_service, second_service))]
    assert tasks[0]['task_id'] != tasks[1]['task_id']
    caches = [_cache_directory(_user_config(USER_A), owner, task['task_id']) for task in tasks]
    assert caches[0] != caches[1]
    for cache in caches:
        wait(lambda: (cache / 'ready').exists())
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    observer = context.Process(target=observe_tasks, args=(child, f.store.path, owner))
    observer.start()
    assert parent.poll(30)
    assert {row['status'] for row in parent.recv()['tasks']} == {'processing'}
    observer.join(10)
    assert observer.exitcode == 0
    with f.store.db() as db:
        db.execute('UPDATE grants SET expires=0 WHERE id=?', (owner['delegationId'],))
    caches[0].joinpath('release').touch()

    def state(index):
        return f.service.get({'task_id': tasks[index]['task_id']}, owner=owner)['status']
    wait(lambda: state(0) == 'completed')
    with pytest.raises(HTTPException):
        first_service.start({'strategy': 'dual_low'}, owner=owner, session=SimpleNamespace(user_id=USER_A), operation_id='expired-new')
    with f.store.db() as db:
        db.execute('UPDATE grants SET revoked=1 WHERE id=?', (owner['delegationId'],))
    caches[1].joinpath('release').touch()
    wait(lambda: state(1) == 'failed')


def test_running_task_aborts_when_personal_connection_changes(fixture):
    from tests.fixtures.finance_screening_worker import gated_worker
    from src.services.finance_user_screening import _cache_directory, _user_config
    f = fixture
    owner = f.owners[USER_A]
    service = FinanceUserScreening(store=f.store, worker_target=gated_worker)
    task = service.start({'strategy': 'dual_low'}, owner=owner,
                         session=SimpleNamespace(user_id=USER_A), operation_id='connection-change')
    cache = _cache_directory(_user_config(USER_A), owner, task['task_id'])
    wait(lambda: cache.joinpath('ready').exists())
    give_user_model(USER_A, key='synthetic-replaced-key', model='user-model')
    cache.joinpath('release').touch()
    wait(lambda: service.get({'task_id': task['task_id']}, owner=owner)['status'] == 'failed')
