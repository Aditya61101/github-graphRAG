"""Offline preserved-context rebuild using the installed production planner."""

import asyncio
import time

from ai_services.runs import prepare_stage_4_2_v6_final as check
from ai_services.runs.benchmark_stage_4_1_hybrid_retrieval import save
from ai_services.runs.prepare_stage_4_2_live_validation import ROOT

Base = check.ContextRequestBudget
traces = []


class Measured(Base):
    def pack(self, *args, **kwargs):
        start = time.monotonic()
        result = super().pack(*args, **kwargs)
        self.planner_seconds = time.monotonic() - start
        return result

    def __call__(self, ctx):
        result = super().__call__(ctx)
        traces.append(
            {
                "context_hash": ctx.context_hash,
                "requirements": [r.requirement_id for r in ctx.requirements],
                "context_tokens": ctx.budget.estimated_tokens,
                "minimum_units": [
                    m.model_dump(mode="json") for m in self.last_unit_measurements
                ],
                "omitted_unit_ids": result.omitted_unit_ids,
                "packets": [
                    {
                        "batch_id": p.batch_id,
                        "unit_ids": [u.unit_id for u in p.units],
                        "input_tokens": self.count(p, "ASSESS"),
                    }
                    for p in self.last_packets
                ],
                "planner_seconds": self.planner_seconds,
                "individually_feasible": not result.omitted_unit_ids,
            }
        )
        return result


async def main():
    check.TAG = "budget_preserved_v6_final"
    check.ContextRequestBudget = Measured
    start = time.monotonic()
    await check.main()
    save(
        ROOT / "A_budget_preservation_final_trace.json",
        {"iterations": traces, "runtime_seconds": time.monotonic() - start},
    )


if __name__ == "__main__":
    asyncio.run(main())
