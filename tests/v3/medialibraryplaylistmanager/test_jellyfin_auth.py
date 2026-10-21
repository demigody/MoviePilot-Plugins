"""Official Quick Connect authorization and publishing with MP credentials only."""
import copy
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.plugins.medialibraryplaylistmanager.emby import EmbyAPIError
from app.plugins.medialibraryplaylistmanager.jellyfin import JellyfinAuthError, JellyfinClient
from app.plugins.medialibraryplaylistmanager.jellyfin_auth import JellyfinSessions
from app.plugins.medialibraryplaylistmanager.sync import Publisher
from .test_jellyfin import JellyfinServer, jellyfin_listing
from .test_api import plugin


class Store:
    def __init__(self): self.data, self.writes = None, []
    def load(self): return copy.deepcopy(self.data)
    def save(self, data):
        self.data=copy.deepcopy(data); self.writes.append(copy.deepcopy(data))


class AutoServer(JellyfinServer):
    def __init__(self):
        super().__init__()
        self.auth_calls=[]; self.tokens={}; self.api_keys={'private'}
        self.enabled=True; self.disabled=False; self.grants=0; self.failure={}
        self.after_request=None

    def client(self):
        return JellyfinClient('http://server/jellyfin','private','u',self,page_size=2)

    def request(self, method,path,params=None,json=None,headers=None,timeout=None):
        params=dict(params or {})
        self.auth_calls.append((method,path,copy.deepcopy(params),copy.deepcopy(json),dict(headers)))
        token=re.search(r'Token="([^"]*)"',headers['Authorization']).group(1)
        if self.after_request: self.after_request(method,path)
        if (method,path) in self.failure: return self.failure[(method,path)]
        if path=='Users/Me':
            if token in self.api_keys: return 400,{}
            return {'Id':self.tokens[token]} if token in self.tokens else (401,{})
        if path.startswith('Users/') and path not in {'Users/AuthenticateWithQuickConnect','Users/u/Items'}:
            if method=='GET' and len(path.split('/'))==2:
                assert token in self.api_keys
                return {'Id':path.split('/')[1], 'Policy':{'IsDisabled':self.disabled}}
        if path=='QuickConnect/Enabled': return self.enabled
        if path=='QuickConnect/Initiate':
            assert method=='POST' and token in self.api_keys
            assert all(x+'="' in headers['Authorization'] for x in ['Client','Device','DeviceId','Version'])
            self.device=re.search(r'DeviceId="([^"]+)"',headers['Authorization']).group(1)
            return {'Code':'123456','Secret':'private-quick-secret'}
        if path=='QuickConnect/Authorize':
            assert method=='POST' and token in self.api_keys and params['Code']=='123456'
            self.owner=params['UserId']; self.grants+=1
            self.pending='user-session-'+str(self.grants)
            self.tokens[self.pending]=self.owner
            return True
        if path=='Users/AuthenticateWithQuickConnect':
            assert method=='POST' and json=={'Secret':'private-quick-secret'}
            return {'AccessToken':self.pending,'User':{'Id':self.owner}}
        if path.startswith('Playlists') and method!='GET':
            if token not in self.tokens: return 401,{}
            assert self.tokens[token]=='u'
        elif path.startswith('Collections') and method!='GET':
            assert token in self.api_keys, 'collection operations must retain the MP API key'
        return super().request(method,path,params,json,{**headers,'Authorization':'MediaBrowser Token="private"'},timeout)


def connect(server, store=None, sessions=None, server_id='family'):
    store=store or Store()
    sessions=sessions or JellyfinSessions(store.load,store.save)
    client=server.client()
    client.token_provider=lambda source,force: sessions.get(server_id,source,
        rejected_token=getattr(source._playlist_user,'api_key',None) if force else None)
    client.authorization_check=lambda source: sessions.check(server_id,source)
    return client,sessions,store


def test_mp_key_automatically_authorizes_then_preserves_order_and_uses_user_only_for_playlist():
    server=AutoServer(); client,sessions,store=connect(server)
    value=jellyfin_listing()
    before=copy.deepcopy(value.to_dict())
    result=Publisher(client).publish(value,value.targets[0])
    assert result['success'],result
    assert server.grants==1 and client.api_key=='private'
    assert [item for _,item in server.playlists['p']]==['m2','m1']
    assert value.to_dict()==before
    assert len(store.data['entries'])==1
    assert Publisher(client).publish(value,value.targets[0])['success']
    assert server.grants==1
    steps=[path for method,path,*_ in server.auth_calls if method=='POST' and ('QuickConnect' in path)]
    assert steps==['QuickConnect/Initiate','QuickConnect/Authorize','Users/AuthenticateWithQuickConnect']
    assert all(not body or 'Pw' not in body for _,_,_,body,_ in server.auth_calls)


def test_preview_is_get_only_and_no_session_cache_or_binding_writes():
    server=AutoServer(); client,_,store=connect(server)
    value=jellyfin_listing(bound=False)
    before=copy.deepcopy(value.to_dict())
    assert Publisher(client).publish(value,value.targets[0],dry_run=True)['success']
    assert all(method=='GET' for method,*_ in server.auth_calls)
    assert store.writes==[] and value.to_dict()==before and server.grants==0


def test_saved_token_reused_after_plugin_restart_even_when_quick_connect_is_later_disabled():
    server=AutoServer(); client,_,store=connect(server)
    client.ensure_playlist_access()
    old_device=server.device
    server.enabled=False; server.auth_calls.clear()
    restarted,sessions,_=connect(server,store)
    restarted.check_playlist_access()
    restarted.ensure_playlist_access()
    assert server.grants==1 and server.device==old_device
    assert all(method=='GET' for method,*_ in server.auth_calls)


def test_revoked_cached_token_and_write_401_renew_automatically_on_same_device():
    server=AutoServer(); client,_,store=connect(server)
    client.ensure_playlist_access(); device=server.device
    server.tokens.clear()
    client.move_playlist_entry('p','e1',0)
    assert server.grants==2 and server.device==device
    server.tokens.clear()
    restarted,_,_=connect(server,store)
    restarted.ensure_playlist_access()
    assert server.grants==3 and server.device==device


@pytest.mark.parametrize('status',[403,500,504])
def test_ambiguous_or_forbidden_playlist_write_never_reauthorizes_or_repeats(status):
    server=AutoServer(); client,_,_=connect(server)
    client.ensure_playlist_access()
    path='Playlists/p/Items/e1/Move/0'
    server.failure[('POST',path)]=(status,{})
    with pytest.raises(EmbyAPIError): client.move_playlist_entry('p','e1',0)
    assert len([1 for method,item,*_ in server.auth_calls if method=='POST' and item==path])==1
    assert server.grants==1


@pytest.mark.parametrize('path,status,code',[
    ('Users/Me',401,'authorization_credentials_invalid'),
    ('Users/u',404,'authorization_user_missing'),
    ('QuickConnect/Authorize',403,'authorization_denied'),
    ('QuickConnect/Authorize',404,'authorization_expired'),
    ('QuickConnect/Authorize',401,'quick_connect_disabled'),
    ('QuickConnect/Enabled',404,'authorization_unsupported'),
    ('Users/AuthenticateWithQuickConnect',500,'server_error')])
def test_auth_failures_have_specific_safe_reasons_and_stop_before_output_mutations(path,status,code):
    server=AutoServer(); client,_,_=connect(server)
    method='GET' if path.startswith('Users/u') or path in {'Users/Me','QuickConnect/Enabled'} else 'POST'
    server.failure[(method,path)]=(status,{'secret':'private-server-secret'})
    value=jellyfin_listing()
    result=Publisher(client).publish(value,value.targets[0])
    assert not result['success'] and result['error_code']==code,result
    assert result['error'] and 'private' not in str(result)
    assert not server.writes


@pytest.mark.parametrize('mode,code',[('disabled','quick_connect_disabled'),('user_disabled','authorization_user_disabled'),
    ('bad_enabled','authorization_invalid_response'),('mismatch','authorization_user_mismatch')])
def test_prerequisite_errors_are_explicit_and_never_create_output(mode,code):
    server=AutoServer(); client,_,_=connect(server)
    if mode=='disabled': server.enabled=False
    if mode=='bad_enabled': server.enabled={'unexpected':False}
    if mode=='user_disabled': server.disabled=True
    if mode=='mismatch': server.failure[('GET','Users/u')]={'Id':'other'}
    with pytest.raises(JellyfinAuthError) as error: client.ensure_playlist_access()
    assert error.value.code==code
    assert not server.writes and server.grants==0


def test_grants_are_single_flight_and_cache_is_scoped_to_server_user_address_and_api_key():
    server=AutoServer(); client,sessions,store=connect(server)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert len(set(pool.map(lambda _:sessions.get('family',client),range(4))))==1
    assert server.grants==1
    for name,change in [('other',{}),('family',{'user_id':'second'}),('family',{'base_url':'http://other/jellyfin/'}),
        ('family',{'api_key':'private-new'})]:
        other=server.client()
        for key,value in change.items(): setattr(other,key,value)
        server.api_keys.add(other.api_key)
        sessions.get(name,other)
    assert server.grants==5 and len(store.data['entries'])==5


def test_storage_failure_and_cancelled_inflight_auth_block_publish():
    server=AutoServer(); store=Store()
    sessions=JellyfinSessions(store.load,lambda _:False)
    client,_,_=connect(server,store,sessions)
    with pytest.raises(JellyfinAuthError) as error: client.ensure_playlist_access()
    assert error.value.code=='authorization_storage_error' and server.grants==0
    client,sessions,store=connect(server)
    server.after_request=lambda method,path:sessions.stop() if path=='QuickConnect/Authorize' else None
    with pytest.raises(JellyfinAuthError) as error: client.ensure_playlist_access()
    assert error.value.code=='cancelled'
    assert all('token' not in entry for entry in store.data['entries'].values())
    assert not server.writes


def test_factory_injects_automatic_auth_without_changing_mp_config_and_detects_key_changes(plugin,monkeypatch):
    import app.plugins.medialibraryplaylistmanager as module
    server=AutoServer()
    class Client(JellyfinClient):
        def __init__(self,host,key,user,**kwargs): super().__init__(host,key,user,server,**kwargs)
    monkeypatch.setitem(module.SERVER_CLIENTS,'jellyfin',Client)
    conf={'kind':'jellyfin','host':'http://server/jellyfin','apikey':'private','default_user_id':'u'}
    plugin._host.servers=lambda:{'family':conf}
    original=copy.deepcopy(conf)
    client=plugin._client('family'); client.ensure_playlist_access()
    assert conf==original and plugin._client('family') is client
    conf['apikey']='private-new'; server.api_keys.add('private-new')
    newer=plugin._client('family'); newer.ensure_playlist_access()
    assert newer is not client and server.grants==2
    assert plugin.get_data('jellyfin_sessions')['entries']
    public=plugin.api_servers()
    assert 'private' not in str(public) and 'session' not in str(public)


def test_exchanged_application_key_cannot_be_saved_or_used_as_a_verified_user_session():
    server=AutoServer(); client,_,store=connect(server)
    server.failure[('POST','Users/AuthenticateWithQuickConnect')]={'AccessToken':'private','User':{'Id':'u'}}
    with pytest.raises(JellyfinAuthError) as error: client.ensure_playlist_access()
    assert error.value.code=='authorization_invalid_response'
    assert all('token' not in entry for entry in store.data['entries'].values())
    assert client._playlist_user is None and not server.writes
