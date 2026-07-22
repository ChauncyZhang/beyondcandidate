import asyncio,json,os,sys
from dataclasses import asdict
from server.app.queue.service import normalize_safe_code
from server.app.screening.parsers import ParsedDocument,ParserLimits

class IsolatedParserError(Exception):
    def __init__(self,safe_code): self.safe_code=normalize_safe_code(safe_code); super().__init__(self.safe_code)

class IsolatedParser:
    def __init__(self,timeout_seconds:float,worker_module="server.app.screening.parser_worker"): self.timeout_seconds=timeout_seconds; self.worker_module=worker_module; self.last_pid=None
    async def parse(self,stream,*,extension,mime_type,limits:ParserLimits):
        stream.seek(0); data=stream.read(limits.max_source_bytes+1)
        if len(data)>limits.max_source_bytes: raise IsolatedParserError("file_too_large")
        header=json.dumps({"extension":extension,"mime_type":mime_type,"limits":asdict(limits)},separators=(",",":")).encode()+b"\n"
        environment={key:os.environ[key] for key in ("PATH","PYTHONPATH") if key in os.environ}; environment.update({"PYTHONDONTWRITEBYTECODE":"1","PYTHONIOENCODING":"utf-8"})
        process=await asyncio.create_subprocess_exec(sys.executable,"-m",self.worker_module,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,env=environment); self.last_pid=process.pid
        try: stdout,_=await asyncio.wait_for(process.communicate(header+data),self.timeout_seconds)
        except TimeoutError: process.kill(); await process.wait(); raise IsolatedParserError("parser_timeout") from None
        except asyncio.CancelledError: process.kill(); await process.wait(); raise
        if process.returncode!=0 or len(stdout)>limits.max_text_chars*4+4096: raise IsolatedParserError("parser_failed")
        try: result=json.loads(stdout)
        except Exception: raise IsolatedParserError("parser_failed") from None
        if not result.get("ok"): raise IsolatedParserError(result.get("safe_code"))
        text=result.get("text")
        if not isinstance(text,str) or len(text)>limits.max_text_chars: raise IsolatedParserError("parser_failed")
        return ParsedDocument(text,result["parser_version"],result["quality"])
