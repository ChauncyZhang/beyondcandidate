import io,json,socket,sys
from dataclasses import asdict
def _network_disabled(*args,**kwargs): raise PermissionError("network disabled")
socket.socket=_network_disabled
from server.app.screening.parsers import ParserError,ParserLimits,parse_document

def main():
    try:
        header=json.loads(sys.stdin.buffer.readline(8192)); limits=ParserLimits(**header["limits"]); data=sys.stdin.buffer.read(limits.max_source_bytes+1)
        result=parse_document(io.BytesIO(data),extension=header["extension"],mime_type=header["mime_type"],limits=limits); output={"ok":True,**asdict(result)}
    except ParserError as error: output={"ok":False,"safe_code":error.safe_code}
    except Exception: output={"ok":False,"safe_code":"parser_failed"}
    sys.stdout.write(json.dumps(output,separators=(",",":"),ensure_ascii=False))
if __name__=="__main__": main()
