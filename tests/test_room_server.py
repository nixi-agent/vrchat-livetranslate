"""Run actual Room handlers, not a rewritten relay, with no public connections."""
import shutil
import unittest
from pathlib import Path

from _node_script import run_node


class ServerTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js required for actual server handler execution')
    def test_utf8_limit_and_all_frame_rate_limit(self):
        script = r'''
import assert from 'node:assert/strict';
const {Room} = await import(JSON.parse(process.argv[1]));
globalThis.WebSocket={OPEN:1};
Date.now=()=>100000;
const make=()=>{
  const sent=[];let att={id:'peer',nick:'test',win:100000,n:0};
  const ws={readyState:WebSocket.OPEN,send:raw=>sent.push(JSON.parse(raw))};
  const state={id:{name:'ABCD1234'},getWebSockets:async()=>[ws],
    deserializeAttachment:()=>att,serializeAttachment:(_,a)=>{att=a;}};
  return {room:new Room(state,{}),ws,sent};
};
for(const binary of [false,true]){
  const c=make();
  await c.room.webSocketMessage(c.ws,JSON.stringify({t:'ping',pad:'x'.repeat(5000)}));
  assert.equal(c.sent.pop().t,'pong');
  let raw=JSON.stringify({t:'ping',pad:'測'.repeat(5000)});
  if(binary)raw=new TextEncoder().encode(raw).buffer;
  await c.room.webSocketMessage(c.ws,raw);
  assert.equal(c.sent.pop().code,'bad_frame','UTF-8 oversize must be rejected');
}
for(const type of ['ping','hello','unknown']){
  const c=make();
  for(let i=0;i<50;i++)await c.room.webSocketMessage(c.ws,JSON.stringify({t:type,room:'ABCD1234'}));
  assert.equal(c.sent.filter(x=>x.code==='rate').length,30,`${type} must count toward inbound rate`);
}
{
  const c=make();
  const prefix=new TextEncoder().encode('{"t":"ping","pad":"');
  const suffix=new TextEncoder().encode('"}');
  const invalid=new Uint8Array([...prefix,0xff,...suffix]);
  await c.room.webSocketMessage(c.ws,invalid.buffer);
  assert.equal(c.sent.pop().code,'bad_frame','Invalid UTF-8 must not become replacement text');
  await c.room.webSocketMessage(c.ws,new TextEncoder().encode('{"t":"ping","pad":"測試日本語"}').buffer);
  assert.equal(c.sent.pop().t,'pong','Valid UTF-8 must keep working after rejection');
}
console.log('Actual Room UTF-8 and total inbound limits PASS');
'''
        uri = (Path(__file__).resolve().parents[1]/'server/src/room.js').as_uri()
        result = run_node(script, uri)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
