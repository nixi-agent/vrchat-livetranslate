"""Execute the actual production page in Node; no real browser or remote service."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]
from _node_script import run_node
from vlt.session.chatgpt_reserved_bridge import reserved_page


class PageContract(unittest.TestCase):
    def test_actual_page_holds_rtp_until_answer_and_marks_every_pcm_byte(self):
        script = r'''
import assert from 'node:assert/strict'; import vm from 'node:vm';
const source=JSON.parse(process.argv[1]), sockets=[], peers=[], nodes=[];
const stream={getAudioTracks:()=>[{}]};
class Context {
  constructor(){this.sampleRate=24000;this.state='running';this.destination={};
    this.audioWorklet={addModule:async()=>{}};}
  async resume(){} async suspend(){} async close(){}
  createMediaStreamDestination(){return {stream};}
  createMediaStreamSource(){return {connect(){}};}
  createGain(){return {gain:{value:0},connect(){}};}
}
class Worklet {
  constructor(ctx,name){this.name=name;this.port={};nodes.push(this);}
  connect(){}
}
class Peer {
  constructor(){this.descriptions=[];this.iceGatheringState='complete';peers.push(this);}
  addTrack(){} addTransceiver(){} createDataChannel(){return {};}
  async createOffer(){return {sdp:'v=0\r\noffer'};}
  async setLocalDescription(value){this.localDescription=value;}
  async setRemoteDescription(value){this.descriptions.push(value);}
  close(){}
}
class Socket {
  constructor(){this.readyState=1;this.bufferedAmount=0;this.sent=[];sockets.push(this);}
  send(value){this.sent.push(value);if(value instanceof ArrayBuffer)this.bufferedAmount+=value.byteLength;} close(){}
}
vm.runInNewContext(source.split('<script>')[1].split('</script>')[0],{
  AudioContext:Context,AudioWorkletNode:Worklet,RTCPeerConnection:Peer,WebSocket:Socket,
  Blob:class {},URL:{createObjectURL:()=>'',revokeObjectURL(){}},
  location:{host:'127.0.0.1',pathname:'/offline'},
  document:{createElement:()=>({volume:0,play:async()=>{},pause(){}})},
  MediaStream:class {},TextEncoder,ArrayBuffer,Uint8Array,setTimeout,clearTimeout,
  window:{close(){}}});
await new Promise(resolve=>setImmediate(resolve));
const socket=sockets[0], peer=peers[0], nonce='1'.repeat(32);
await socket.onopen();
assert.equal(JSON.parse(socket.sent.at(-1)).type,'prepared');
assert.equal(peer.descriptions.length,0);
await socket.onmessage({data:JSON.stringify({type:'activate',nonce:'2'.repeat(32)})});
assert.equal(JSON.parse(socket.sent.at(-1)).type,'offer');
assert.equal(peer.descriptions.length,0,'no RTP answer applied while reservation is held');
await socket.onmessage({data:JSON.stringify({type:'answer',sdp:'v=0\r\nanswer',nonce})});
assert.equal(peer.descriptions.length,1);
assert.deepEqual(JSON.parse(socket.sent.at(-1)),{type:'generation',nonce});
peer.connectionState='connected';peer.onconnectionstatechange();
assert.equal(JSON.parse(socket.sent.at(-1)).nonce,nonce);
peer.ontrack({track:{kind:'audio'}});
const capture=nodes.find(node=>node.name==='pcm-output');
const pcm=new Uint8Array([1,0,2,0,3,0,4,0]);
await capture.port.onmessage({data:pcm.buffer});
const framed=new Uint8Array(socket.sent.at(-1));
assert.deepEqual(framed,pcm,'all PCM bytes preserved without per-packet overhead');
socket.sent=[];socket.bufferedAmount=0;
for(let i=0;i<98;i++)await capture.port.onmessage({data:new ArrayBuffer(960)});
assert.equal(socket.sent.filter(value=>value instanceof ArrayBuffer).length,98,
  'generation metadata must not reduce the original 96000-byte PCM backpressure allowance');
await socket.onmessage({data:JSON.stringify({type:'answer',sdp:'v=0\r\nduplicate',nonce})});
assert.equal(peer.descriptions.length,1,'duplicate answer must not start another peer');
assert.equal(JSON.parse(socket.sent.at(-1)).type,'error');
console.log('Actual PAGE deferred RTP / ready nonce / exact PCM / duplicate rejection PASS');
'''
        result = run_node(script, reserved_page())
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
