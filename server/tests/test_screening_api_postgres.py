import os,subprocess,threading
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func,select,text
from sqlalchemy.exc import IntegrityError
from server.app.core.settings import Settings
from server.app.identity.models import Job,JobCollaborator,Organization,User,UserRole
from server.app.identity.security import PasswordService
from server.app.main import create_app
from server.app.recruiting.models import FileObject,JobJdVersion,ScreeningRuleVersion
from server.app.queue.models import BackgroundJob
from server.app.screening.models import CandidateDuplicateHint,ScreeningRun
from server.tests.test_screening_api import FakeQuarantineStorage,Probe,login
pytestmark=pytest.mark.skipif(not os.getenv("POSTGRES_SMOKE_URL"),reason="PostgreSQL smoke URL not configured")

def test_postgres_screening_api_concurrency_versions_limit_and_duplicates():
    url=os.environ["POSTGRES_SMOKE_URL"]; subprocess.run(["python","-m","alembic","-c","server/alembic.ini","upgrade","head"],check=True,env={**os.environ,"DATABASE_URL":url}); storage=FakeQuarantineStorage(); app=create_app(settings=Settings(environment="test",database_url=url,cors_origins=["https://hr.example.test"]),database_probe=Probe(),storage_probe=Probe(),quarantine_storage=storage)
    with app.state.identity_store.sync_session() as db:
        db.execute(text("TRUNCATE organizations CASCADE")); org=Organization(slug="acme",name="PG",status="active"); user=User(organization=org,email="admin@pg.test",normalized_email="admin@pg.test",display_name="Admin",password_hash=PasswordService().hash("correct")); user.roles.append(UserRole(role="recruiting_admin")); db.add(user); db.flush(); jobs=[]
        for title in ("One","Two"):
            job=Job(organization_id=org.id,title=title,owner_id=user.id,status="draft"); db.add(job); db.flush(); jd=JobJdVersion(organization_id=org.id,job_id=job.id,version_number=1,content={},created_by=user.id); rule=ScreeningRuleVersion(organization_id=org.id,job_id=job.id,version_number=1,content={},created_by=user.id); db.add_all([jd,rule]); db.flush(); jobs.append((job.id,jd.id,rule.id))
        db.commit()
    with TestClient(app) as client:
        headers=login(client,"admin@pg.test"); headers["Idempotency-Key"]="mismatch"; mismatch=client.post(f"/api/v1/jobs/{jobs[0][0]}/screening-runs",json={"jd_version_id":str(jobs[1][1]),"rule_version_id":str(jobs[1][2])},headers=headers); assert mismatch.status_code==422
        run=client.post(f"/api/v1/jobs/{jobs[0][0]}/screening-runs",json={},headers={**headers,"Idempotency-Key":"run"}).json()["data"]
        def upload(index): return client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":(f"{index}.txt",b"same-content","text/plain")},headers={**headers,"Idempotency-Key":f"item-{index}"})
        with ThreadPoolExecutor(max_workers=2) as pool: responses=list(pool.map(upload,(1,2)))
        assert [r.status_code for r in responses]==[201,201]
        with app.state.identity_store.sync_session() as db:
            stored=db.get(ScreeningRun,run["id"]); assert stored.total_count==2; assert db.scalar(select(CandidateDuplicateHint)) is not None; organization_id=stored.organization_id
            stored.total_count=100; db.commit()
        limited=upload(101); assert limited.status_code==409 and limited.json()["code"]=="screening_item_limit"
        assert all(key.startswith(f"quarantine/{organization_id}/{run['id']}/") and "same" not in key for key in storage.objects)

        writes_before=storage.writes
        with app.state.identity_store.sync_session() as db: db.get(ScreeningRun,run["id"]).total_count=2; db.commit()
        entered=threading.Event(); release=threading.Event(); original_write=storage.write
        def slow_write(*args): entered.set(); assert release.wait(5); return original_write(*args)
        storage.write=slow_write
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(lambda: client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":("slow.txt",b"slow","text/plain")},headers={**headers,"Idempotency-Key":"slow"}))
            assert entered.wait(5)
            with app.state.identity_store.sync_session() as db: assert db.scalar(select(ScreeningRun).where(ScreeningRun.id==run["id"]).with_for_update(nowait=True)); db.commit()
            release.set(); assert pending.result().status_code==201
        storage.write=original_write; writes_before=storage.writes; deletes_before=storage.deletes
        def replay(): return client.post(f"/api/v1/screening-runs/{run['id']}/items",files={"file":("race.txt",b"race","text/plain")},headers={**headers,"Idempotency-Key":"race"})
        with ThreadPoolExecutor(max_workers=2) as pool: raced=list(pool.map(lambda _: replay(),range(2)))
        assert [r.status_code for r in raced]==[201,201] and raced[0].json()==raced[1].json()
        assert storage.writes in {writes_before+1,writes_before+2} and storage.deletes-deletes_before==storage.writes-(writes_before+1)
        with app.state.identity_store.sync_session() as db:
            db.scalar(select(FileObject)).detected_type="exe"
            with pytest.raises(IntegrityError): db.commit()
            db.rollback()
        def start(key): return client.post(f"/api/v1/screening-runs/{run['id']}/start",headers={**headers,"Idempotency-Key":key})
        with ThreadPoolExecutor(max_workers=2) as pool: starts=list(pool.map(start,("start-a","start-b")))
        assert sorted(response.status_code for response in starts)==[200,409]
        with app.state.identity_store.sync_session() as db:
            assert db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.type=="screening.parse_item"))==db.get(ScreeningRun,run["id"]).total_count
