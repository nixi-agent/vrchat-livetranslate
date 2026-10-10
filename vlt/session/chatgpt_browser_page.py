"""私有背景 WebRTC 頁面：僅處理 PCM，沒有登入憑證或麥克風權限。"""

PAGE = r'''<!doctype html><meta charset="utf-8"><title>VLT audio bridge</title>
<script>
(async () => {
  let peer, context, socket, input, sink;
  const fail = reason => { if(socket?.readyState === 1) socket.send(JSON.stringify({type:'error',
    reason:['input-overflow','output-backpressure'].includes(reason)?reason:'audio'})); };
  try {
    context = new AudioContext({sampleRate:24000}); await context.resume();
    if(context.sampleRate !== 24000 || context.state !== 'running') throw Error('Audio context unavailable');
    const worklet = `
    class PCMInput extends AudioWorkletProcessor {
      constructor(){super();this.blocks=[];this.offset=0;this.phase=0;this.previous=0;this.current=0;this.count=0;
        this.port.onmessage=e=>{const samples=new Int16Array(e.data);this.blocks.push(samples);this.count+=samples.length;
          if(this.count>16000){this.port.postMessage({type:'overflow'});this.blocks=[];this.count=0;
            this.offset=this.phase=this.previous=this.current=0;}};}
      next(){if(!this.blocks.length)return 0;const value=this.blocks[0][this.offset++]/32768;this.count--;
        if(this.offset===this.blocks[0].length){const block=this.blocks.shift();this.offset=0;
          this.port.postMessage({type:'consumed',bytes:block.byteLength});}return value;}
      process(inputs,outputs){const out=outputs[0][0];for(let i=0;i<out.length;i++){
        out[i]=this.previous*(1-this.phase)+this.current*this.phase;this.phase+=16000/sampleRate;
        if(this.phase>=1){this.phase-=1;this.previous=this.current;this.current=this.next();}}return true;}
    }
    class PCMOutput extends AudioWorkletProcessor {
      constructor(){super();this.buffer=new Int16Array(480);this.offset=0;}
      process(inputs,outputs){const channels=inputs[0];if(!channels?.length)return true;
        for(let i=0;i<channels[0].length;i++){let value=0;for(const channel of channels)value+=channel[i];value/=channels.length;
          this.buffer[this.offset++]=Math.round(Math.max(-1,Math.min(1,value))*32767);
          if(this.offset===480){const buffer=this.buffer.buffer;this.port.postMessage(buffer,[buffer]);this.buffer=new Int16Array(480);this.offset=0;}}
        return true;}
    }
    registerProcessor('pcm-input',PCMInput);registerProcessor('pcm-output',PCMOutput);`;
    const blobUrl=URL.createObjectURL(new Blob([worklet],{type:'text/javascript'}));
    await context.audioWorklet.addModule(blobUrl); URL.revokeObjectURL(blobUrl);
    const destination=context.createMediaStreamDestination();
    input=new AudioWorkletNode(context,'pcm-input'); input.connect(destination);
    input.port.onmessage=e=>{if(e.data.type==='consumed' && socket?.readyState===1)socket.send(JSON.stringify(e.data));else fail('input-overflow');};
    peer=new RTCPeerConnection({bundlePolicy:'max-bundle'});
    peer.addTrack(destination.stream.getAudioTracks()[0],destination.stream);
    peer.addTransceiver('video',{direction:'sendonly'}); peer.createDataChannel('',{negotiated:true,id:0});
    socket=new WebSocket('ws://'+location.host+location.pathname+'/ws'); socket.binaryType='arraybuffer';
    peer.ontrack=event=>{
      if(event.track.kind!=='audio')return;
      const stream=new MediaStream([event.track]); sink=document.createElement('audio');sink.srcObject=stream;sink.volume=0;sink.play().catch(fail);
      const source=context.createMediaStreamSource(stream), capture=new AudioWorkletNode(context,'pcm-output'), mute=context.createGain();mute.gain.value=0;
      source.connect(capture);capture.connect(mute);mute.connect(context.destination);
      capture.port.onmessage=e=>{if(socket.readyState!==1)return;if(socket.bufferedAmount>96000){fail('output-backpressure');return;}socket.send(e.data);};
    };
    peer.onconnectionstatechange=()=>{
      if(peer.connectionState==='connected')socket.send(JSON.stringify({type:'ready'}));
      if(peer.connectionState==='failed')fail();
    };
    socket.onmessage=async event=>{
      try {
        if(event.data instanceof ArrayBuffer){input.port.postMessage(event.data,[event.data]);return;}
        const message=JSON.parse(event.data);
        if(message.type==='answer')await peer.setRemoteDescription({type:'answer',sdp:message.sdp});
        if(message.type==='close'){sink?.pause();peer.close();await context.close();socket.close();window.close();}
      }catch{fail();}
    };
    socket.onclose=()=>{sink?.pause();peer.close();context.close();};
    socket.onopen=async()=>{
      try {
        await peer.setLocalDescription(await peer.createOffer());
        await new Promise(resolve=>{if(peer.iceGatheringState==='complete')resolve();else{const timer=setTimeout(resolve,2500);
          peer.onicegatheringstatechange=()=>{if(peer.iceGatheringState==='complete'){clearTimeout(timer);resolve();}};}});
        socket.send(JSON.stringify({type:'offer',sdp:peer.localDescription.sdp}));
      }catch{fail();}
    };
  }catch{fail();}
})();
</script>'''
