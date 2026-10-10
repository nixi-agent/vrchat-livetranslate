"""Actual Worker authentication accepts UTF-8 tokens and rejects malformed data."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path


class WorkerAuthTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js required for Worker authentication execution')
    def test_headers_legacy_queries_and_unicode_tokens(self):
        script = r'''
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
if(!globalThis.crypto)globalThis.crypto=webcrypto;
const {default:worker}=await import(JSON.parse(process.argv[1]));
const status=await worker.fetch(new Request('https://example.invalid/'),{});
assert(!(await status.text()).includes('&k='),'Status must not recommend query credentials');
for(const token of ['synthetic-token','合成令牌 テスト']){
  const hash=Buffer.from(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(token))).toString('hex');
  const env={ROOM_TOKEN_HASH:hash,ROOM:{idFromName:s=>s,get:()=>({fetch:()=>new Response('accepted')})}};
  const base='https://example.invalid/ws?room=ABCD1234';
  const encoded=Buffer.from(token).toString('base64url');
  for(const [url,auth] of [[base,'VLT '+encoded],[base+'&k='+encodeURIComponent(token),null]]){
    const headers={Upgrade:'websocket'};if(auth)headers.Authorization=auth;
    assert.equal((await worker.fetch(new Request(url,{headers}),env)).status,200);
  }
  if(token==='synthetic-token'){
    assert.equal((await worker.fetch(new Request(base,{headers:{Upgrade:'websocket',Authorization:'Bearer '+token}}),env)).status,200);
  }
  for(const invalid of ['VLT !!!','VLT /w==','Bearer incorrect']){
    const response=await worker.fetch(new Request(base,{headers:{Upgrade:'websocket',Authorization:invalid}}),env);
    assert([401,403].includes(response.status));
    const body=await response.text();
    assert(!body.includes('?k='),'Auth errors must not recommend query credentials');
  }
}
console.log('Actual Worker token auth PASS');
'''
        uri = (Path(__file__).resolve().parents[1]/'server/src/index.js').as_uri()
        result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script, json.dumps(uri)],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
