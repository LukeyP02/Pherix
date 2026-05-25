"""The air-gapped flagship: the frozen enterprise agent, governed offline, on a
LOCAL open model.

Nothing new is built here — no new agent, no new engine. This package is *deployment*:
it points the same frozen regulated-data-ops agent
(:mod:`examples.dogfood.sims.enterprise.agent`) at a local OpenAI-compatible
endpoint (Ollama / vLLM / LM Studio) instead of a cloud model, and proves the
sovereignty claim that makes this the strongest single demo for a regulated
buyer: the model is local, the regulated data never leaves the perimeter, and
Pherix governs the run entirely offline — *the same governed journal* it would
produce on cloud Claude. That last clause is model-blindness: Pherix wraps the
tool-call layer, not the model, so an open local model is governed identically.

Two halves:

  * :mod:`run_local` — run the frozen agent through ``run_agent(api="openai")``
    against ``LOCAL_MODEL_URL``, governed; print the governed result + journal.
    Skips cleanly when no endpoint is configured / reachable.
  * :mod:`capture_airgap` — wrap that run in an :class:`~capture_airgap.EgressGuard`
    that *records every socket the run opens* and asserts none of them left the
    perimeter (no public-internet egress to a cloud model API), then surfaces the
    journal in the governance console. The sovereignty claim is **verified, not
    asserted**.
"""
