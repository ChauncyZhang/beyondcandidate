import asyncio
from tempfile import SpooledTemporaryFile
from minio.commonconfig import CopySource
class StorageWriteFailed(Exception):
    def __init__(self,safe_code): self.safe_code=safe_code; super().__init__(safe_code)
class QuarantineStorage:
    """Keys are UUID-only quarantine prefixes, enabling deterministic orphan sweeps."""
    def __init__(self,client,bucket): self.client=client; self.bucket=bucket
    def write(self,stream,storage_key,content_type,max_bytes):
        spool=SpooledTemporaryFile(max_size=min(max_bytes,1024*1024),mode="w+b"); total=0
        try:
            stream.seek(0)
            while chunk:=stream.read(64*1024):
                total+=len(chunk)
                if total>max_bytes: raise StorageWriteFailed("file_too_large")
                spool.write(chunk)
            spool.seek(0); self.client.put_object(self.bucket,storage_key,spool,total,content_type=content_type)
        except StorageWriteFailed: raise
        except Exception: raise StorageWriteFailed("storage_write_failed") from None
        finally: spool.close()
    def delete(self,storage_key):
        try: self.client.remove_object(self.bucket,storage_key)
        except Exception: pass

class PipelineStorage:
    def __init__(self,client,bucket): self.client,self.bucket=client,bucket
    async def open(self,key,max_bytes): return await asyncio.to_thread(self._open,key,max_bytes)
    def _open(self,key,max_bytes):
        response=self.client.get_object(self.bucket,key); spool=SpooledTemporaryFile(max_size=min(max_bytes,1024*1024),mode="w+b"); total=0
        try:
            while chunk:=response.read(64*1024):
                total+=len(chunk)
                if total>max_bytes: raise StorageWriteFailed("file_too_large")
                spool.write(chunk)
            spool.seek(0); return spool
        except Exception: spool.close(); raise
        finally: response.close(); response.release_conn()
    async def copy(self,source,target,max_bytes):
        metadata=await asyncio.to_thread(self.client.stat_object,self.bucket,source)
        if metadata.size>max_bytes: raise StorageWriteFailed("file_too_large")
        await asyncio.to_thread(self.client.copy_object,self.bucket,target,CopySource(self.bucket,source))
    async def delete(self,key):
        try: await asyncio.to_thread(self.client.remove_object,self.bucket,key); return True
        except Exception: return False
