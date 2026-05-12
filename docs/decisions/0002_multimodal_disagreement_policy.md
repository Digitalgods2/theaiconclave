# Decision Record 0002 — Charter Amendment v1.1: Multimodal Disagreement Policy

**Date**: 2026-05-11
**Mode**: conclave (codex + gemini + claude-code) → charter amendment ratified by Glen
**Convergence**: weak (all three i_am_done, positions substantively identical with wording differences)
**Keeper**: claude-code

## What Was Chosen

Amend the Conclave Charter to **v1.1** by adding a "Multimodal Disagreement" section. When the conclave is reasoning about image (or other non-text) content and participants describe the underlying material differently in *literal perception* (not opinion or interpretation), they MUST:

1. **Not run a synthesis round** for the perceptual disagreement.
2. **Not force consensus** by having one participant defer to others.
3. Set `convergence: need_user_input` and emit a structured disagreement summary as the `user_input_question`. The summary must include: consensus observations, disputed observations (attributed by participant), type of each claim (literal / OCR / interpretation), confidence where reported, suspected cause of divergence, and a concrete adjudication question for Glen.

Glen's answer becomes the authoritative resolution for that image. A participant whose perception was wrong does not have its non-perceptual contributions discarded.

## Why It Was Chosen

A 3-AI conclave run on 2026-05-11 surfaced a real failure mode: Gemini's multimodal pipeline silently produced a wrong image description on round 1 (water and trees instead of the actual desert / TikTok Tribe scene), then "corrected" on round 2 only after seeing Codex and Claude's accurate descriptions. That is conformity, not perception — and conformity produces less reliable answers than transparent disagreement.

Three independent reasons motivate the policy:

1. **Visual perception is fact, not opinion.** The synthesis round is designed to resolve wording or analytical differences. Applying it to perceptual disputes asks a participant to "update its view" of pixels it cannot re-examine — which can only produce conformity, never improved perception.
2. **Glen has the ground truth.** Glen can see the image. The conclave cannot independently verify its own perception (no participant can audit another's vision pipeline). Escalation to the user is the only honest resolution.
3. **The existing `need_user_input` + `awaiting_user_input` flow already supports this.** No orchestrator code change is needed; the charter text is enforced through prompt embedding, and agents follow it by self-reporting via the existing schema.

The three conclave participants all reached this conclusion independently in round 1 with substantively identical positions; the wording-level differences were resolved by synthesis. Codex's version contributed the most detailed summary structure; Claude's added the "suspected cause of divergence" diagnostic field; Gemini's clarified the "literal vs interpretation" classification. The canonical text combines all three contributions.

## What Was Rejected

- **Internal consensus-forcing on perceptual content.** The pre-amendment behavior — synthesis round triggered on wording divergence regardless of cause — would have run a synthesis round even when the disagreement was about whether an image contains a tree or a flag. Rejected because synthesis cannot improve perception, only paper over it.
- **Mechanical orchestrator-side detection of multimodal disagreement.** Initially considered but rejected for this amendment: detecting "this disagreement is about image content vs. about analysis" requires natural-language parsing of agent outputs and is unreliable. Pushing the detection responsibility to the agents themselves (via charter text in every prompt) is simpler and more honest — the agent is in the best position to know whether its own claim is perceptual or analytical.
- **Auto-routing to Codex when Gemini fails.** Rejected because we cannot reliably identify *which* participant is wrong without Glen. The right response is to surface all positions, not silently substitute one for another.

## Known Risks

- **Agents may not always recognize a perceptual disagreement.** A subtle mis-perception (e.g., one model reads a logo color as "red-orange" while another reads it as "red") may be classified as wording-level disagreement by the agents themselves, triggering an inappropriate synthesis round. Mitigation: the charter explicitly defines the trigger; future amendments may add orchestrator-side heuristics if this is observed in practice.
- **The escalation could be noisy.** If multimodal disagreement is frequent, Glen will be asked to adjudicate many image disputes. If this becomes burdensome, the policy can be amended to allow Glen to pre-authorize one participant as "perception authority" for a given image type.
- **Non-image multimodal (audio, video) is not explicitly covered.** The amendment uses the parenthetical "(or other non-text) content" but does not enumerate. As Switchboard gains support for audio/video, future amendments may need to refine.
- **Claude's `--tools "Read"` mode for images is broader than strict image-read.** Same Read tool can in principle reach other files in `--add-dir` directories. Currently the dir scope is narrow (per-image upload directories), but this widens slightly when images are attached. Acceptable for MVP; revisit if multi-image tasks combine sensitive directories.

## Open Questions

- **Should the orchestrator mechanically detect perceptual-vs-analytical disagreement** as a backstop? Currently relies on agent self-classification via `convergence: need_user_input`. A future amendment could add detection (e.g., when image attachments are present and positions diverge on observable elements) to make the policy robust even if an agent forgets the rule.
- **What if Glen's adjudication conflicts with a participant's confidence?** E.g., a participant claims confidence 0.95 on an observation Glen says is wrong. Should that lower the participant's confidence weighting in future tasks? Not addressed by this amendment.
- **How does this interact with `consult` and `resolve` modes**, where the relationship is hierarchical (primary/consultant) rather than equal? The amendment is written for conclave but the principle ("visual perception is fact, escalate to Glen") applies in those modes too. May warrant a follow-up amendment generalizing.

## Who Is Keeping Continuity

**`claude-code`** as the designated keeper. Responsibilities for this amendment:

- The canonical text is now at `skills/generic/conclave_charter.md` v1.1.
- Every prompt sent by `app/services/prompt_builder.py` will embed the updated charter at runtime (no service restart needed beyond the one that happened during the build; the prompt builder reads via `_load_skill` which caches per process — see note below).
- This decision record is filed at `docs/decisions/0002_multimodal_disagreement_policy.md`.

**Service-restart note**: the prompt builder's `_skill_cache` is populated on first use per process. A service restart is required for the new charter text to take effect in the running instance.
