"""Synthetic provider boundaries for a real isolated screening worker."""


def gated_worker(connection, config, request, environment, user_id):
    """Process lifecycle fixture; the full engine has a separate provider test."""
    import time
    from pathlib import Path
    connection.send({'type': 'authorize'})
    if connection.recv() is not True:
        return
    cache = Path(environment['SCREENING_DATA_DIR'])
    (cache / 'ready').touch()
    while not (cache / 'release').exists():
        time.sleep(0.02)
    connection.send({'type': 'authorize'})
    if connection.recv() is True:
        connection.send({'type': 'result', 'result': {'candidates': []}})
    connection.close()


def observe_tasks(connection, path, owner):
    from src.services.finance_identity import FinanceIdentityStore
    from src.services.finance_user_screening import FinanceUserScreening
    connection.send(FinanceUserScreening(store=FinanceIdentityStore(path)).get({}, owner=owner))
    connection.close()


def screening_worker(connection, config, request, environment, user_id):
    import hashlib
    import json
    import socket
    from types import SimpleNamespace
    import pandas as pd
    import litellm
    from src.services.screening import pipeline, daily
    from src.llm import credentials_guard
    from src.search_service import TavilySearchProvider, SearchResponse, SearchResult
    from data_provider.base import DataFetcherManager
    from src.services.finance_user_screening import _execute_worker

    def digest(value):
        return hashlib.sha256(str(value).encode()).hexdigest()[:12]
    seen_search = []

    def search(self, query, api_key, max_results, **kwargs):
        seen_search.append(digest(api_key))
        return SearchResponse(query=query, provider='Tavily', results=[SearchResult(title='600001 合成公司公告', snippet='600001 新业务量产', url='https://example.test/news', source='Tavily')])
    TavilySearchProvider._do_search = search
    credentials_guard.assert_public_https_url = lambda value: value
    socket.getaddrinfo = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('External network forbidden'))
    rows = [dict(code='600001', name='合成公司', price=12, change_pct=1, amount=200000000, pe_ratio=8, pb_ratio=1, total_mv=20000000000, turnover_rate=2, volume_ratio=1.2), dict(code='600002', name='过高估值', price=12, change_pct=1, amount=200000000, pe_ratio=80, pb_ratio=5, total_mv=20000000000, turnover_rate=2, volume_ratio=1.2)]

    def snapshot(*args, **kwargs):
        frame = pd.DataFrame(rows)
        frame.attrs['snapshot_source'] = 'synthetic'
        return frame
    pipeline.fetch_snapshot_with_fallback = snapshot
    frame = pd.DataFrame({'date': pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=140), 'open': [10.0] * 140, 'high': [12.0] * 140, 'low': [9.0] * 140, 'close': [11.0] * 140, 'volume': [1000000] * 140, 'amount': [100000000] * 140, 'pct_chg': [1.0] * 140})
    daily.fetch_daily_history = lambda *args, **kwargs: frame.copy()
    DataFetcherManager.get_daily_data = lambda *args, **kwargs: (frame.copy(), 'synthetic')
    DataFetcherManager.get_realtime_quote = lambda self, code, **kwargs: SimpleNamespace(code=code, name='合成公司', price=12)
    DataFetcherManager.get_stock_name = lambda *args, **kwargs: '合成公司'
    DataFetcherManager.get_fundamental_context = lambda *args, **kwargs: {}
    DataFetcherManager.get_capital_flow_context = lambda *args, **kwargs: {}
    DataFetcherManager.get_dragon_tiger_context = lambda *args, **kwargs: {}
    DataFetcherManager.get_board_context = lambda *args, **kwargs: {}

    def completion(**kwargs):
        key = kwargs.get('api_key') or ''
        reason = json.dumps({'model_key': digest(key), 'search_keys': seen_search, 'data_key': digest(environment.get('TUSHARE_TOKEN', ''))})
        content = json.dumps({'ranked': [{'code': '600001', 'llm_score': 91, 'confidence': 0.9, 'reason': reason, 'risk': 'synthetic', 'sector': '合成', 'theme': '验证'}]})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], model=kwargs.get('model'), usage=SimpleNamespace(prompt_tokens=11, completion_tokens=5, total_tokens=16))
    litellm.completion = completion

    class Router:

        def __init__(self, model_list=None, **kwargs):
            self.models = model_list

        def completion(self, **kwargs):
            selected = next((item for item in self.models if item['model_name'] == kwargs['model']))
            return completion(**{**selected['litellm_params'], **kwargs})
    litellm.Router = Router
    _execute_worker(connection, config, request, environment, user_id)
