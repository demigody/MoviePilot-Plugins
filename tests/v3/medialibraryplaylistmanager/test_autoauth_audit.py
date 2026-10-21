"""Independent integration audit for authorization visibility and credential isolation."""
import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.plugins.medialibraryplaylistmanager.emby import EmbyAPIError
from app.plugins.medialibraryplaylistmanager.jellyfin import JellyfinAuthError, JellyfinClient
from app.plugins.medialibraryplaylistmanager.models import MediaList, MediaMember
from .test_api import plugin
from .test_jellyfin import jellyfin_listing
from .test_jellyfin_auth import AutoServer, connect


def wire_factory(plugin, monkeypatch, server):
    import app.plugins.medialibraryplaylistmanager as module
    class Client(JellyfinClient):
        def __init__(self,host,key,user,**kwargs):
            super().__init__(host,key,user,server,**kwargs)
    monkeypatch.setitem(module.SERVER_CLIENTS,'jellyfin',Client)
    conf={'kind':'jellyfin','host':'http://server/jellyfin','apikey':'private','default_user_id':'u'}
    plugin._host.servers=lambda:{'family':copy.deepcopy(conf)}
    return conf


def test_completed_job_and_all_public_reads_never_expose_cached_user_session(plugin,monkeypatch):
    server=AutoServer()
    wire_factory(plugin,monkeypatch,server)
    value=jellyfin_listing()
    value.targets[0].server_id='family'
    saved=plugin.api_save_lists({'list':value.to_dict()})
    assert saved['success']
    before=copy.deepcopy(plugin._config)
    queued=plugin.api_publish({'list_id':value.id})
    assert queued['success']
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        job=plugin.api_job(queued['job_id'])
        if job['status'] not in {'queued','running'}:
            break
        time.sleep(.005)
    assert job['status']=='success',job
    backend=json.dumps(plugin.get_data('jellyfin_sessions'))
    assert 'user-session-1' in backend
    public=json.dumps([queued,job,plugin.api_settings(),plugin.api_lists(),plugin.api_servers(),plugin.api_history()],ensure_ascii=False)
    for secret in ('private','private-quick-secret','user-session-1'):
        assert secret not in public
    assert plugin._config==before
    assert [item for _,item in server.playlists['p']]==['m2','m1']


def test_real_reload_cancels_inflight_grant_without_persisting_a_stale_token(plugin,monkeypatch):
    server=AutoServer()
    wire_factory(plugin,monkeypatch,server)
    client=plugin._client('family')
    reached,release=threading.Event(),threading.Event()
    def pause(method,path):
        if path=='QuickConnect/Authorize':
            reached.set()
            assert release.wait(2)
    server.after_request=pause
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(client.ensure_playlist_access)
        try:
            assert reached.wait(1)
            plugin.init_plugin(copy.deepcopy(plugin._config))
        finally:
            release.set()
        with pytest.raises(JellyfinAuthError) as failure:
            pending.result(timeout=2)
    assert failure.value.code=='cancelled'
    assert all('token' not in entry for entry in plugin.get_data('jellyfin_sessions')['entries'].values())
    assert not server.writes


def test_reload_generation_guard_blocks_save_even_after_authentication_finished(plugin,monkeypatch):
    server=AutoServer()
    wire_factory(plugin,monkeypatch,server)
    client=plugin._client('family')
    session=plugin._jellyfin_sessions
    original_save=session._save
    reached,release=threading.Event(),threading.Event()
    def pause_before_guarded_save(data):
        if any('token' in entry for entry in data['entries'].values()):
            reached.set()
            assert release.wait(2)
        return original_save(data)
    session._save=pause_before_guarded_save
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(client.ensure_playlist_access)
        try:
            assert reached.wait(1)
            plugin.init_plugin(copy.deepcopy(plugin._config))
        finally:
            release.set()
        with pytest.raises(JellyfinAuthError) as failure:
            pending.result(timeout=2)
    assert failure.value.code=='cancelled'
    assert all('token' not in entry for entry in plugin.get_data('jellyfin_sessions')['entries'].values())
    assert server.grants==1 and not server.writes


def test_username_lookup_denial_survives_factory_and_provider_error_boundaries(plugin,monkeypatch):
    import app.plugins.medialibraryplaylistmanager as module
    class Client:
        def __init__(self,*args,**kwargs): self.user_id=''
        def resolve_authenticated_user_id(self): return None
        def resolve_user_id(self,name):
            raise EmbyAPIError('private-response-secret',status=403,code='permission_denied')
    monkeypatch.setitem(module.SERVER_CLIENTS,'jellyfin',Client)
    plugin._host.servers=lambda:{'family':{'kind':'jellyfin','host':'http://server','apikey':'private','username':'Owner'}}
    result=plugin.api_libraries({'server_id':'family'})
    assert not result['success']
    assert result['error_code']=='authorization_denied',result
    assert '用户' in result['error'] and 'private' not in str(result)


def test_subscription_only_auth_failure_keeps_specific_safe_reason(plugin,monkeypatch):
    import app.plugins.medialibraryplaylistmanager as module
    class Client:
        def __init__(self,*args,**kwargs): self.user_id=''
        def resolve_authenticated_user_id(self):
            raise EmbyAPIError('private-response-secret',status=401)
    monkeypatch.setitem(module.SERVER_CLIENTS,'jellyfin',Client)
    plugin._host.servers=lambda:{'family':{'kind':'jellyfin','host':'http://server','apikey':'private','default_user_id':'u'}}
    value=MediaList('只订阅',members=[MediaMember(1)],policy={'auto_subscribe':True})
    result=plugin._subscribe_pending(value)[0]
    assert result['error_code']=='authorization_credentials_invalid',result
    assert '无效或已撤销' in result['error'] and 'private' not in str(result)
    assert plugin._host.subscriptions==[]


def test_playlist_denial_after_autoauth_reports_edit_permission_without_manual_token_instructions():
    server=AutoServer()
    client,_,_=connect(server)
    path='Playlists/p/Items/e1/Move/0'
    server.failure[('POST',path)]=(403,{'secret':'private-response-secret'})
    with pytest.raises(JellyfinAuthError) as failure:
        client.move_playlist_entry('p','e1',0)
    assert '编辑权限' in str(failure.value) and '访问令牌' not in str(failure.value)
    assert 'private' not in str(failure.value)
    assert server.grants==1
    assert len([1 for method,item,*_ in server.auth_calls if method=='POST' and item==path])==1


@pytest.mark.parametrize('status,code,reason',[(403,'permission_denied','编辑权限'),(401,'authorization_denied','HTTP 401')])
def test_denial_after_token_renewal_keeps_specific_reason_and_never_attempts_a_third_write(status,code,reason):
    server=AutoServer()
    client,_,_=connect(server)
    client.ensure_playlist_access()
    path='Playlists/p/Items/e1/Move/0'
    attempts=[]
    def fail_after_renewal(method,item):
        if method=='POST' and item==path:
            attempts.append(item)
            server.failure[(method,item)]=(401 if len(attempts)==1 else status,{'secret':'private-response-secret'})
    server.after_request=fail_after_renewal
    with pytest.raises(JellyfinAuthError) as failure:
        client.move_playlist_entry('p','e1',0)
    assert failure.value.code==code and reason in str(failure.value)
    assert 'private' not in str(failure.value)
    assert len(attempts)==2 and server.grants==2
