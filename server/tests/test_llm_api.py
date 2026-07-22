from fastapi.testclient import TestClient
from sqlalchemy import select
from server.app.identity.models import AuditLog,Organization,User,UserRole
from server.app.identity.security import PasswordService
from server.app.llm.models import LlmInvocation,LlmProviderConfig
from server.app.llm.policy import ProviderAllowlist
from server.tests.test_screening_api import app_and_seed,login

class Gateway:
    def __init__(self): self.calls=[]
    async def test_connection(self,provider,model,key,**kwargs): self.calls.append((provider,model,key)); return 12

def test_llm_settings_role_key_preservation_and_fixed_connection_test(tmp_path):
    app,_,_=app_and_seed(tmp_path); app.state.llm_allowlist=ProviderAllowlist({"approved":{"base_url":"https://provider.example/v1","models":["model-a"]}},resolver=lambda *args,**kwargs:[(2,1,6,"",("8.8.8.8",443))]); gateway=Gateway(); app.state.llm_gateway=gateway
    with app.state.identity_store.sync_session() as db:
        organization=db.scalar(select(Organization)); password=PasswordService().hash("correct")
        for role,email in (("recruiter","llm-recruiter@example.test"),("interviewer","llm-interviewer@example.test")):
            user=User(organization_id=organization.id,email=email,normalized_email=email,display_name=role,password_hash=password); user.roles.append(UserRole(role=role)); db.add(user)
        db.commit()
    with TestClient(app) as client:
        system=login(client,"system@example.test"); payload={"provider_id":"approved","model":"model-a","enabled":True,"api_key":"sk-private","allowed_job_ids":[]}
        saved=client.put("/api/v1/settings/llm",json=payload,headers={**system,"If-Match":'"0"',"Idempotency-Key":"save"}); assert saved.status_code==200,saved.text; assert saved.json()["data"]["key_configured"] is True and saved.json()["data"]["available_providers"]=={"approved":["model-a"]} and "api_key" not in saved.text
        conflict=client.put("/api/v1/settings/llm",json={**payload,"enabled":False},headers={**system,"If-Match":'"0"',"Idempotency-Key":"save"}); assert conflict.status_code==409 and conflict.json()["code"]=="idempotency_conflict"
        no_csrf=client.put("/api/v1/settings/llm",json=payload,headers={"Origin":system["Origin"],"If-Match":'"1"',"Idempotency-Key":"no-csrf"}); assert no_csrf.status_code==403
        replay=client.put("/api/v1/settings/llm",json=payload,headers={**system,"If-Match":'"0"',"Idempotency-Key":"save"}); assert replay.json()==saved.json()
        preserved=client.put("/api/v1/settings/llm",json={"provider_id":"approved","model":"model-a","enabled":True,"allowed_job_ids":[]},headers={**system,"If-Match":'"1"',"Idempotency-Key":"preserve"}); assert preserved.status_code==200 and preserved.json()["data"]["version"]==2 and preserved.json()["data"]["key_configured"] is True
        stale=client.put("/api/v1/settings/llm",json={"provider_id":"approved","model":"model-a","enabled":False,"allowed_job_ids":[]},headers={**system,"If-Match":'"1"',"Idempotency-Key":"stale"}); assert stale.status_code==409 and stale.json()["code"]=="resource_version_conflict"
        tested=client.post("/api/v1/settings/llm/test",headers={**system,"Idempotency-Key":"test"}); assert tested.status_code==200 and tested.json()["data"]["status"]=="succeeded"; assert gateway.calls==[("approved","model-a","sk-private")]
        test_replay=client.post("/api/v1/settings/llm/test",headers={**system,"Idempotency-Key":"test"}); assert test_replay.json()==tested.json() and len(gateway.calls)==1
        client.post("/api/v1/auth/logout",headers=system); admin=login(client,"admin@example.test"); visible=client.get("/api/v1/settings/llm",headers=admin); assert visible.status_code==200 and "key_configured" not in visible.text
        client.post("/api/v1/auth/logout",headers=admin); manager=login(client,"manager@example.test"); assert client.get("/api/v1/settings/llm",headers=manager).status_code==404
        client.post("/api/v1/auth/logout",headers=manager); system=login(client,"system@example.test"); system_audits=client.get("/api/v1/audit-logs?event_type=llm.config_updated",headers=system)
        client.post("/api/v1/auth/logout",headers=system); admin=login(client,"admin@example.test"); recruiting_audits=client.get("/api/v1/audit-logs?event_type=llm.config_updated",headers=admin)
        assert [row["category"] for row in system_audits.json()["data"]]==["system","system"]
        assert recruiting_audits.json()["data"]==[]
        for email in ("llm-recruiter@example.test","manager@example.test","llm-interviewer@example.test"):
            client.post("/api/v1/auth/logout",headers=admin); denied_headers=login(client,email); denied=client.get("/api/v1/audit-logs?event_type=llm.config_updated",headers=denied_headers)
            assert denied.json()["data"]==[] if email.startswith("llm-recruiter") else denied.status_code==404
            admin=denied_headers
        with app.state.identity_store.sync_session() as db:
            db.add(UserRole(user_id=db.scalar(select(User.id).where(User.email=="admin@example.test")),role="system_admin")); db.commit()
        client.post("/api/v1/auth/logout",headers=admin); dual=login(client,"admin@example.test"); dual_audits=client.get("/api/v1/audit-logs?event_type=llm.config_updated",headers=dual)
        assert [row["category"] for row in dual_audits.json()["data"]]==["system","system"]
    with app.state.identity_store.sync_session() as db:
        config=db.scalar(select(LlmProviderConfig)); assert config.encrypted_api_key and b"sk-private" not in config.encrypted_api_key
        assert db.scalar(select(LlmInvocation)).request_field_manifest==["fixed_probe"]
        rendered=str([audit.metadata_json for audit in db.scalars(select(AuditLog))]); assert "sk-private" not in rendered

def test_llm_settings_reject_unapproved_provider_and_require_key(tmp_path):
    app,_,_=app_and_seed(tmp_path); app.state.llm_allowlist=ProviderAllowlist({"approved":{"base_url":"https://provider.example/v1","models":["model-a"]}},resolver=lambda *args,**kwargs:[(2,1,6,"",("8.8.8.8",443))])
    with TestClient(app) as client:
        system=login(client,"system@example.test")
        denied=client.put("/api/v1/settings/llm",json={"provider_id":"other","model":"model-a","enabled":False},headers={**system,"If-Match":'"0"',"Idempotency-Key":"other"}); assert denied.status_code==422
        missing=client.put("/api/v1/settings/llm",json={"provider_id":"approved","model":"model-a","enabled":True},headers={**system,"If-Match":'"0"',"Idempotency-Key":"missing"}); assert missing.status_code==422 and missing.json()["code"]=="api_key_required"

def test_system_admin_can_add_an_organization_provider_and_use_it(tmp_path):
    app,_,_=app_and_seed(tmp_path)
    with TestClient(app) as client:
        system=login(client,"system@example.test")
        payload={"provider_id":"bigmodel","display_name":"智谱 BigModel","base_url":"https://open.bigmodel.cn/api/paas/v4","models":["glm-4.5","glm-5.2"]}
        created=client.post("/api/v1/settings/llm/providers",json=payload,headers={**system,"Idempotency-Key":"provider-create"})
        assert created.status_code==201,created.text
        assert created.json()["data"]=={**payload,"source":"organization"}
        providers=client.get("/api/v1/settings/llm/providers",headers=system)
        assert providers.status_code==200 and providers.json()["data"]==[{**payload,"source":"organization"}]
        config=client.get("/api/v1/settings/llm",headers=system).json()["data"]
        assert config["available_providers"]=={"bigmodel":["glm-4.5","glm-5.2"]}
        assert config["provider_options"]==[{**payload,"source":"organization"}]
        saved=client.put("/api/v1/settings/llm",json={"provider_id":"bigmodel","model":"glm-5.2","enabled":False},headers={**system,"If-Match":'"0"',"Idempotency-Key":"config-save"})
        assert saved.status_code==200,saved.text
        replay=client.post("/api/v1/settings/llm/providers",json=payload,headers={**system,"Idempotency-Key":"provider-create"})
        assert replay.status_code==201 and replay.json()==created.json()

def test_provider_creation_rejects_unsafe_urls_duplicates_and_non_system_users(tmp_path):
    app,_,_=app_and_seed(tmp_path)
    with TestClient(app) as client:
        system=login(client,"system@example.test")
        unsafe={"provider_id":"private","display_name":"Private","base_url":"https://127.0.0.1/v1","models":["model"]}
        rejected=client.post("/api/v1/settings/llm/providers",json=unsafe,headers={**system,"Idempotency-Key":"unsafe"})
        assert rejected.status_code==422 and rejected.json()["code"]=="provider_address_forbidden"
        valid={**unsafe,"provider_id":"approved","base_url":"https://provider.example/v1"}
        first=client.post("/api/v1/settings/llm/providers",json=valid,headers={**system,"Idempotency-Key":"first"})
        assert first.status_code==201
        duplicate=client.post("/api/v1/settings/llm/providers",json=valid,headers={**system,"Idempotency-Key":"second"})
        assert duplicate.status_code==409 and duplicate.json()["code"]=="provider_already_exists"
        client.post("/api/v1/auth/logout",headers=system)
        admin=login(client,"admin@example.test")
        denied=client.post("/api/v1/settings/llm/providers",json={**valid,"provider_id":"other"},headers={**admin,"Idempotency-Key":"denied"})
        assert denied.status_code==404
