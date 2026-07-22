import asyncio,struct
from enum import Enum
from typing import BinaryIO,Protocol

class ScanResult(str,Enum): CLEAN="clean"; INFECTED="infected"; UNAVAILABLE="unavailable"; ERROR="error"
class MalwareScanner(Protocol):
    async def scan(self,stream:BinaryIO,max_bytes:int)->ScanResult: ...

class ClamAvScanner:
    def __init__(self,host:str,port:int,*,connect_timeout:float,read_timeout:float,total_timeout:float):
        self.host,self.port=host,port; self.connect_timeout=connect_timeout; self.read_timeout=read_timeout; self.total_timeout=total_timeout
    async def scan(self,stream:BinaryIO,max_bytes:int)->ScanResult:
        try: return await asyncio.wait_for(self._scan(stream,max_bytes),self.total_timeout)
        except (OSError,TimeoutError,asyncio.IncompleteReadError): return ScanResult.UNAVAILABLE
        except Exception: return ScanResult.ERROR
    async def _scan(self,stream,max_bytes):
        reader,writer=await asyncio.wait_for(asyncio.open_connection(self.host,self.port),self.connect_timeout); total=0
        try:
            writer.write(b"zINSTREAM\0"); stream.seek(0)
            while chunk:=stream.read(64*1024):
                total+=len(chunk)
                if total>max_bytes: return ScanResult.ERROR
                writer.write(struct.pack("!I",len(chunk))+chunk); await writer.drain()
            writer.write(struct.pack("!I",0)); await writer.drain(); response=await asyncio.wait_for(reader.readuntil(b"\0"),self.read_timeout)
            if response.endswith(b"OK\0"): return ScanResult.CLEAN
            if b"FOUND\0" in response: return ScanResult.INFECTED
            return ScanResult.ERROR
        finally: writer.close(); await writer.wait_closed()
