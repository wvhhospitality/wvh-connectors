import asyncio, json, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
tool=sys.argv[1]; args=json.loads(sys.argv[2]) if len(sys.argv)>2 else {}
async def main():
    async with stdio_client(StdioServerParameters(command='.venv/bin/wvh-slack-mcp',args=[])) as (r,w):
        async with ClientSession(r,w) as s:
            await s.initialize()
            if tool=='__schema__':
                t=await s.list_tools()
                for x in t.tools: print(x.name, json.dumps(x.inputSchema))
                return
            res=await s.call_tool(tool,args)
            for c in res.content: print(getattr(c,'text',''))
asyncio.run(main())
