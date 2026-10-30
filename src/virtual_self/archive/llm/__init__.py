"""Archive LLM layers L1–L5 and archive supervisor."""

from virtual_self.archive.llm.specialists import PRODUCT_TO_SPECIALIST_ID, SPECIALIST_REGISTRY
from virtual_self.archive.llm.supervisor import (
    INTEGRATION_RETRY_NOTE,
    SUPERVISED_PLAN_SCHEMA_VERSION,
    SupervisorAssignment,
    SupervisorPlan,
    SupervisedPlan,
    collect_detected,
    is_available,
    load_supervised_plan,
    merge_llm_supervisor_output,
    plan,
    run_supervisor,
    specialist_id_for_product,
    supervised_plan_path,
    write_supervised_plan,
)
from virtual_self.archive.llm.supervisor_prompts import SUPERVISOR_PROMPT_VERSION

__all__ = [
    "INTEGRATION_RETRY_NOTE",
    "PRODUCT_TO_SPECIALIST_ID",
    "SPECIALIST_REGISTRY",
    "SUPERVISED_PLAN_SCHEMA_VERSION",
    "SUPERVISOR_PROMPT_VERSION",
    "SupervisorAssignment",
    "SupervisorPlan",
    "SupervisedPlan",
    "collect_detected",
    "is_available",
    "load_supervised_plan",
    "merge_llm_supervisor_output",
    "plan",
    "run_supervisor",
    "specialist_id_for_product",
    "supervised_plan_path",
    "write_supervised_plan",
]
