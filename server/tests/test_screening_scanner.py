import asyncio,io,struct
import pytest

from server.app.screening.scanner import ClamAvScanner,ScanResult

def test_clamav_instream_clean_infected_and_unavailable_without_metadata():
    async def scenario():
        received=[]
        async def serve(reader,writer):
            assert await reader.readexactly(10)==b"zINSTREAM\0"; body=b""
            while size:=struct.unpack("!I",await reader.readexactly(4))[0]: body+=await reader.readexactly(size)
            received.append(body); writer.write(b"stream: OK\0" if body==b"clean" else b"stream: Eicar-Test-Signature FOUND\0"); await writer.drain(); writer.close()
        server=await asyncio.start_server(serve,"127.0.0.1",0); port=server.sockets[0].getsockname()[1]
        scanner=ClamAvScanner("127.0.0.1",port,connect_timeout=1,read_timeout=1,total_timeout=2)
        assert await scanner.scan(io.BytesIO(b"clean"),10)==ScanResult.CLEAN
        assert await scanner.scan(io.BytesIO(b"virus"),10)==ScanResult.INFECTED
        server.close(); await server.wait_closed(); assert received==[b"clean",b"virus"]
        assert await scanner.scan(io.BytesIO(b"clean"),10)==ScanResult.UNAVAILABLE
    asyncio.run(scenario())
