import asyncio,json,threading,time
import pytest
from server.app.llm.gateway import EVALUATION_MAX_TOKENS,FIXED_PROBE_MAX_TOKENS,FIXED_PROBE_SYSTEM,FIXED_PROBE_USER,GatewayError,OpenAiCompatibleGateway,TransportResponse
from server.app.llm.policy import ProviderAllowlist,ProviderPolicyError
from server.app.llm.security import ApiKeyCipher
from server.app.llm.screening import ScreeningRequest

KEY=b"QEFCQ0RFRkdISUpLTE1OT1BRUlNUVVZXWFlaW1xdXl8="
def resolver(address): return lambda host,port,type: [(2,1,6,"",(address,port))]
class Transport:
    def __init__(self,status=200,body=None): self.status=status; self.body=body or b'{"choices":[{"message":{"content":"{\\"status\\":\\"ok\\"}"}}]}'; self.calls=[]
    def post(self,spec,address,path,headers,body,max_response_bytes): self.calls.append((spec,address,path,headers,body,max_response_bytes)); return TransportResponse(self.status,self.body)

def test_api_key_cipher_is_randomized_redacted_and_wrong_key_fails():
    cipher=ApiKeyCipher(KEY); first=cipher.encrypt("sk-private"); second=cipher.encrypt("sk-private")
    assert first!=second and cipher.decrypt(first)=="sk-private" and "sk-private" not in repr(first)
    with pytest.raises(ValueError): ApiKeyCipher(b"ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8=").decrypt(first)

@pytest.mark.parametrize("address",["127.0.0.1","10.0.0.1","169.254.169.254","::1","fc00::1","fe80::1"])
def test_allowlist_rejects_private_and_metadata_addresses(address):
    policy=ProviderAllowlist({"provider":{"base_url":"https://provider.example/v1","models":["model"]}},resolver=resolver(address))
    with pytest.raises(ProviderPolicyError): policy.resolve_public(policy.require("provider","model"))

def test_gateway_pins_public_dns_uses_fixed_probe_and_maps_safe_errors():
    policy=ProviderAllowlist({"provider":{"base_url":"https://provider.example/v1","models":["model"]}},resolver=resolver("8.8.8.8")); transport=Transport(); gateway=OpenAiCompatibleGateway(policy,transport)
    assert asyncio.run(gateway.test_connection("provider","model","sk-secret"))>=0
    _,address,path,headers,body,_=transport.calls[0]; assert address=="8.8.8.8" and path=="/v1/chat/completions" and headers["Authorization"]=="Bearer sk-secret"
    document=json.loads(body); assert [message["content"] for message in document["messages"]]==[FIXED_PROBE_SYSTEM,FIXED_PROBE_USER]
    assert document["max_tokens"]==FIXED_PROBE_MAX_TOKENS==256
    assert document["thinking"]=={"type":"disabled"}
    rendered=body.decode(); assert all(value not in rendered for value in ("resume","简历","13800000000","JD text"))
    for status,code in ((401,"provider_auth_failed"),(404,"provider_model_not_found"),(429,"provider_quota_or_rate_limited"),(302,"provider_redirect_rejected")):
        with pytest.raises(GatewayError) as raised: asyncio.run(OpenAiCompatibleGateway(policy,Transport(status)).test_connection("provider","model","secret"))
        assert raised.value.safe_code==code
    with pytest.raises(GatewayError) as malformed: asyncio.run(OpenAiCompatibleGateway(policy,Transport(200,b"not-json")).test_connection("provider","model","secret"))
    assert malformed.value.safe_code=="provider_response_invalid"
    with pytest.raises(GatewayError) as oversized: asyncio.run(OpenAiCompatibleGateway(policy,Transport(200,b"x"*100),max_response_bytes=10).test_connection("provider","model","secret"))
    assert oversized.value.safe_code=="provider_response_too_large"
    invalid_content=b'{"choices":[{"message":{"content":"all systems nominal"}}]}'
    with pytest.raises(GatewayError,match="provider_response_invalid"):
        asyncio.run(OpenAiCompatibleGateway(policy,Transport(200,invalid_content)).test_connection("provider","model","secret"))


def test_evaluation_requires_and_sends_the_persisted_system_prompt():
    policy=ProviderAllowlist({"provider":{"base_url":"https://provider.example/v1","models":["model"]}},resolver=resolver("8.8.8.8"))
    body=b'{"choices":[{"message":{"content":"{\\"score\\":0,\\"dimensions\\":[{\\"key\\":\\"core_capability\\",\\"score\\":0,\\"evidence\\":[],\\"gaps\\":[]},{\\"key\\":\\"experience_depth\\",\\"score\\":0,\\"evidence\\":[],\\"gaps\\":[]},{\\"key\\":\\"role_seniority\\",\\"score\\":0,\\"evidence\\":[],\\"gaps\\":[]},{\\"key\\":\\"transferability\\",\\"score\\":0,\\"evidence\\":[],\\"gaps\\":[]},{\\"key\\":\\"explicit_constraints\\",\\"score\\":0,\\"evidence\\":[],\\"gaps\\":[]}],\\"summary\\":\\"none\\",\\"strengths\\":[],\\"gaps\\":[],\\"risks\\":[],\\"questions\\":[]}"}}]}'
    transport=Transport(body=body)

    asyncio.run(OpenAiCompatibleGateway(policy,transport).evaluate(
        "provider","model","secret",ScreeningRequest(job_description="JD",resume_text="resume"),
        system_prompt="prompt loaded from PromptVersion",
    ))

    document=json.loads(transport.calls[0][4])
    assert document["messages"][0]=={"role":"system","content":"prompt loaded from PromptVersion"}
    assert document["max_tokens"]==EVALUATION_MAX_TOKENS==8192
    assert document["thinking"]=={"type":"disabled"}


def test_evaluation_normalizes_common_openai_compatible_json_shapes():
    policy=ProviderAllowlist({"provider":{"base_url":"https://provider.example/v1","models":["model"]}},resolver=resolver("8.8.8.8"))
    provider_result={
        "score":0,
        "dimensions":{
            key:{"key":key,"score":0,"evidence":"No evidence supplied.","gaps":"No evidence supplied."}
            for key in ("core_capability","experience_depth","role_seniority","transferability","explicit_constraints")
        },
        "summary":"No matching evidence was supplied.",
        "strengths":"No confirmed strengths.",
        "gaps":"Insufficient resume detail.",
        "risks":[],
        "questions":"Please provide more project detail.",
    }
    body=json.dumps({"choices":[{"message":{"content":json.dumps(provider_result)}}]}).encode()

    evaluation=asyncio.run(OpenAiCompatibleGateway(policy,Transport(body=body)).evaluate(
        "provider","model","secret",ScreeningRequest(job_description="JD",resume_text="resume"),
        system_prompt="return the screening JSON",
    ))

    assert [dimension.key for dimension in evaluation.result.dimensions]==[
        "core_capability","experience_depth","role_seniority","transferability","explicit_constraints",
    ]
    assert evaluation.result.dimensions[0].evidence==["No evidence supplied."]
    assert evaluation.result.dimensions[0].gaps==["No evidence supplied."]
    assert evaluation.result.strengths==["No confirmed strengths."]
    assert evaluation.result.gaps==["Insufficient resume detail."]
    assert evaluation.result.questions==["Please provide more project detail."]

def test_allowlist_rejects_arbitrary_url_features_and_models():
    for url in ("http://provider.example/v1","https://user:pass@provider.example/v1","https://provider.example:8443/v1","file:///tmp/model","https://provider.example/v1?target=x","https://xn--bcher-kva.example/v1","https://127.0.0.1/v1","https://169.254.169.254/v1"):
        with pytest.raises(ProviderPolicyError): ProviderAllowlist({"provider":{"base_url":url,"models":["model"]}})
    policy=ProviderAllowlist({"provider":{"base_url":"https://provider.example/v1","models":["model"]}},resolver=resolver("8.8.8.8"))
    with pytest.raises(ProviderPolicyError): policy.require("provider","other")

@pytest.mark.parametrize("url",[
    "https://provider.example:not-a-port/v1",
    "https://provider.example:70000/v1",
])
def test_allowlist_normalizes_invalid_ports(url):
    with pytest.raises(ProviderPolicyError,match="provider_port_forbidden"):
        ProviderAllowlist({"provider":{"base_url":url,"models":["model"]}})

def test_gateway_normalizes_unexpected_transport_errors():
    class BrokenTransport:
        def post(self,*args,**kwargs):
            raise RuntimeError("secret provider response")

    policy=ProviderAllowlist({"provider":{"base_url":"https://provider.example/v1","models":["model"]}},resolver=resolver("8.8.8.8"))
    with pytest.raises(GatewayError,match="provider_unavailable") as raised:
        asyncio.run(OpenAiCompatibleGateway(policy,BrokenTransport()).test_connection("provider","model","sk-secret"))
    assert "secret provider response" not in str(raised.value)

def test_development_http_provider_preserves_transport_scheme():
    policy=ProviderAllowlist({"provider":{"base_url":"http://provider.example/v1","models":["model"]}},allow_http=True,resolver=resolver("8.8.8.8"))
    spec=policy.require("provider","model")
    assert spec.scheme=="http" and spec.port==80

    for url in ("http://provider.example:443/v1","https://provider.example:80/v1"):
        with pytest.raises(ProviderPolicyError,match="provider_port_forbidden"):
            ProviderAllowlist({"provider":{"base_url":url,"models":["model"]}},allow_http=True)

def test_gateway_bounds_concurrent_provider_requests():
    class CountingTransport(Transport):
        def __init__(self): super().__init__(); self.active=0; self.maximum=0; self.lock=threading.Lock()
        def post(self,*args,**kwargs):
            with self.lock:
                self.active+=1; self.maximum=max(self.maximum,self.active)
            try:
                time.sleep(.03)
                return super().post(*args,**kwargs)
            finally:
                with self.lock: self.active-=1

    policy=ProviderAllowlist({"provider":{"base_url":"https://provider.example/v1","models":["model"]}},resolver=resolver("8.8.8.8")); transport=CountingTransport(); gateway=OpenAiCompatibleGateway(policy,transport,max_concurrency=1)
    async def run_both(): await asyncio.gather(gateway.test_connection("provider","model","key"),gateway.test_connection("provider","model","key"))
    asyncio.run(run_both())
    assert transport.maximum==1 and len(transport.calls)==2

def test_mixed_dns_answers_fail_closed():
    policy=ProviderAllowlist({"provider":{"base_url":"https://provider.example/v1","models":["model"]}},resolver=lambda *args,**kwargs:[(2,1,6,"",("8.8.8.8",443)),(2,1,6,"",("127.0.0.1",443))])
    with pytest.raises(ProviderPolicyError): policy.resolve_public(policy.require("provider","model"))
