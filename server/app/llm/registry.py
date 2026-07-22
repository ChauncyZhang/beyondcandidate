from sqlalchemy import select

from server.app.llm.models import LlmProvider
from server.app.llm.policy import ProviderAllowlist,ProviderPolicyError


class DatabaseProviderCatalog:
    def __init__(self,sessions,deployed:ProviderAllowlist,*,allow_http:bool=False,resolver=None):
        self.sessions=sessions; self.deployed=deployed; self.allow_http=allow_http; self.resolver=resolver

    def _policy(self,row:LlmProvider)->ProviderAllowlist:
        return ProviderAllowlist({row.provider_id:{"base_url":row.base_url,"models":list(row.models)}},allow_http=self.allow_http,resolver=self.resolver)

    def require(self,provider_id:str,model:str,*,organization_id=None):
        try: return self.deployed.require(provider_id,model)
        except ProviderPolicyError:
            if organization_id is None: raise
        with self.sessions() as db:
            row=db.scalar(select(LlmProvider).where(LlmProvider.organization_id==organization_id,LlmProvider.provider_id==provider_id))
            if row is None: raise ProviderPolicyError("provider_or_model_not_allowed")
            return self._policy(row).require(provider_id,model)

    def resolve_public(self,spec):
        return ProviderAllowlist({},allow_http=self.allow_http,resolver=self.resolver).resolve_public(spec)

    def public_view(self,organization_id=None)->dict[str,list[str]]:
        values=self.deployed.public_view()
        if organization_id is None: return values
        with self.sessions() as db:
            rows=db.scalars(select(LlmProvider).where(LlmProvider.organization_id==organization_id).order_by(LlmProvider.created_at,LlmProvider.provider_id)).all()
            values.update({row.provider_id:list(row.models) for row in rows})
        return values

    def options(self,organization_id)->list[dict]:
        options=[{"provider_id":provider_id,"display_name":provider_id,"base_url":None,"models":models,"source":"deployment"} for provider_id,models in self.deployed.public_view().items()]
        with self.sessions() as db:
            rows=db.scalars(select(LlmProvider).where(LlmProvider.organization_id==organization_id).order_by(LlmProvider.created_at,LlmProvider.provider_id)).all()
            options.extend({"provider_id":row.provider_id,"display_name":row.display_name,"base_url":row.base_url,"models":list(row.models),"source":"organization"} for row in rows)
        return options
