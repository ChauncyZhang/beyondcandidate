import asyncio,io,os,time
import pytest
from server.app.screening.isolated_parser import IsolatedParser,IsolatedParserError
from server.app.screening.parsers import ParserLimits

def test_isolated_parser_timeout_kills_child_and_closes_no_parent_stream():
    parser=IsolatedParser(timeout_seconds=.1,worker_module="server.tests.parser_hang_worker")
    started=time.monotonic()
    with pytest.raises(IsolatedParserError) as raised: asyncio.run(parser.parse(io.BytesIO(b"private"),extension=".txt",mime_type="text/plain",limits=ParserLimits()))
    assert raised.value.safe_code=="parser_timeout" and time.monotonic()-started<2
    with pytest.raises(ProcessLookupError): os.kill(parser.last_pid,0)

def test_isolated_parser_returns_bounded_result():
    result=asyncio.run(IsolatedParser(timeout_seconds=2).parse(io.BytesIO(b"Python"),extension=".txt",mime_type="text/plain",limits=ParserLimits()))
    assert result.text=="Python" and result.parser_version=="txt-v1"

def test_isolated_parser_cancellation_kills_child(tmp_path,monkeypatch):
    monkeypatch.setenv("TMPDIR",str(tmp_path)); parser=IsolatedParser(timeout_seconds=30,worker_module="server.tests.parser_hang_worker")
    async def scenario():
        task=asyncio.create_task(parser.parse(io.BytesIO(b"private"),extension=".txt",mime_type="text/plain",limits=ParserLimits())); await asyncio.sleep(.1); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(scenario())
    with pytest.raises(ProcessLookupError): os.kill(parser.last_pid,0)
    assert list(tmp_path.iterdir())==[]
