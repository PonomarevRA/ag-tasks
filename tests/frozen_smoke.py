"""Exercise the actual frozen HTTP/MCP executable using isolated state, never user tasks."""

import asyncio
from pathlib import Path
import json
import os

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    """Verify useful tool data, missing merge capability and clean stdio protocol shutdown."""
    root=Path(__file__).resolve().parents[1]
    directory=Path(os.environ.get('AG_TASKS_SMOKE_DATA_DIR', '/tmp/ag-tasks-frozen-qa'))
    binary=root/'dist/Ag Tasks.app/Contents/MacOS/AgTasks'
    with httpx.Client(base_url='http://127.0.0.1:8897',trust_env=False,follow_redirects=False,timeout=10) as client:
        status=client.get('/api/v1/status').json()
        assert status['application']=='ag-tasks'
        assert client.get('/').status_code==200
    stderr=root/'evidence/frozen-mcp-stderr.log'
    with stderr.open('w') as errors:
        parameters=StdioServerParameters(command=str(binary),args=['--mcp','--data-dir',str(directory)],env=dict(os.environ))
        async with stdio_client(parameters,errlog=errors) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools=await session.list_tools()
                names=[tool.name for tool in tools.tools]
                assert len(names)==26 and all('merge' not in name and 'approv' not in name for name in names)
                created=await session.call_tool('tasks_create',{'title':'Frozen protocol QA','external_refs':[{'url':'http://127.0.0.1:8890/?incidentId='+'a'*32,'title':'QA incident','kind':'incident'}]})
                assert not created.isError
                records=await session.call_tool('tasks_list',{})
                assert not records.isError
                payload=records.structuredContent or json.loads(records.content[0].text)
                if 'result' in payload: payload=payload['result']
                assert payload['items'] and payload['items'][0]['title']=='Frozen protocol QA'
    assert stderr.stat().st_size==0,stderr.read_text()
    result={'application':status['application'],'tools':names,'createdAndListed':True,'stderrBytes':0,'state':'isolated temporary QA only'}
    (root/'evidence/frozen-protocol.json').write_text(json.dumps(result,indent=2))
    print(json.dumps({'tools':len(names),'createdAndListed':True,'stderrBytes':0}))


if __name__=='__main__':
    asyncio.run(main())
