"""Fresh MCP console keeps work ownership separate from discovery and reports."""
import asyncio
import json
from types import SimpleNamespace

from ag_tasks.agent_console import Console, MAX_LINE

class Session:
    def __init__(self):
        self.calls=[]
    async def call_tool(self,name,args,**kwargs):
        self.calls.append((name,args))
        data={'id':'issue','work':{'activeSession':{'sessionId':'session'}}} if name=='tasks_work_start' else {'id':'issue'}
        return SimpleNamespace(isError=False,content=[SimpleNamespace(type='text',text=json.dumps(data))])

class Reader:
    def __init__(self,lines):self.lines=iter(lines)
    async def readline(self):return next(self.lines,b'')

def test_console_eof_stops_only_owned_session_and_does_not_auto_start():
    async def run():
        session=Session();out=[]
        console=Console(session,['tasks_get','tasks_work_start','tasks_work_stop'],output=out.append)
        await console.run(Reader([b'{"tool":"tasks_get","arguments":{"issue_id":"issue"}}']))
        assert [x[0] for x in session.calls]==['tasks_get']
        await console.run(Reader([b'{"tool":"tasks_work_start","arguments":{"issue_id":"issue","agent_id":"declared","session_key":"key"}}']))
        assert session.calls[-1]==('tasks_work_stop',{'issue_id':'issue','agent_id':'declared','session_key':'key','session_id':'session'})
        assert out[-1]['event']=='work_stopped'
    asyncio.run(run())

def test_console_rejects_unadvertised_and_second_task_without_mutation():
    async def run():
        session=Session();out=[];console=Console(session,['tasks_work_start','tasks_work_stop'],output=out.append)
        await console.run(Reader([b'{"tool":"tasks_approve","arguments":{}}', b'{"tool":"tasks_work_start","arguments":{"issue_id":"issue","agent_id":"a","session_key":"k"}}', b'{"tool":"tasks_work_start","arguments":{"issue_id":"different","agent_id":"a","session_key":"new"}}']))
        assert [x[0] for x in session.calls]==['tasks_work_start','tasks_work_stop']
        assert sum(bool(x.get('error')) for x in out)==2
    asyncio.run(run())

def test_console_rejects_oversized_input_without_mutation():
    async def run():
        session=Session();out=[];console=Console(session,['tasks_get'],output=out.append)
        await console.run(Reader([b'x'*(MAX_LINE+1)]))
        assert not session.calls and out[0]['error']['code']=='invalid_console_request'
    asyncio.run(run())


def test_idle_and_lease_failure_do_not_keep_renewing(monkeypatch):
    import ag_tasks.agent_console as module
    async def run():
        session=Session();out=[]
        console=Console(session,['tasks_work_start','tasks_work_stop','tasks_work_heartbeat'],idle_seconds=0,output=out.append)
        await console.invoke('tasks_work_start',{'issue_id':'issue','agent_id':'a','session_key':'k'})
        ticks=0
        async def tick(_):
            nonlocal ticks
            ticks+=1
            if ticks>1:raise asyncio.CancelledError()
        monkeypatch.setattr(module.asyncio,'sleep',tick)
        try:await console.maintain()
        except asyncio.CancelledError:pass
        assert console.active is None
        assert [name for name,_ in session.calls]==['tasks_work_start','tasks_work_stop']
        assert out[-1]['event']=='idle_paused'
    asyncio.run(run())


def test_domain_rejection_never_acquires_a_controller_lease():
    class Rejected(Session):
        async def call_tool(self,name,args,**kwargs):
            self.calls.append((name,args))
            return SimpleNamespace(isError=False,content=[SimpleNamespace(type='text',text='{"error":{"code":"definition_failed"}}')])
    async def run():
        session=Rejected();console=Console(session,['tasks_work_start'])
        value=await console.invoke('tasks_work_start',{'issue_id':'issue','agent_id':'a','session_key':'k'})
        assert value['error']['code']=='definition_failed' and console.active is None
    asyncio.run(run())


def test_close_control_pauses_owned_work_without_processing_later_commands():
    async def run():
        session=Session();out=[];console=Console(session,['tasks_work_start','tasks_work_stop','tasks_get'],output=out.append)
        await console.run(Reader([b'{"tool":"tasks_work_start","arguments":{"issue_id":"issue","agent_id":"a","session_key":"k"}}',b'{"control":"close"}',b'{"tool":"tasks_get"}']))
        assert [name for name,_ in session.calls]==['tasks_work_start','tasks_work_stop']
        assert console.active is None
    asyncio.run(run())


def test_definition_change_releases_console_ownership_of_closed_session():
    class Changing(Session):
        async def call_tool(self,name,args,**kwargs):
            if name=='tasks_update':
                return SimpleNamespace(isError=False,content=[SimpleNamespace(type='text',text='{"id":"issue","work":{"state":"paused","activeSession":null}}')])
            return await super().call_tool(name,args,**kwargs)
    async def run():
        session=Changing();console=Console(session,['tasks_work_start','tasks_update','tasks_work_stop'])
        await console.invoke('tasks_work_start',{'issue_id':'issue','agent_id':'a','session_key':'first'})
        await console.invoke('tasks_update',{'issue_id':'issue','dod':[]})
        assert console.active is None
        await console.invoke('tasks_work_start',{'issue_id':'issue','agent_id':'a','session_key':'final-verification'})
        assert console.active['session_key']=='final-verification'
        await console.pause()
    asyncio.run(run())


def test_queued_renewal_does_not_follow_successful_result_submission():
    """Force result closure before its response; a queued runner heartbeat must never hit the closed lease."""
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        class Completing(Session):
            async def call_tool(self, name, args, **kwargs):
                if name == 'tasks_submit_result':
                    self.calls.append((name, args))
                    entered.set()
                    await release.wait()
                    return SimpleNamespace(isError=False, content=[SimpleNamespace(type='text', text='{"id":"issue","currentResult":{"generation":1},"work":{"activeSession":null}}')])
                return await super().call_tool(name, args, **kwargs)
        session = Completing()
        console = Console(session, ['tasks_work_start', 'tasks_submit_result', 'tasks_work_heartbeat'])
        await console.invoke('tasks_work_start', {'issue_id': 'issue', 'agent_id': 'a', 'session_key': 'key'})
        credentials = console.active.copy()
        completion = asyncio.create_task(console.invoke('tasks_submit_result', credentials))
        await entered.wait()
        renewal = asyncio.create_task(console.renew(credentials))
        await asyncio.sleep(0)
        release.set()
        result, renewed = await asyncio.gather(completion, renewal)
        assert result['currentResult']['generation'] == 1
        assert renewed is None and console.active is None
        assert [name for name, _ in session.calls] == ['tasks_work_start', 'tasks_submit_result']
    asyncio.run(run())


def test_result_submission_waits_for_heartbeat_already_in_flight():
    """An earlier renewal completes before a later result; the result still closes the owned lease."""
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        class Renewing(Session):
            async def call_tool(self, name, args, **kwargs):
                if name == 'tasks_work_heartbeat':
                    self.calls.append((name, args))
                    entered.set()
                    await release.wait()
                    data = {'id': 'issue', 'work': {'activeSession': {'sessionId': 'session'}}}
                    return SimpleNamespace(isError=False, content=[SimpleNamespace(type='text', text=json.dumps(data))])
                return await super().call_tool(name, args, **kwargs)
        session = Renewing()
        console = Console(session, ['tasks_work_start', 'tasks_work_heartbeat', 'tasks_submit_result'])
        await console.invoke('tasks_work_start', {'issue_id': 'issue', 'agent_id': 'a', 'session_key': 'key'})
        credentials = console.active.copy()
        renewal = asyncio.create_task(console.renew(credentials))
        await entered.wait()
        completion = asyncio.create_task(console.invoke('tasks_submit_result', credentials))
        await asyncio.sleep(0)
        assert [name for name, _ in session.calls] == ['tasks_work_start', 'tasks_work_heartbeat']
        release.set()
        await asyncio.gather(renewal, completion)
        assert console.active is None
        assert session.calls[-1][0] == 'tasks_submit_result'
    asyncio.run(run())


def test_rejected_result_retains_lease_but_definition_rework_releases_it():
    """Payload correction can reuse live work; a server-confirmed closed definition cannot."""
    class Rejected(Session):
        async def call_tool(self, name, args, **kwargs):
            if name == 'tasks_submit_result':
                return SimpleNamespace(isError=False, content=[SimpleNamespace(type='text', text='{"error":{"code":"invalid_references"}}')])
            if name == 'tasks_update':
                return SimpleNamespace(isError=False, content=[SimpleNamespace(type='text', text='{"error":{"code":"definition_failed","details":{"issueId":"issue","status":"rework"}}}')])
            return await super().call_tool(name, args, **kwargs)
    async def run():
        console = Console(Rejected(), ['tasks_work_start', 'tasks_submit_result', 'tasks_update'])
        await console.invoke('tasks_work_start', {'issue_id': 'issue', 'agent_id': 'a', 'session_key': 'key'})
        credentials = console.active.copy()
        assert (await console.invoke('tasks_submit_result', credentials))['error']['code'] == 'invalid_references'
        assert console.active == credentials
        await console.invoke('tasks_update', {'issue_id': 'issue', 'dod': []})
        assert console.active is None
    asyncio.run(run())


def test_owner_outage_retries_same_heartbeat_and_recovers_without_restarting_work(monkeypatch):
    """Two temporary HTTP outages preserve exact credentials; a later renewal confirms server ownership."""
    import ag_tasks.agent_console as module
    async def run():
        class Restarting(Session):
            """Keep one MCP transport while its backing HTTP owner changes."""
            async def call_tool(self, name, args, **kwargs):
                if name != 'tasks_work_heartbeat':
                    return await super().call_tool(name, args, **kwargs)
                self.calls.append((name, args.copy()))
                count = sum(call[0] == name for call in self.calls)
                data = {'error':{'code':'tasks_unavailable'}} if count <= 2 else {
                    'id':'issue', 'work':{'activeSession':{'sessionId':'session'}}}
                return SimpleNamespace(isError=False, content=[SimpleNamespace(type='text', text=json.dumps(data))])
        session, output = Restarting(), []
        console = Console(session, ['tasks_work_start','tasks_work_heartbeat'], output=output.append)
        await console.invoke('tasks_work_start', {'issue_id':'issue','agent_id':'a','session_key':'key'})
        credentials = console.active.copy()
        ticks = []
        async def tick(delay):
            """Run the actual 25-second schedule deterministically without waiting in a unit test."""
            ticks.append(delay)
            if len(ticks) > 3:
                raise asyncio.CancelledError()
        monkeypatch.setattr(module.asyncio, 'sleep', tick)
        try:
            await console.maintain()
        except asyncio.CancelledError:
            pass
        assert ticks == [25,25,25,25]
        assert console.active == credentials and not console.failed
        assert [entry['event'] for entry in output] == ['heartbeat_deferred','heartbeat_deferred']
        assert [name for name, _ in session.calls] == ['tasks_work_start'] + ['tasks_work_heartbeat'] * 3
        assert all(args == credentials for _, args in session.calls[1:])
    asyncio.run(run())


def test_owner_outage_never_revives_a_lease_the_returning_server_rejects(monkeypatch):
    """Repeated unavailable responses do not extend the lease; server expiry ends retries without work/start."""
    import ag_tasks.agent_console as module
    async def run():
        class Expiring(Session):
            """Return an authoritative expired-session rejection after three transient outages."""
            async def call_tool(self, name, args, **kwargs):
                if name != 'tasks_work_heartbeat':
                    return await super().call_tool(name, args, **kwargs)
                self.calls.append((name, args.copy()))
                count = sum(call[0] == name for call in self.calls)
                code = 'tasks_unavailable' if count <= 3 else 'work_session_unavailable'
                return SimpleNamespace(isError=False, content=[SimpleNamespace(type='text', text=json.dumps({'error':{'code':code}}))])
        session, output = Expiring(), []
        console = Console(session, ['tasks_work_start','tasks_work_heartbeat'], output=output.append)
        await console.invoke('tasks_work_start', {'issue_id':'issue','agent_id':'a','session_key':'key'})
        ticks = 0
        async def tick(delay):
            """Allow a fifth schedule tick to prove the closed lease receives no more heartbeat."""
            nonlocal ticks
            assert delay == 25
            ticks += 1
            if ticks > 5:
                raise asyncio.CancelledError()
        monkeypatch.setattr(module.asyncio, 'sleep', tick)
        try:
            await console.maintain()
        except asyncio.CancelledError:
            pass
        assert console.active is None and console.failed
        assert [entry['event'] for entry in output] == ['heartbeat_deferred'] * 3 + ['lease_lost']
        assert [name for name, _ in session.calls] == ['tasks_work_start'] + ['tasks_work_heartbeat'] * 4
    asyncio.run(run())
