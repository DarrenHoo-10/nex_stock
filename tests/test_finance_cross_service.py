"""Real loopback Stock/Core/nex_user contract test; all provider data is synthetic.

Set NEXBOT_TEST_CHECKOUT to the NexBot working tree containing
server/personal-os/testing/cross-stock-fixture.ts. No production credentials,
network endpoints, dependency overrides or fake business methods are used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import requests


def _wait(predicate, timeout=30):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        value=predicate()
        if value:
            return value
        time.sleep(.04)
    raise AssertionError("synthetic cross-service fixture timed out")


@pytest.fixture
def services(tmp_path,monkeypatch):
    checkout=os.environ.get("NEXBOT_TEST_CHECKOUT")
    if not checkout:
        pytest.skip("Set NEXBOT_TEST_CHECKOUT to run the offline loopback Core fixture")
    checkout=Path(checkout).resolve()
    fixture=checkout/"server/personal-os/testing/cross-stock-fixture.ts"
    assert fixture.is_file(),"NexBot cross-stock fixture missing"
    node=shutil.which("node")
    assert node,"Node is required for cross-service acceptance"
    listener=socket.socket();listener.bind(("127.0.0.1",0));listener.listen(128)
    origin=f"http://127.0.0.1:{listener.getsockname()[1]}"
    key="synthetic-cross-service-token-"+"x"*40
    config=tmp_path/"core-fixture.json"
    config.write_text(json.dumps({"dir":str(tmp_path/"core"),"stockUrl":origin,"serviceToken":key}),encoding="utf-8")
    child=subprocess.Popen([node,"--experimental-transform-types",str(fixture),str(config)],cwd=checkout,
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding="utf-8",bufsize=1)
    output=queue.Queue()
    def collect():
        for line in child.stdout:
            output.put(line.rstrip())
    threading.Thread(target=collect,daemon=True).start()
    def result(prefix,timeout=60):
        deadline=time.monotonic()+timeout
        diagnostics=[]
        while time.monotonic()<deadline:
            try:line=output.get(timeout=.2)
            except queue.Empty:
                if child.poll() is not None:raise AssertionError("Core fixture exited: "+"\n".join(diagnostics[-8:]))
                continue
            if line.startswith(prefix):return json.loads(line[len(prefix):])
            diagnostics.append(line)
        raise AssertionError("Core fixture readiness timed out: "+"\n".join(diagnostics[-8:]))
    command_id=0
    def command(action,**arguments):
        nonlocal command_id
        command_id+=1;child.stdin.write(json.dumps({"id":command_id,"action":action,**arguments})+"\n");child.stdin.flush()
        return result("NEX_CROSS_SERVICE_RESULT ")["result"]
    server=None;thread=None
    try:
        ready=result("NEX_CROSS_SERVICE_READY ")
        env=tmp_path/".env";env.write_text("STOCK_LIST=600519\n",encoding="utf-8")
        values={"ENV_FILE":str(env),"DATABASE_PATH":str(tmp_path/"stock.db"),"AUTH_MODE":"user_center",
            "USER_CENTER_URL":ready["identityOrigin"],"USER_CENTER_REQUIRED_ROLE":"user","PUBLIC_ORIGIN":origin,
            "PRIMARY_USER_ID":"synthetic-owner-uid","STOCK_LIST":"600519","FINANCE_SERVICE_TOKEN":key,"FINANCE_INSTANCE_ID":"fixture-stock",
            "FINANCE_CORE_ID":"fixture-core","FINANCE_CORE_URL":ready["coreOrigin"],"FINANCE_FEISHU_MANAGED":"false","LITELLM_LOCAL_MODEL_COST_MAP":"True"}
        for name,value in values.items():monkeypatch.setenv(name,value)
        # Fail any accidental production access, including from a business service.
        original=requests.Session.request
        def offline(session,method,url,*args,**kwargs):
            assert urlsplit(str(url)).hostname in {"127.0.0.1","localhost","::1"},"nonfixture network refused"
            session.trust_env=False
            return original(session,method,url,*args,**kwargs)
        monkeypatch.setattr(requests.Session,"request",offline)
        lookup=socket.getaddrinfo
        def loopback_lookup(host,*args,**kwargs):
            assert host in {None,"127.0.0.1","localhost","::1",b"127.0.0.1"},"nonfixture DNS refused"
            return lookup(host,*args,**kwargs)
        monkeypatch.setattr(socket,"getaddrinfo",loopback_lookup)
        from src.config import Config
        from src.storage import DatabaseManager
        Config.reset_instance();DatabaseManager.reset_instance()
        import src.auth as auth
        # Restoring environment variables alone leaves user-center mode cached in later tests.
        for attr in ("_auth_enabled", "_auth_mode", "_session_secret"):
            monkeypatch.setattr(auth, attr, None)
        auth.refresh_auth_state()
        # Provider boundary only: service mapping and the actual quote route remain real.
        from data_provider.base import DataFetcherManager
        monkeypatch.setattr(DataFetcherManager,"get_realtime_quote",lambda self,code:SimpleNamespace(code=code,name="synthetic",price=123.45))
        from api.app import create_app
        app=create_app(static_dir=tmp_path/"empty-static")
        from api.v1.endpoints import closing_analysis
        reports=tmp_path/"reports";reports.mkdir()
        monkeypatch.setattr(closing_analysis,"_REPORTS_DIR",reports)
        import uvicorn
        server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=0,lifespan="off",access_log=False,log_level="error"))
        thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);thread.start()
        _wait(lambda:server.started)
        def account(subject):return next(a for a in ready["accounts"] if a["subject"]==subject)
        session=requests.Session();session.trust_env=False
        def stock(path,who="owner",method="GET",body=None,headers=None):
            principal=account("synthetic-owner-uid" if who=="owner" else "synthetic-other-uid")
            actual={"Cookie":"user_session="+principal["cookie"],"Origin":origin,"X-CSRF-Token":principal["csrf"],**(headers or {})}
            return session.request(method,origin+path,json=body,headers=actual,timeout=35)
        def core(path,who="owner",method="GET",body=None):
            token=ready["ownerCoreToken" if who=="owner" else "otherCoreToken"]
            return session.request(method,ready["coreOrigin"]+"/api/"+path,json=body,headers={"Authorization":"Bearer "+token},timeout=35)
        yield SimpleNamespace(stock=stock,core=core,command=command,origin=origin,ready=ready,session=session,key=key)
    finally:
        if server:server.should_exit=True
        if thread:thread.join(timeout=10)
        if child.poll() is None:
            try:child.stdin.write('{"action":"close"}\n');child.stdin.flush();child.wait(timeout=15)
            except (BrokenPipeError,subprocess.TimeoutExpired):child.kill();child.wait(timeout=5)
        listener.close()
        try:
            from src.storage import DatabaseManager
            from src.config import Config
            DatabaseManager.reset_instance();Config.reset_instance()
        except ImportError:pass


def test_stock_core_real_http_identity_topics_and_revocation(services):
    f=services;prefix="/api/v1/finance/assistant"
    assert f.stock(prefix+"/status").json()=={"managed":True,"bound":False}
    bad=f.stock(prefix+"/connect",method="POST",body={},headers={"X-CSRF-Token":"wrong"})
    assert bad.status_code==403
    bad=f.stock(prefix+"/connect",method="POST",body={},headers={"Origin":"https://attacker.invalid"})
    assert bad.status_code==403
    for who in ("owner","other"):
        response=f.stock(prefix+"/connect",who,method="POST",body={})
        assert response.status_code==200,response.text
        assert f.stock(prefix+"/topics",who).status_code==200
    f.command("model",text="synthetic first answer")
    first=f.stock(prefix+"/input",method="POST",body={"newConversation":True,"clientId":"cross-input","text":"public market question","user_id":"synthetic-other-uid"})
    assert first.status_code==202,first.text
    topic=first.json()["conversationId"]
    replay=f.stock(prefix+"/input",method="POST",body={"newConversation":True,"clientId":"cross-input","text":"public market question","user_id":"synthetic-other-uid"})
    assert replay.status_code==202 and replay.json()["conversationId"]==topic
    def visible():
        result=f.stock(prefix+"/messages?conversationId="+topic)
        assert result.status_code==200,result.text
        return any(m["content"]=="synthetic first answer" for m in result.json()["messages"])
    _wait(visible)
    finance=f.core("agents/finance").json()["botId"]
    native=f.core("agents/messages?bot="+finance+"&conversationId="+topic)
    assert native.status_code==200 and "synthetic first answer" in native.text
    assert f.stock(prefix+"/messages?conversationId="+topic,"other").status_code>=400
    assert f.stock(prefix+"/input","other",method="POST",body={"conversationId":topic,"clientId":"spoof","text":"try another account","user_id":"synthetic-owner-uid"}).status_code>=400
    other_finance=f.core("agents/finance","other").json()["botId"]
    assert other_finance!=finance
    assert topic not in f.core("agents/topics?bot="+other_finance,"other").text
    # One synthetic Feishu input traverses the same assistant and the real
    # delegated Stock quote business route before its durable card is patched.
    connector=next(c for c in f.core("state").json()["connectors"] if any(t["name"]=="call_api" for t in c["tools"]))
    assert f.core("feishu/register/start",method="POST",body={"botId":finance}).status_code==200
    _wait(lambda:any(app["status"]=="connected" for app in f.core("state").json()["feishu"]["apps"]))
    f.command("modelTool",arguments={"action":"connector.call","connectorId":connector["id"],"connectorVersion":connector["version"],"tool":"call_api","parameters":{"method":"GET","path":"/api/v1/stocks/AAPL/quote"}})
    f.command("model",text="synthetic Feishu quote answer")
    f.command("feishu",event={"header":{"app_id":"cli_fake"},"event":{"sender":{"sender_id":{"open_id":"ou_owner"},"sender_type":"user","tenant_key":"fake"},"message":{"message_id":"cross-feishu","chat_id":"synthetic-dm","chat_type":"p2p","message_type":"text","content":json.dumps({"text":"synthetic Feishu question"})}}})
    _wait(lambda:"synthetic Feishu quote answer" in json.dumps(f.command("sent")))
    feishu_topic=next(t for t in f.core("agents/topics?bot="+finance).json()["topics"] if t["sourceContext"].get("channel")=="feishu")
    assert feishu_topic["sourceContext"]["chatId"]=="synthetic-dm"
    assert "synthetic Feishu quote answer" in f.stock(prefix+"/messages?conversationId="+feishu_topic["conversationId"]).text
    assert any("123.45" in (row["result_json"] or "") for row in f.command("inspect",subject="synthetic-owner-uid"))
    removed = feishu_topic["conversationId"]
    assert f.stock(prefix+"/delete", "other", method="POST", body={"conversationId": removed}).status_code >= 400
    for _ in range(2):
        deleted = f.stock(prefix+"/delete", method="POST", body={"conversationId": removed})
        assert deleted.status_code == 200 and deleted.json()["deleted"]
    assert removed not in f.stock(prefix+"/topics").text
    assert removed not in f.core("agents/topics?bot="+finance).text
    assert removed not in f.core("agents/messages?bot="+finance).text
    assert f.core("agents/messages?bot="+finance+"&conversationId="+removed).status_code >= 400
    assert visible()
    grants=f.stock("/api/v1/finance/delegations").json()["delegations"]
    assert grants
    revoked=f.stock("/api/v1/finance/delegations/"+grants[0]["delegationId"],method="DELETE")
    assert revoked.status_code==200,revoked.text
    assert f.stock(prefix+"/topics").status_code>=400
    assert f.core("agents/topics?bot="+finance).status_code==200
    f.command("revoke",subject="synthetic-other-uid")
    assert f.stock(prefix+"/topics","other").status_code==401
    assert f.core("agents/topics?bot="+other_finance,"other").status_code==401


@pytest.mark.skipif(not (Path(__file__).resolve().parents[1]/"src/services/watchlist_service.py").exists(), reason="P2 private data integration branch required")
def test_p2_real_http_delegated_watchlists_and_public_quotes(services):
    from api import deps
    if not hasattr(deps,"CurrentUser"):
        pytest.skip("P2 ownership branch runs in the integration worktree")
    f=services
    for who in ("owner","other"):
        assert f.stock("/api/v1/finance/assistant/connect",who,method="POST",body={}).status_code==200
    for who,code in (("owner","AAPL"),("other","00700")):
        response=f.stock("/api/v1/stocks/watchlist/add",who,method="POST",body={"stock_code":code,"user_id":"forged"})
        assert response.status_code==200,response.text
    assert "00700" not in f.stock("/api/v1/stocks/watchlist").json()["stock_codes"]
    assert f.stock("/api/v1/stocks/watchlist","other").json()["stock_codes"]==["00700"]
    grant={}
    for who in ("owner","other"):
        exchange=f.stock("/api/v1/finance/session-exchanges",who,method="POST",body={})
        redeemed=f.session.post(f.origin+"/api/v1/finance/session-exchanges/redeem",json={"code":exchange.json()["code"],"coreId":"fixture-core"},headers={"Authorization":"Bearer "+f.key},timeout=15)
        assert redeemed.status_code==200,redeemed.text
        grant[who]=redeemed.json()["delegationToken"]
    def tool(who,path,audience="private",**args):
        return f.session.post(f.origin+"/api/v1/finance/tools/call",headers={"Authorization":"Bearer "+f.key,"X-Finance-Delegation":grant[who],"X-Finance-Audience":audience},json={"name":"call_api","arguments":{"method":"GET","path":path,**args}},timeout=20)
    a=tool("owner","/api/v1/stocks/watchlist");b=tool("other","/api/v1/stocks/watchlist")
    assert a.status_code==b.status_code==200,(a.text,b.text)
    assert "AAPL" in a.text and "00700" not in a.text
    assert "00700" in b.text and "AAPL" not in b.text
    assert tool("other","/api/v1/stocks/watchlist","public").status_code==403
    assert tool("other","/api/v1/stocks/watchlist",user_id="synthetic-owner-uid").status_code==422
    # Exercise the complete model -> Core broker -> Stock service grant ->
    # authenticated business service -> SQLite write/read chain for both users.
    for who,own,foreign in (("owner","MSFT","NVDA"),("other","NVDA","MSFT")):
        assert f.stock("/api/v1/finance/assistant/topics",who).status_code==200
        bot=f.core("agents/finance",who).json()["botId"]
        connector=next(c for c in f.core("state",who).json()["connectors"] if any(t["name"]=="call_api" for t in c["tools"]))
        for method,path,body in (("POST","/api/v1/stocks/watchlist/add",{"stock_code":own}),("GET","/api/v1/stocks/watchlist",None)):
            parameters={"method":method,"path":path}
            if body is not None:parameters["body"]=body
            f.command("modelTool",arguments={"action":"connector.call","connectorId":connector["id"],"connectorVersion":connector["version"],"tool":"call_api","parameters":parameters})
            summary=f"{who} {method} complete"
            f.command("model",text=summary)
            sent=f.core("agents/input",who,method="POST",body={"botId":bot,"newConversation":True,"clientId":f"tool-{who}-{method}","text":"synthetic delegated operation"})
            assert sent.status_code==202,sent.text
            target=sent.json()
            _wait(lambda:summary in f.core("agents/messages?bot="+bot+"&conversationId="+target["conversationId"],who).text)
            traces=f.command("inspect",subject="synthetic-owner-uid" if who=="owner" else "synthetic-other-uid")
            trace=next(row for row in traces if row["task_id"]==target["taskId"] and row["tool_name"]=="connector.call")
            assert trace["status"]=="succeeded",trace
            assert own in trace["result_json"] and foreign not in trace["result_json"]
    assert "NVDA" not in f.stock("/api/v1/stocks/watchlist").text
    assert "MSFT" not in f.stock("/api/v1/stocks/watchlist","other").text
    quote=tool("other","/api/v1/stocks/AAPL/quote","public")
    assert quote.status_code==200 and "123.45" in quote.text,quote.text
