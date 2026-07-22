RUN_TRANSITIONS={"queued":{"parsing","cancelled"},"parsing":{"rule_scoring","partial","failed","cancelled"},"rule_scoring":{"llm_scoring","completed","partial","failed","cancelled"},"llm_scoring":{"completed","partial","failed","cancelled"}}
ITEM_TRANSITIONS={"queued":{"parsing","cancelled"},"parsing":{"parsed","failed"},"parsed":{"scoring","failed"},"scoring":{"scored","failed"}}
class InvalidScreeningTransition(ValueError): pass
def _transition(record,target,allowed):
    if target not in allowed.get(record.status,set()): raise InvalidScreeningTransition("invalid_screening_transition")
    record.status=target; record.version=getattr(record,"version",0)+1 if hasattr(record,"version") else getattr(record,"version",0)
def transition_run(run,target): _transition(run,target,RUN_TRANSITIONS)
def transition_item(item,target): _transition(item,target,ITEM_TRANSITIONS)
