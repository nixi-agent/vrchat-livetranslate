"""Execute the shipped AudioWorklet, including overflow recovery, in Node's VM."""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.chatgpt_browser_page import PAGE


class WorkletTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js required for actual worklet execution')
    def test_consumption_ack_and_overflow_reset(self):
        script = r'''
const assert = require('node:assert/strict'), vm = require('node:vm');
const worklet = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const types = {};
class Processor {constructor(){this.messages=[];this.port={postMessage:m=>this.messages.push(m)};}}
vm.runInNewContext(worklet, {AudioWorkletProcessor:Processor, sampleRate:24000,
  registerProcessor:(name,type)=>types[name]=type});
const feed = (p,n=1600)=>p.port.onmessage({data:new Int16Array(n).fill(10).buffer});
const p = new types['pcm-input']();
feed(p); p.process([], [[new Float32Array(128)]]);
for(let i=0;i<10;i++)feed(p);
assert.equal(p.offset,0,'Overflow must reset the old block cursor');
assert.equal(p.phase,0); assert.equal(p.previous,0); assert.equal(p.current,0);
assert(p.messages.some(m=>m.type==='overflow'));
feed(p);
for(let i=0;i<1600;i++)assert(Number.isFinite(p.next()));
assert(p.messages.some(m=>m.type==='consumed' && m.bytes===3200));
assert.equal(p.count,0); assert.equal(p.blocks.length,0);
console.log('Actual worklet reset and consumption acknowledgments PASS');
'''
        source = PAGE.split('const worklet = `', 1)[1].split('`;', 1)[0]
        result = subprocess.run([shutil.which('node'), '-e', script],
                                input=json.dumps(source), text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
