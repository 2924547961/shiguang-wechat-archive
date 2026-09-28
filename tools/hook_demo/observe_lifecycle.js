// Metadata-only probes. No message payloads, typed text, or buffer changes.
const lifecycleHooks = [];
let lifecycleEvents = 0;
function emitLifecycle(event) {
  if (++lifecycleEvents <= 10000)
    send({wall_ms: Date.now(), thread: Process.getCurrentThreadId(), ...event});
}
function attachExport(moduleName, name, callbacks) {
  const address = Process.findModuleByName(moduleName)?.findExportByName(name);
  if (!address) return;
  Interceptor.attach(address, callbacks);
  lifecycleHooks.push(name);
}
attachExport('user32.dll', 'DispatchMessageW', {
  onEnter(args) {
    this.tracked = false;
    try {
      const p = args[0], id = p.add(Process.pointerSize).readU32();
      const wp = p.add(Process.pointerSize === 8 ? 16 : 8).readPointer();
      if (id !== 0x201 && id !== 0x202 && !((id === 0x100 || id === 0x101) && wp.toUInt32() === 13)) return;
      this.tracked = true; this.id = id; this.started = Date.now();
      emitLifecycle({event:'ui_dispatch_enter', message: id, hwnd:p.readPointer().toString(),
        kind: id === 0x201 ? 'left_button_down' : id === 0x202 ? 'left_button_up' : id === 0x100 ? 'enter_down' : 'enter_up',
        stack: stack(this.context)});
    } catch(e) { emitLifecycle({event:'probe_error',api:'DispatchMessageW',error:String(e)}); }
  },
  onLeave() {
    if (this.tracked) emitLifecycle({event:'ui_dispatch_return',message:this.id,elapsed_ms:Date.now()-this.started});
  }
});
attachExport('ws2_32.dll', 'recv', {
  onEnter(args) { this.socket = args[0].toString(); this.started=Date.now(); },
  onLeave(result) {
    if (result.toInt32() > 0) emitLifecycle({event:'socket_receive',api:'recv',socket:this.socket,
      bytes:result.toInt32(),elapsed_ms:Date.now()-this.started,stack:stack(this.context)});
  }
});
attachExport('ws2_32.dll', 'WSARecv', {
  onEnter(args) { this.socket=args[0].toString(); this.received=args[3]; this.overlapped=!args[5].isNull(); },
  onLeave(result) {
    const status=result.toInt32();
    if (status === 0) {
      try { emitLifecycle({event:'socket_receive',api:'WSARecv',socket:this.socket,
        bytes:this.received.isNull()?null:this.received.readU32(),stack:stack(this.context)}); } catch (_) {}
    } else if(this.overlapped) {
      emitLifecycle({event:'receive_async_unresolved',api:'WSARecv',socket:this.socket,
        return_value:status,note:'Completion and error code not captured; do not infer receipt.'});
    }
  }
});
const kernel = Process.findModuleByName('kernel32.dll');
const pathExport=kernel?.findExportByName('GetFinalPathNameByHandleW');
if (pathExport) {
  const pathOf=new NativeFunction(pathExport,'uint32',['pointer','pointer','uint32','uint32']);
  attachExport('kernel32.dll','WriteFile',{
    onEnter(args) {
      this.tracked=false;
      try {
        const buffer=Memory.alloc(4096); const n=pathOf(args[0],buffer,2048,0);
        if (!n || n>=2048) return;
        const path=buffer.readUtf16String(n);
        if (!/db_storage[\\/].*message[^\\/]*\.db(?:-wal|-shm)?$/i.test(path)) return;
        this.tracked=true;this.file=path.split(/[\\/]/).pop();this.started=Date.now();
        emitLifecycle({event:'db_file_write_enter',api:'WriteFile',file:this.file,
          requested_bytes:args[2].toUInt32(),overlapped:!args[4].isNull(),stack:stack(this.context)});
      } catch (_) {}
    },
    onLeave(result) { if(this.tracked)emitLifecycle({event:'db_file_write_return',file:this.file,
      return_value:result.toInt32(),elapsed_ms:Date.now()-this.started}); }
  });
}
send({event:'lifecycle_probe_ready',apis:lifecycleHooks,limitations:[
  'UI events are candidates, not identified Send controls.',
  'No internal business, serialization, encryption or queue functions are identified.',
  'Async receive completion, memory-mapped writes and other API paths may not be observed.'
]});
