import asyncio,io,os
import pytest
from server.app.screening.scanner import ClamAvScanner,ScanResult

pytestmark=pytest.mark.skipif(not os.getenv("CLAMAV_SMOKE_HOST"),reason="ClamAV smoke host not configured")
def test_live_clamav_instream_clean_and_eicar():
    scanner=ClamAvScanner(os.environ["CLAMAV_SMOKE_HOST"],int(os.getenv("CLAMAV_SMOKE_PORT","3310")),connect_timeout=5,read_timeout=15,total_timeout=20)
    assert asyncio.run(scanner.scan(io.BytesIO(b"harmless screening document"),1024))==ScanResult.CLEAN
    eicar=b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    assert asyncio.run(scanner.scan(io.BytesIO(eicar),1024))==ScanResult.INFECTED
