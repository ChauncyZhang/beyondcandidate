import pytest
from sqlalchemy import select, update

from server.app.integrations.feishu.models import FeishuOrganizationConfig  # noqa: F401 - register producer dependencies
from server.app.recruiting.models import Application
from server.app.recruiting.service import (
    ResourceVersionConflict,
    apply_application_workflow_action_record,
    transition_application_record,
)
from server.tests.test_offer_service import ORG, OWNER, make_session, seed_application


@pytest.mark.parametrize("workflow", [False, True])
def test_forward_transition_refreshes_application_cached_before_stop(workflow):
    with make_session() as db:
        application = seed_application(db)
        application.stage = "review"
        db.flush()
        # Model the row observed after a concurrent stop commits while this
        # session still retains the original ORM identity.
        db.execute(update(Application).where(Application.id == application.id).values(
            stage="withdrawn", version=2,
        ).execution_options(synchronize_session=False))
        assert application.stage == "review" and application.version == 1
        with pytest.raises(ResourceVersionConflict):
            if workflow:
                apply_application_workflow_action_record(db, ORG, application.id, "review_approved",
                    expected_version=1, actor_user_id=OWNER, trace_id="stale-workflow")
            else:
                transition_application_record(db, ORG, application.id, "contact",
                    expected_version=1, actor_user_id=OWNER, trace_id="stale-transition")
        assert db.scalar(select(Application.stage).where(Application.id == application.id)) == "withdrawn"
