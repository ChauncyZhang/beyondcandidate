import os,subprocess,uuid
import pytest
from sqlalchemy import create_engine,inspect,text
from sqlalchemy.exc import DBAPIError,IntegrityError

pytestmark=pytest.mark.skipif(not os.getenv("POSTGRES_SMOKE_URL"),reason="PostgreSQL smoke URL not configured")
def test_llm_tables_are_tenant_scoped_and_evidence_is_append_only():
    url=os.environ["POSTGRES_SMOKE_URL"]; subprocess.run(["python","-m","alembic","-c","server/alembic.ini","upgrade","head"],check=True,env={**os.environ,"DATABASE_URL":url}); engine=create_engine(url.replace("+asyncpg","+psycopg")); ids={name:uuid.uuid4() for name in ("o1","o2","u1","u2","config","prompt","invocation")}
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE organizations CASCADE")); connection.execute(text("INSERT INTO organizations(id,slug,name,status,created_at,updated_at) VALUES(:o1,'one','One','active',now(),now()),(:o2,'two','Two','active',now(),now())"),ids); connection.execute(text("INSERT INTO users(id,organization_id,email,normalized_email,display_name,password_hash,status,authorization_version,created_at,updated_at) VALUES(:u1,:o1,'a@x','a@x','A','x','active',1,now(),now()),(:u2,:o2,'b@x','b@x','B','x','active',1,now(),now())"),ids)
        connection.execute(text("INSERT INTO llm_provider_configs(id,organization_id,provider_id,model,encrypted_api_key,enabled,allowed_job_ids,version,created_by,updated_by,created_at,updated_at) VALUES(:config,:o1,'approved','model',decode('00','hex'),false,'[]',1,:u1,:u1,now(),now())"),ids)
        connection.execute(text("INSERT INTO prompt_versions(id,organization_id,name,version_number,content,content_hash,created_by,created_at) VALUES(:prompt,:o1,'screen',1,'{}',repeat('0',64),:u1,now())"),ids)
        connection.execute(text("INSERT INTO llm_invocations(id,organization_id,config_id,prompt_version_id,provider_id,model,request_field_manifest,status,usage,created_at) VALUES(:invocation,:o1,:config,:prompt,'approved','model','[]','queued','{}',now())"),ids)
    for statement,error in (("INSERT INTO llm_invocations(id,organization_id,config_id,provider_id,model,request_field_manifest,status,usage,created_at) VALUES(gen_random_uuid(),:o2,:config,'approved','model','[]','queued','{}',now())",IntegrityError),("UPDATE prompt_versions SET content='{}' WHERE id=:prompt",DBAPIError),("DELETE FROM llm_invocations WHERE id=:invocation",DBAPIError)):
        with engine.connect() as connection:
            transaction=connection.begin()
            with pytest.raises(error): connection.execute(text(statement),ids)
            transaction.rollback()
    engine.dispose()

def test_llm_screening_evaluations_are_tenant_safe_idempotent_and_immutable():
    url=os.environ["POSTGRES_SMOKE_URL"]; subprocess.run(["python","-m","alembic","-c","server/alembic.ini","upgrade","head"],check=True,env={**os.environ,"DATABASE_URL":url}); engine=create_engine(url.replace("+asyncpg","+psycopg")); ids={name:uuid.uuid4() for name in ("o1","o2","u1","u2","job","jd","rule","file","run","item","result","config","prompt","queue","invocation","evaluation","priority_invocation","suggest_invocation","invalid_invocation")}
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE organizations CASCADE")); connection.execute(text("INSERT INTO organizations(id,slug,name,status,created_at,updated_at) VALUES(:o1,'eval-one','One','active',now(),now()),(:o2,'eval-two','Two','active',now(),now())"),ids)
        connection.execute(text("INSERT INTO users(id,organization_id,email,normalized_email,display_name,password_hash,status,authorization_version,created_at,updated_at) VALUES(:u1,:o1,'eval-a@x','eval-a@x','A','x','active',1,now(),now()),(:u2,:o2,'eval-b@x','eval-b@x','B','x','active',1,now(),now())"),ids)
        connection.execute(text("INSERT INTO jobs(id,organization_id,title,status,owner_id,headcount,priority,version,created_at,updated_at) VALUES(:job,:o1,'Job','draft',:u1,1,'normal',1,now(),now())"),ids)
        for table,key in (("job_jd_versions","jd"),("screening_rule_versions","rule")): connection.execute(text(f"INSERT INTO {table}(id,organization_id,job_id,version_number,content,created_by,created_at) VALUES(:{key},:o1,:job,1,'{{}}',:u1,now())"),ids)
        connection.execute(text("INSERT INTO file_objects(id,organization_id,storage_key,original_filename,mime_type,size_bytes,sha256,uploaded_by,created_at) VALUES(:file,:o1,'eval/x','x.txt','text/plain',1,repeat('0',64),:u1,now())"),ids)
        connection.execute(text("INSERT INTO screening_runs(id,organization_id,job_id,jd_version_id,rule_version_id,source,status,total_count,processed_count,succeeded_count,failed_count,created_by,version,created_at,updated_at) VALUES(:run,:o1,:job,:jd,:rule,'upload','completed',1,1,1,0,:u1,1,now(),now())"),ids)
        connection.execute(text("INSERT INTO screening_items(id,organization_id,run_id,file_object_id,status,attempts,llm_status,llm_attempts,created_at,updated_at) VALUES(:item,:o1,:run,:file,'scored',1,'succeeded',1,now(),now())"),ids)
        connection.execute(text("INSERT INTO screening_results(id,organization_id,item_id,rule_engine_version,rule_score,recommendation,required_hits,required_missing,bonus_hits,estimated_years,risks,questions,created_at,updated_at) VALUES(:result,:o1,:item,'rule-v1',80,'优先沟通','[]','[]','[]',3,'[]','[]',now(),now())"),ids)
        connection.execute(text("INSERT INTO llm_provider_configs(id,organization_id,provider_id,model,encrypted_api_key,enabled,allowed_job_ids,version,created_by,updated_by,created_at,updated_at) VALUES(:config,:o1,'approved','model',decode('00','hex'),false,'[]',2,:u1,:u1,now(),now())"),ids)
        connection.execute(text("INSERT INTO prompt_versions(id,organization_id,name,version_number,content,content_hash,created_by,created_at) VALUES(:prompt,:o1,'screen',1,'{}',repeat('0',64),:u1,now())"),ids)
        connection.execute(text("INSERT INTO background_jobs(id,organization_id,type,payload,status,priority,attempts,max_attempts,run_after,created_at,updated_at) VALUES(:queue,:o1,'screening.llm_score_item','{}','succeeded',0,1,3,now(),now(),now())"),ids)
        connection.execute(text("INSERT INTO llm_invocations(id,organization_id,config_id,prompt_version_id,screening_result_id,queue_job_id,attempt_no,config_version,input_sha256,provider_id,model,request_field_manifest,status,usage,created_at) VALUES(:invocation,:o1,:config,:prompt,:result,:queue,1,2,repeat('a',64),'approved','model','[]','succeeded','{}',now())"),ids)
        for invocation in ("priority_invocation", "suggest_invocation", "invalid_invocation"):
            connection.execute(text(f"INSERT INTO llm_invocations(id,organization_id,config_id,prompt_version_id,screening_result_id,provider_id,model,request_field_manifest,status,usage,created_at) VALUES(:{invocation},:o1,:config,:prompt,:result,'approved','model','[]','succeeded','{{}}',now())"),ids)
        connection.execute(text("INSERT INTO llm_screening_evaluations(id,organization_id,screening_result_id,invocation_id,prompt_version_id,score,recommendation,summary,strengths,gaps,risks,interview_questions,dimensions,created_at) VALUES(:evaluation,:o1,:result,:invocation,:prompt,91,'优先沟通','Strong match','[]','[]','[]','[]','[{\"name\": \"experience\", \"score\": 91}]',now())"),ids)
        for recommendation, invocation in (("优先评审", "priority_invocation"), ("建议评审", "suggest_invocation")):
            connection.execute(text("INSERT INTO llm_screening_evaluations(id,organization_id,screening_result_id,invocation_id,prompt_version_id,score,recommendation,summary,strengths,gaps,risks,interview_questions,dimensions,created_at) VALUES(gen_random_uuid(),:o1,:result,:invocation,:prompt,90,:recommendation,'New recommendation','[]','[]','[]','[]','[]',now())"), {**ids, "invocation": ids[invocation], "recommendation": recommendation})
    statements=(
        "INSERT INTO llm_screening_evaluations(id,organization_id,screening_result_id,invocation_id,prompt_version_id,score,recommendation,summary,strengths,gaps,risks,interview_questions,created_at) VALUES(gen_random_uuid(),:o2,:result,:invocation,:prompt,50,'可沟通','x','[]','[]','[]','[]',now())",
        "INSERT INTO llm_invocations(id,organization_id,config_id,prompt_version_id,screening_result_id,queue_job_id,attempt_no,config_version,input_sha256,provider_id,model,request_field_manifest,status,usage,created_at) VALUES(gen_random_uuid(),:o1,:config,:prompt,:result,:queue,1,2,repeat('b',64),'approved','model','[]','failed','{}',now())",
        "UPDATE llm_screening_evaluations SET score=1 WHERE id=:evaluation",
        "DELETE FROM llm_screening_evaluations WHERE id=:evaluation",
    )
    for statement in statements:
        with engine.connect() as connection:
            transaction=connection.begin()
            with pytest.raises((IntegrityError,DBAPIError)): connection.execute(text(statement),ids)
            transaction.rollback()
    checks={item["name"] for item in inspect(engine).get_check_constraints("llm_screening_evaluations")}
    assert {"ck_llm_screening_evaluations_score","ck_llm_screening_evaluations_recommendation"} <= checks
    with engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(IntegrityError):
            connection.execute(text("INSERT INTO llm_screening_evaluations(id,organization_id,screening_result_id,invocation_id,prompt_version_id,score,recommendation,summary,strengths,gaps,risks,interview_questions,dimensions,created_at) VALUES(gen_random_uuid(),:o1,:result,:invalid_invocation,:prompt,90,'AI评分不可用','Invalid recommendation','[]','[]','[]','[]','[]',now())"),ids)
        transaction.rollback()
        assert connection.scalar(text("SELECT dimensions FROM llm_screening_evaluations WHERE id = :evaluation"), ids) == [{"name": "experience", "score": 91}]
        assert set(connection.scalars(text("SELECT recommendation FROM llm_screening_evaluations WHERE recommendation IN ('优先评审','建议评审')"))) == {"优先评审", "建议评审"}
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE organizations CASCADE"))
    engine.dispose()
