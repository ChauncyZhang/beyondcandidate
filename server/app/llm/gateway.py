import asyncio,http.client,json,socket,ssl,time
from dataclasses import dataclass
from typing import Protocol
from pydantic import BaseModel,ConfigDict,Field,ValidationError
from server.app.llm.policy import ProviderAllowlist,ProviderPolicyError,ProviderSpec
from server.app.llm.resume_profile import RESUME_PROFILE_SYSTEM_PROMPT,ResumeProfileEvaluation,ResumeProfileRequest,validate_resume_profile_content
from server.app.llm.screening import DIMENSION_LIMITS,ScreeningEvaluation,ScreeningRequest,ScreeningResult

FIXED_PROBE_SYSTEM="Return the requested health-check JSON only."
FIXED_PROBE_USER='Return {"status":"ok"}. This is a configuration test with no recruiting data.'
FIXED_PROBE_MAX_TOKENS=256
EVALUATION_MAX_TOKENS=8192
THINKING_DISABLED={"type":"disabled"}


def _normalize_result_list(value):
    if isinstance(value,str):
        value=value.strip()
        return [value] if value else []
    return value


def _validate_screening_result(content:str)->ScreeningResult:
    document=json.loads(content)
    if not isinstance(document,dict): raise ValueError
    dimensions=document.get("dimensions")
    if isinstance(dimensions,dict):
        document["dimensions"]=[dimensions[key] for key in DIMENSION_LIMITS if key in dimensions]
        document["dimensions"].extend(value for key,value in dimensions.items() if key not in DIMENSION_LIMITS)
    if isinstance(document.get("dimensions"),list):
        for dimension in document["dimensions"]:
            if not isinstance(dimension,dict): continue
            for field in ("evidence","gaps"):
                if field in dimension: dimension[field]=_normalize_result_list(dimension[field])
    for field in ("strengths","gaps","risks","questions"):
        if field in document: document[field]=_normalize_result_list(document[field])
    return ScreeningResult.model_validate(document)


class GatewayError(RuntimeError):
    def __init__(self,safe_code:str): self.safe_code=safe_code; super().__init__(safe_code)

@dataclass(frozen=True)
class TransportResponse: status_code:int; body:bytes
class GatewayTransport(Protocol):
    def post(self,spec:ProviderSpec,address:str,path:str,headers:dict[str,str],body:bytes,max_response_bytes:int)->TransportResponse: ...

class _Usage(BaseModel):
    # OpenAI-compatible providers may add token-detail objects. Keep the
    # portable counters strict while ignoring provider-specific metadata.
    model_config=ConfigDict(extra="ignore",strict=True)
    prompt_tokens:int=Field(default=0,ge=0,le=10_000_000)
    completion_tokens:int=Field(default=0,ge=0,le=10_000_000)
    total_tokens:int=Field(default=0,ge=0,le=10_000_000)

class PinnedHttpsTransport:
    def __init__(self,*,connect_timeout:float=3,read_timeout:float=60): self.connect_timeout=connect_timeout; self.read_timeout=read_timeout
    def post(self,spec,address,path,headers,body,max_response_bytes):
        raw=socket.create_connection((address,spec.port),timeout=self.connect_timeout); connection=raw
        try:
            if spec.scheme=="https": connection=ssl.create_default_context().wrap_socket(raw,server_hostname=spec.host)
            connection.settimeout(self.read_timeout)
            lines=[f"POST {path} HTTP/1.1",f"Host: {spec.host}","Connection: close",f"Content-Length: {len(body)}"]+[f"{key}: {value}" for key,value in headers.items()]
            connection.sendall(("\r\n".join(lines)+"\r\n\r\n").encode()+body); response=http.client.HTTPResponse(connection); response.begin(); payload=response.read(max_response_bytes+1)
            if len(payload)>max_response_bytes: raise GatewayError("provider_response_too_large")
            return TransportResponse(response.status,payload)
        finally:
            connection.close()

class OpenAiCompatibleGateway:
    def __init__(self,allowlist:ProviderAllowlist,transport:GatewayTransport|None=None,*,total_timeout:float=15,evaluation_total_timeout:float=75,max_response_bytes:int=64*1024,max_concurrency:int=4):
        if max_concurrency<1: raise ValueError("max_concurrency must be positive")
        if total_timeout<=0 or evaluation_total_timeout<=0: raise ValueError("timeouts must be positive")
        self.allowlist=allowlist; self.transport=transport or PinnedHttpsTransport(); self.total_timeout=total_timeout; self.evaluation_total_timeout=evaluation_total_timeout; self.max_response_bytes=max_response_bytes; self._semaphore=asyncio.Semaphore(max_concurrency)
    async def test_connection(self,provider_id:str,model:str,api_key:str,*,organization_id=None)->int:
        started=time.monotonic()
        try:
            spec=self.allowlist.require(provider_id,model,organization_id=organization_id); addresses=self.allowlist.resolve_public(spec); address=addresses[0]
            payload=json.dumps({"model":model,"messages":[{"role":"system","content":FIXED_PROBE_SYSTEM},{"role":"user","content":FIXED_PROBE_USER}],"temperature":0,"max_tokens":FIXED_PROBE_MAX_TOKENS,"thinking":THINKING_DISABLED},separators=(",",":"),ensure_ascii=True).encode()
            path=(spec.base_path or "")+"/chat/completions"; headers={"Authorization":f"Bearer {api_key}","Content-Type":"application/json","Accept":"application/json"}
            async def send():
                async with self._semaphore:
                    return await asyncio.to_thread(self.transport.post,spec,address,path,headers,payload,self.max_response_bytes)
            response=await asyncio.wait_for(send(),self.total_timeout)
        except GatewayError: raise
        except ProviderPolicyError as error: raise GatewayError(str(error)) from None
        except (TimeoutError,OSError,ssl.SSLError): raise GatewayError("provider_unavailable") from None
        except Exception: raise GatewayError("provider_unavailable") from None
        if len(response.body)>self.max_response_bytes: raise GatewayError("provider_response_too_large")
        if response.status_code in {401,403}: raise GatewayError("provider_auth_failed")
        if response.status_code in {400,422}: raise GatewayError("provider_request_rejected")
        if response.status_code==404: raise GatewayError("provider_model_not_found")
        if response.status_code==429: raise GatewayError("provider_quota_or_rate_limited")
        if 300<=response.status_code<400: raise GatewayError("provider_redirect_rejected")
        if response.status_code<200 or response.status_code>=300: raise GatewayError("provider_unavailable")
        try:
            document=json.loads(response.body); content=document["choices"][0]["message"]["content"]
            if not isinstance(content,str) or len(content)>1000 or json.loads(content)!={"status":"ok"}: raise ValueError
        except (ValueError,KeyError,IndexError,TypeError,json.JSONDecodeError): raise GatewayError("provider_response_invalid") from None
        return max(0,int((time.monotonic()-started)*1000))

    async def evaluate(self,provider_id:str,model:str,api_key:str,request:ScreeningRequest,*,organization_id=None,system_prompt:str)->ScreeningEvaluation:
        if not isinstance(request,ScreeningRequest): raise GatewayError("screening_request_invalid")
        if not isinstance(system_prompt,str) or not system_prompt.strip(): raise GatewayError("screening_prompt_invalid")
        started=time.monotonic()
        try:
            spec=self.allowlist.require(provider_id,model,organization_id=organization_id); address=self.allowlist.resolve_public(spec)[0]
            payload_document={"model":model,"messages":[{"role":"system","content":system_prompt},{"role":"user","content":request.provider_content()}],"temperature":0,"max_tokens":EVALUATION_MAX_TOKENS,"response_format":{"type":"json_object"},"thinking":THINKING_DISABLED}
            payload=json.dumps(payload_document,separators=(",",":"),ensure_ascii=False).encode()
            path=(spec.base_path or "")+"/chat/completions"; headers={"Authorization":f"Bearer {api_key}","Content-Type":"application/json","Accept":"application/json"}
            async def send():
                async with self._semaphore:
                    return await asyncio.to_thread(self.transport.post,spec,address,path,headers,payload,self.max_response_bytes)
            response=await asyncio.wait_for(send(),self.evaluation_total_timeout)
        except GatewayError: raise
        except ProviderPolicyError as error: raise GatewayError(str(error)) from None
        except (TimeoutError,OSError,ssl.SSLError): raise GatewayError("provider_unavailable") from None
        except Exception: raise GatewayError("provider_unavailable") from None
        if len(response.body)>self.max_response_bytes: raise GatewayError("provider_response_too_large")
        if response.status_code in {401,403}: raise GatewayError("provider_auth_failed")
        if response.status_code in {400,422}: raise GatewayError("provider_request_rejected")
        if response.status_code==404: raise GatewayError("provider_model_not_found")
        if response.status_code==429: raise GatewayError("provider_quota_or_rate_limited")
        if 300<=response.status_code<400: raise GatewayError("provider_redirect_rejected")
        if response.status_code<200 or response.status_code>=300: raise GatewayError("provider_unavailable")
        try:
            document=json.loads(response.body)
            content=document["choices"][0]["message"]["content"]
            if not isinstance(content,str): raise ValueError
            result=_validate_screening_result(content)
            usage=_Usage.model_validate(document.get("usage",{})).model_dump()
        except (ValueError,KeyError,IndexError,TypeError,json.JSONDecodeError,ValidationError):
            raise GatewayError("provider_response_invalid") from None
        return ScreeningEvaluation(result,max(0,int((time.monotonic()-started)*1000)),usage)

    async def extract_resume_profile(self,provider_id:str,model:str,api_key:str,request:ResumeProfileRequest,*,organization_id=None)->ResumeProfileEvaluation:
        if not isinstance(request,ResumeProfileRequest): raise GatewayError("resume_profile_request_invalid")
        started=time.monotonic()
        try:
            spec=self.allowlist.require(provider_id,model,organization_id=organization_id); address=self.allowlist.resolve_public(spec)[0]
            payload=json.dumps({"model":model,"messages":[{"role":"system","content":RESUME_PROFILE_SYSTEM_PROMPT},{"role":"user","content":request.provider_content()}],"temperature":0,"max_tokens":EVALUATION_MAX_TOKENS,"response_format":{"type":"json_object"},"thinking":THINKING_DISABLED},separators=(",",":"),ensure_ascii=False).encode()
            path=(spec.base_path or "")+"/chat/completions"; headers={"Authorization":f"Bearer {api_key}","Content-Type":"application/json","Accept":"application/json"}
            async def send():
                async with self._semaphore:
                    return await asyncio.to_thread(self.transport.post,spec,address,path,headers,payload,self.max_response_bytes)
            response=await asyncio.wait_for(send(),self.evaluation_total_timeout)
        except GatewayError: raise
        except ProviderPolicyError as error: raise GatewayError(str(error)) from None
        except (TimeoutError,OSError,ssl.SSLError): raise GatewayError("provider_unavailable") from None
        except Exception: raise GatewayError("provider_unavailable") from None
        if len(response.body)>self.max_response_bytes: raise GatewayError("provider_response_too_large")
        if response.status_code in {401,403}: raise GatewayError("provider_auth_failed")
        if response.status_code in {400,422}: raise GatewayError("provider_request_rejected")
        if response.status_code==404: raise GatewayError("provider_model_not_found")
        if response.status_code==429: raise GatewayError("provider_quota_or_rate_limited")
        if 300<=response.status_code<400: raise GatewayError("provider_redirect_rejected")
        if response.status_code<200 or response.status_code>=300: raise GatewayError("provider_unavailable")
        try:
            document=json.loads(response.body); content=document["choices"][0]["message"]["content"]
            if not isinstance(content,str): raise ValueError
            result=validate_resume_profile_content(content)
            usage=_Usage.model_validate(document.get("usage",{})).model_dump()
        except (ValueError,KeyError,IndexError,TypeError,json.JSONDecodeError,ValidationError):
            raise GatewayError("provider_response_invalid") from None
        return ResumeProfileEvaluation(result,max(0,int((time.monotonic()-started)*1000)),usage)
