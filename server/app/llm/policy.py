import ipaddress,re,socket
from dataclasses import dataclass
from urllib.parse import urlsplit

PROVIDER_ID=re.compile(r"^[a-z][a-z0-9_-]{1,63}$"); MODEL_ID=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
class ProviderPolicyError(ValueError): pass

@dataclass(frozen=True)
class ProviderSpec:
    provider_id:str; base_url:str; models:tuple[str,...]; scheme:str; host:str; port:int; base_path:str

def _public_address(value:str)->str:
    try: address=ipaddress.ip_address(value.split("%")[0])
    except ValueError: raise ProviderPolicyError("provider_dns_invalid") from None
    if not address.is_global: raise ProviderPolicyError("provider_address_forbidden")
    return str(address)

class ProviderAllowlist:
    def __init__(self,raw:dict[str,dict[str,object]],*,allow_http:bool=False,resolver=None):
        self._resolver=resolver or socket.getaddrinfo; self._entries={}
        for provider_id,value in raw.items():
            if not PROVIDER_ID.fullmatch(provider_id) or not isinstance(value,dict) or set(value)!={"base_url","models"}: raise ProviderPolicyError("provider_allowlist_invalid")
            base_url=value["base_url"]; models=value["models"]
            if not isinstance(base_url,str) or not isinstance(models,list) or not 1<=len(models)<=100 or any(not isinstance(model,str) or not MODEL_ID.fullmatch(model) for model in models): raise ProviderPolicyError("provider_allowlist_invalid")
            parsed=urlsplit(base_url)
            if parsed.scheme not in ({"https","http"} if allow_http else {"https"}) or parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname: raise ProviderPolicyError("provider_url_forbidden")
            if not parsed.hostname.isascii() or any(label.casefold().startswith("xn--") for label in parsed.hostname.split(".")) or "%" in parsed.path or any(part in {".",".."} for part in parsed.path.split("/")): raise ProviderPolicyError("provider_url_forbidden")
            try: literal=ipaddress.ip_address(parsed.hostname)
            except ValueError: literal=None
            if literal is not None and not literal.is_global: raise ProviderPolicyError("provider_address_forbidden")
            try: port=parsed.port or (443 if parsed.scheme=="https" else 80)
            except ValueError: raise ProviderPolicyError("provider_port_forbidden") from None
            if port != (443 if parsed.scheme=="https" else 80): raise ProviderPolicyError("provider_port_forbidden")
            self._entries[provider_id]=ProviderSpec(provider_id,base_url,tuple(models),parsed.scheme,parsed.hostname.rstrip(".").casefold(),port,parsed.path.rstrip("/"))
    def require(self,provider_id:str,model:str,*,organization_id=None)->ProviderSpec:
        spec=self._entries.get(provider_id)
        if spec is None or model not in spec.models: raise ProviderPolicyError("provider_or_model_not_allowed")
        return spec
    def resolve_public(self,spec:ProviderSpec)->tuple[str,...]:
        try: rows=self._resolver(spec.host,spec.port,type=socket.SOCK_STREAM)
        except OSError: raise ProviderPolicyError("provider_dns_failed") from None
        addresses=tuple(dict.fromkeys(_public_address(row[4][0]) for row in rows))
        if not addresses: raise ProviderPolicyError("provider_dns_failed")
        return addresses
    def public_view(self,organization_id=None)->dict[str,list[str]]: return {key:list(value.models) for key,value in self._entries.items()}
